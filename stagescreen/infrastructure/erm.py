"""eventrentmanager Display API client and pollers.

Each configured display token gets one poller that requests
``GET /api/display/v1/state`` in the interval the server asks for
(``pollIntervalSeconds``). The latest payload, the offset between the ERM
server clock and the local clock, and a connection status are kept in memory
and handed to the render pages. See docs/display-api.md and
docs/stagedisplay-api.md in the eventrentmanager repository.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .. import __version__
from ..application.bus import DomainError

log = logging.getLogger("ess.erm")

TOKEN_RE = re.compile(r"^mvd_[A-Za-z0-9_]{8,120}$")
DEFAULT_POLL_S = 3.0
TIMEOUT_S = 8.0


class ErmUnauthorized(Exception):
    pass


class ErmError(Exception):
    pass


def normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+(/[\w\-./~%]*)?", url):
        raise DomainError("Invalid server address")
    return url


def validate_token(token: str) -> str:
    token = (token or "").strip()
    if not TOKEN_RE.fullmatch(token):
        raise DomainError("The display token must start with mvd_ (Settings › Stage & FOH displays in eventrentmanager)")
    return token


def token_hint(token: str | None) -> str | None:
    if not token:
        return None
    return token[:8] + "…" + token[-4:]


def parse_time(value: str | None) -> float | None:
    """ISO 8601 timestamp → epoch seconds."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class ErmClient:
    def __init__(self, base_url: str, verify_tls: bool = True):
        self.base_url = base_url
        self.verify_tls = verify_tls

    def _context(self) -> ssl.SSLContext | None:
        if not self.base_url.startswith("https"):
            return None
        if self.verify_tls:
            return ssl.create_default_context()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def fetch_state(self, token: str) -> dict[str, Any]:
        url = f"{self.base_url}/api/display/v1/state?version=ess-{__version__}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json",
            "User-Agent": f"event-stage-screen/{__version__}",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=self._context()) as res:
                body = res.read()
        except urllib.error.HTTPError as exc:
            message = _error_text(exc)
            if exc.code == 401:
                raise ErmUnauthorized(message or "Invalid or disabled display token") from None
            raise ErmError(f"HTTP {exc.code}" + (f": {message}" if message else "")) from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ErmError(str(reason)) from None
        try:
            data = json.loads(body)
        except ValueError:
            raise ErmError("The server did not answer with JSON – is the address correct?") from None
        if not isinstance(data, dict) or "display" not in data:
            raise ErmError("Unexpected answer – is this an eventrentmanager server?")
        return data


def _error_text(exc: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(exc.read() or b"{}")
        return str(data.get("error") or "")[:200]
    except (ValueError, OSError):
        return ""


@dataclass
class PollerState:
    status: str = "connecting"  # connecting | online | offline | unauthorized
    message: str | None = None
    data: dict[str, Any] | None = None
    offset_ms: float = 0.0          # ERM server clock − local clock
    received_at: float = 0.0        # local epoch seconds of the last successful poll
    last_error_at: float = 0.0
    poll_interval: float = DEFAULT_POLL_S

    def to_dict(self) -> dict[str, Any]:
        d = self.data or {}
        display = d.get("display") or {}
        rs = d.get("runSheet") or {}
        return {
            "status": self.status,
            "message": self.message,
            "received_at": self.received_at or None,
            "poll_interval": self.poll_interval,
            "display_name": display.get("name"),
            "role": display.get("role"),
            "program": (rs.get("program") or {}).get("name") if rs else None,
            "event": (rs.get("event") or {}).get("name") if rs else None,
        }


@dataclass
class ErmPoller:
    client: ErmClient
    token: str
    on_change: Callable[[], None] = lambda: None
    state: PollerState = field(default_factory=PollerState)
    task: asyncio.Task | None = None
    _fingerprint: str = ""

    def poll_once(self) -> float:
        """One blocking request; returns the delay until the next poll."""
        sent = time.time()
        before = (self.state.status, self.state.message)
        try:
            data = self.client.fetch_state(self.token)
        except ErmUnauthorized as exc:
            self.state.status, self.state.message, self.state.last_error_at = "unauthorized", str(exc), time.time()
            self._changed_if(before)
            return 15.0
        except ErmError as exc:
            self.state.status, self.state.message, self.state.last_error_at = "offline", str(exc), time.time()
            self._changed_if(before)
            return min(30.0, max(5.0, self.state.poll_interval * 2))
        received = time.time()
        server = parse_time(data.get("serverTime"))
        if server is not None:
            self.state.offset_ms = round((server - (sent + received) / 2) * 1000)
        self.state.poll_interval = float(min(60, max(1, data.get("pollIntervalSeconds") or DEFAULT_POLL_S)))
        self.state.received_at = received
        self.state.status, self.state.message = "online", None
        fingerprint = json.dumps({k: v for k, v in data.items() if k != "serverTime"}, sort_keys=True)
        if fingerprint != self._fingerprint or before[0] != "online":
            self._fingerprint = fingerprint
            self.state.data = data
            self.on_change()
        else:
            self.state.data = data
        return self.state.poll_interval

    def _changed_if(self, before: tuple) -> None:
        if (self.state.status, self.state.message) != before:
            log.warning("eventrentmanager (%s): %s", token_hint(self.token), self.state.message)
            self.on_change()

    async def run(self) -> None:
        while True:
            delay = await asyncio.to_thread(self.poll_once)
            await asyncio.sleep(delay)

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name=f"ess-erm-{token_hint(self.token)}")

    def stop(self) -> None:
        if self.task:
            self.task.cancel()
            self.task = None


