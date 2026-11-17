"""Composition root: builds and wires all services."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import time
from pathlib import Path

from .application.bus import CommandBus, QueryBus
from .application.command_handlers import register_command_handlers
from .application.query_handlers import register_query_handlers
from .application.scene_handlers import register_scene_handlers
from .config import AppConfig
from .infrastructure import system
from .infrastructure.auth import SessionManager
from .infrastructure.db import Database
from .infrastructure.events import EventHub
from .infrastructure.logs import ring
from .infrastructure.media import MediaStore
from .infrastructure.network import NetworkService
from .infrastructure.privileged import NullRunner, PrivilegedRunner, SudoHelper
from .infrastructure.repositories import (
    MediaRepository,
    OutputRepository,
    SceneRepository,
    SettingsRepository,
    UserRepository,
)

log = logging.getLogger("ess")

STATUS_INTERVAL_S = 3.0


class Services:
    def __init__(self, config: AppConfig, privileged: PrivilegedRunner | None = None,
                 network: NetworkService | None = None):
        self.config = config
        self.started_at = time.time()
        config.ensure_dirs()
        self.db = Database(config.db_path)
        self.db.migrate()
        self.settings = SettingsRepository(self.db)
        self.user_repo = UserRepository(self.db)
        self.output_repo = OutputRepository(self.db)
        self.scene_repo = SceneRepository(self.db)
        self.media_repo = MediaRepository(self.db)
        self.media_store = MediaStore(config.media_dir)

        secret = self.settings.get("auth.secret")
        if not secret:
            secret = secrets.token_urlsafe(48)
            self.settings.set("auth.secret", secret)
        self.sessions = SessionManager(secret)
        self.kiosk_token = self._kiosk_token()
        self.kiosk_state: dict = {}
        self._tz_cache: tuple[float, str] = (0.0, "UTC")

        self.model = system.pi_model()
        self.model_family = system.model_family(self.model)

        if privileged is None:
            privileged = SudoHelper(config.helper_path) if config.system_backend == "sudo" else NullRunner()
        self.privileged = privileged
        self.network = network or NetworkService(self.privileged)
        self.events = EventHub()

        self.commands = CommandBus()
        self.queries = QueryBus()
        register_command_handlers(self.commands, self)
        register_query_handlers(self.queries, self)
        register_scene_handlers(self.commands, self.queries, self)
        self._status_task: asyncio.Task | None = None
        self._change_pending = False

    # ------------------------------------------------------------------ helpers used by handlers

    def ndi_limit(self) -> int:
        custom = self.settings.get("ndi.max_sources")
        return int(custom) if custom is not None else system.default_ndi_limit(self.model_family)

    def _kiosk_token(self) -> str:
        """Shared secret of the web service and the kiosk agent (same system user).

        The agent reads it from ``<data>/kiosk.token``; render pages receive it in
        their URL and send it with every request.
        """
        token = self.settings.get("kiosk.token")
        if not token:
            token = secrets.token_urlsafe(32)
            self.settings.set("kiosk.token", token)
        path = self.config.data_dir / "kiosk.token"
        try:
            if not path.exists() or path.read_text().strip() != token:
                path.write_text(token)
            path.chmod(0o600)
        except OSError:  # pragma: no cover - read-only data dir
            log.warning("Could not write %s", path)
        return token

    def kiosk_outputs(self) -> dict[str, dict]:
        """Outputs as last reported by the kiosk agent, by connector name."""
        return {o.get("name"): o for o in self.kiosk_state.get("outputs") or [] if isinstance(o, dict)}

    def kiosk_current_mode(self, connector: str) -> dict | None:
        out = self.kiosk_outputs().get(connector) or {}
        return next((m for m in out.get("modes") or [] if m.get("current")), None)

    def bundled_font_available(self, family: str) -> bool:
        from .infrastructure.media import BUNDLED_FONTS

        fonts_dir = Path(__file__).parent / "web" / "static" / "vendor" / "fonts"
        return any((fonts_dir / f[0]).exists() for f in BUNDLED_FONTS.get(family, []))

    def timezone(self) -> str:
        ts, tz = self._tz_cache
        if time.time() - ts > 60:
            tz = system.time_settings()["timezone"] or "UTC"
            self._tz_cache = (time.time(), tz)
        return tz

    def notify_change(self, topic: str = "state") -> None:
        """Coalesce change notifications into one SSE 'changed' event per loop tick."""
        if self._change_pending:
            return
        self._change_pending = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._change_pending = False
            return

        def fire() -> None:
            self._change_pending = False
            self.events.publish("changed", {"topic": topic, "t": time.time()})

        loop.call_soon(fire)

    def request_restart(self, delay: float = 1.0) -> None:
        """Exit the process after a short delay; systemd (Restart=always) starts it again."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        log.warning("Restarting the service in %.0f s", delay)
        loop.call_later(delay, lambda: os.kill(os.getpid(), signal.SIGTERM))

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.events.bind_loop(loop)
        ring().listeners.append(lambda e: self.events.publish_threadsafe("log", e))
        self._status_task = asyncio.create_task(self._status_loop(), name="ess-status")
        log.info("event-stage-screen started on %s – web interface on port %d", self.model, self.config.port)

    async def shutdown(self) -> None:
        if self._status_task:
            self._status_task.cancel()
        self.db.close()

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(STATUS_INTERVAL_S)
            if self.events.has_subscribers:
                try:
                    self.events.publish("status", await self.queries.dispatch(_status_query()))
                except Exception:  # pragma: no cover - status is best effort
                    log.debug("status update failed", exc_info=True)


def _status_query():
    from .application.queries import GetStatus

    return GetStatus()