class ErmService:
    """Keeps one poller per distinct token and maps outputs to pollers."""

    def __init__(self, settings, notify: Callable[[str], None]):
        self.settings = settings
        self.notify = notify
        self.pollers: dict[str, ErmPoller] = {}
        self.running = False
        self.loop: asyncio.AbstractEventLoop | None = None

    def _changed(self) -> None:
        """Called from poller threads: hand the notification to the event loop."""
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.notify, "erm")

    # ------------------------------------------------------------------ configuration
    @property
    def base_url(self) -> str:
        return self.settings.get("erm.base_url", "") or ""

    @property
    def verify_tls(self) -> bool:
        return bool(self.settings.get("erm.verify_tls", True))

    def source(self, output_id: int) -> str:
        """'own' or – for output 2 only – 'output1'."""
        src = self.settings.get(f"erm.source.{output_id}", "own")
        return "output1" if output_id == 2 and src == "output1" else "own"

    def token_for(self, output_id: int) -> str | None:
        if self.source(output_id) == "output1":
            return self.settings.get("erm.token.1")
        return self.settings.get(f"erm.token.{output_id}")

    def poller_for(self, output_id: int) -> ErmPoller | None:
        token = self.token_for(output_id)
        return self.pollers.get(token) if token else None

    # ------------------------------------------------------------------ lifecycle
    def reconfigure(self) -> None:
        wanted: set[str] = set()
        if self.base_url:
            for n in (1, 2):
                token = self.token_for(n)
                if token:
                    wanted.add(token)
        client = ErmClient(self.base_url, self.verify_tls) if self.base_url else None
        for token in list(self.pollers):
            p = self.pollers[token]
            if token not in wanted or client is None or p.client.base_url != client.base_url or p.client.verify_tls != client.verify_tls:
                p.stop()
                del self.pollers[token]
        for token in wanted:
            if token not in self.pollers and client is not None:
                poller = ErmPoller(client, token, on_change=self._changed)
                self.pollers[token] = poller
                if self.running:
                    poller.start()
        self.notify("erm")

    def start(self) -> None:
        self.running = True
        self.loop = asyncio.get_running_loop()
        self.reconfigure()
        for p in self.pollers.values():
            p.start()

    def stop(self) -> None:
        self.running = False
        for p in self.pollers.values():
            p.stop()

    # ------------------------------------------------------------------ views
    def output_status(self, output_id: int) -> dict[str, Any]:
        token = self.token_for(output_id)
        poller = self.poller_for(output_id)
        if not self.base_url or not token:
            state = {"status": "unconfigured", "message": None}
        elif poller is None:
            state = {"status": "connecting", "message": None}
        else:
            state = poller.state.to_dict()
        own = self.settings.get(f"erm.token.{output_id}")
        return {"id": output_id, "source": self.source(output_id), "token_hint": token_hint(own), **state}

    def render_data(self, output_id: int) -> dict[str, Any]:
        """ERM data for a render page (see scene-render.js)."""
        poller = self.poller_for(output_id)
        configured = bool(self.base_url and self.token_for(output_id))
        if poller is None:
            return {"configured": configured, "status": "unconfigured" if not configured else "connecting",
                    "data": None, "offset_ms": 0, "received_at_ms": None, "poll_interval": DEFAULT_POLL_S}
        st = poller.state
        data = st.data or {}
        return {
            "configured": True,
            "status": st.status,
            "data": {"runSheet": data.get("runSheet"), "display": data.get("display")} if data else None,
            "offset_ms": st.offset_ms,
            "received_at_ms": round(st.received_at * 1000) if st.received_at else None,
            "poll_interval": st.poll_interval,
        }

    def server_time_ms(self, output_id: int) -> float | None:
        poller = self.poller_for(output_id)
        if poller and poller.state.status == "online":
            return time.time() * 1000 + poller.state.offset_ms
        return None

    async def test(self, base_url: str, token: str, verify_tls: bool) -> dict[str, Any]:
        client = ErmClient(normalize_base_url(base_url), verify_tls)
        try:
            data = await asyncio.to_thread(client.fetch_state, validate_token(token))
        except ErmUnauthorized as exc:
            raise DomainError(str(exc), 401) from None
        except ErmError as exc:
            raise DomainError(str(exc), 502) from None
        display = data.get("display") or {}
        rs = data.get("runSheet") or {}
        return {"display_name": display.get("name"), "role": display.get("role"),
                "program": (rs.get("program") or {}).get("name") if rs else None}
