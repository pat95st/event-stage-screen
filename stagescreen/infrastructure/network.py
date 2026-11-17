"""Network configuration through NetworkManager (``nmcli``).

Reading uses ``nmcli`` directly (no privileges needed). Changes go through the
root helper and are protected by a NetworkManager *checkpoint*: before a
change, a checkpoint with a rollback timeout is created. If the user does not
confirm the new settings within :data:`ROLLBACK_SECONDS` (for example because
the device is no longer reachable), NetworkManager restores the previous
configuration on its own.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..application.bus import DomainError
from ..domain.model import Ipv4Settings
from .privileged import PrivilegedRunner

log = logging.getLogger("ess.network")

ROLLBACK_SECONDS = 60
MANAGED_TYPES = ("ethernet", "wifi")

ReadCommand = Callable[[list[str]], Awaitable[str]]


async def run_nmcli(args: list[str]) -> str:
    """Run ``nmcli`` read-only and return stdout ('' if unavailable)."""
    if not shutil.which("nmcli"):
        return ""
    proc = await asyncio.create_subprocess_exec(
        "nmcli", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
    except TimeoutError:
        proc.kill()
        return ""
    return out.decode(errors="replace")


def split_terse(line: str) -> list[str]:
    """Split one line of ``nmcli -t`` output (':' separated, '\\:' escaped)."""
    fields, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    fields.append("".join(cur))
    return fields


def parse_key_values(text: str) -> dict[str, list[str]]:
    """Parse ``nmcli -t -f a,b device/connection show`` output into lists per key."""
    result: dict[str, list[str]] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.split("[")[0]
        value = value.replace("\\:", ":")
        if key in ("ipv4.addresses", "ipv4.dns"):
            values = [v.strip() for v in value.split(",") if v.strip()]
        else:
            values = [value.strip()] if value.strip() and value.strip() != "--" else []
        result.setdefault(key, []).extend(values)
    return result


@dataclass
class PendingChange:
    checkpoint: str
    device: str
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "expires_at": self.expires_at,
            "seconds_left": max(0, int(self.expires_at - time.time())),
        }


class NetworkService:
    def __init__(self, runner: PrivilegedRunner, read: ReadCommand = run_nmcli):
        self.runner = runner
        self.read = read
        self.pending: PendingChange | None = None

    # ------------------------------------------------------------------ read

    async def available(self) -> bool:
        return bool((await self.read(["-t", "-f", "RUNNING", "general"])).strip())

    async def devices(self) -> list[dict[str, Any]]:
        out = await self.read(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"])
        devices = []
        for line in out.splitlines():
            parts = split_terse(line)
            if len(parts) < 4 or parts[1] not in MANAGED_TYPES:
                continue
            name, dev_type, state, connection = parts[:4]
            info = parse_key_values(await self.read(["-t", "-f", "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS", "device", "show", name]))
            config: dict[str, Any] = {"method": None, "addresses": [], "gateway": None, "dns": []}
            if connection and connection != "--":
                con = parse_key_values(await self.read(
                    ["-t", "-f", "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns", "connection", "show", connection]))
                config = {
                    "method": (con.get("ipv4.method") or [None])[0],
                    "addresses": con.get("ipv4.addresses", []),
                    "gateway": (con.get("ipv4.gateway") or [None])[0],
                    "dns": con.get("ipv4.dns", []),
                }
            devices.append({
                "device": name,
                "type": dev_type,
                "state": state,
                "connection": None if connection in ("", "--") else connection,
                "addresses": info.get("IP4.ADDRESS", []),
                "gateway": (info.get("IP4.GATEWAY") or [None])[0],
                "dns": info.get("IP4.DNS", []),
                "config": config,
            })
        return devices

    async def wifi_networks(self) -> list[dict[str, Any]]:
        out = await self.read(["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes"])
        networks: dict[str, dict[str, Any]] = {}
        for line in out.splitlines():
            parts = split_terse(line)
            if len(parts) < 4 or not parts[1].strip():  # hidden networks have no SSID
                continue
            ssid = parts[1]
            signal = int(parts[2]) if parts[2].isdigit() else 0
            if ssid not in networks or networks[ssid]["signal"] < signal:
                networks[ssid] = {"ssid": ssid, "signal": signal, "security": parts[3] if parts[3] != "--" else "",
                                  "in_use": parts[0] == "*"}
        return sorted(networks.values(), key=lambda n: (-n["in_use"], -n["signal"]))

    def state(self) -> dict[str, Any] | None:
        if self.pending and self.pending.expires_at < time.time():
            log.warning("Network change on %s was not confirmed and has been rolled back", self.pending.device)
            self.pending = None
        return self.pending.to_dict() if self.pending else None

    # ------------------------------------------------------------------ change

    async def apply_ipv4(self, device: str, settings: Ipv4Settings) -> dict[str, Any]:
        args = [device, settings.method]
        if settings.method == "manual":
            args += [settings.address or "", settings.gateway or "-"]
        args += list(settings.dns)
        return await self._guarded(device, "net-ipv4", args)

    async def connect_wifi(self, device: str, ssid: str, password: str) -> dict[str, Any]:
        if not ssid or len(ssid) > 32:
            raise DomainError("SSID: 1–32 characters")
        if password and not (8 <= len(password) <= 63):
            raise DomainError("Wi-Fi password: 8–63 characters")
        return await self._guarded(device, "net-wifi", [device, ssid], stdin=password)

    async def _guarded(self, device: str, action: str, args: list[str], stdin: str | None = None) -> dict[str, Any]:
        await self._known_device(device)
        if self.state():
            raise DomainError("Another network change is waiting for confirmation", 409)
        checkpoint = await self.runner.run("net-checkpoint", str(ROLLBACK_SECONDS))
        self.pending = PendingChange(checkpoint.strip(), device, time.time() + ROLLBACK_SECONDS)
        log.info("Network change on %s (rollback in %d s unless confirmed)", device, ROLLBACK_SECONDS)

        async def apply() -> None:
            # Give the HTTP response a moment to reach the browser: the change
            # may interrupt the connection the request came in on.
            await asyncio.sleep(0.5)
            try:
                await self.runner.run(action, *args, stdin=stdin, timeout=90)
            except DomainError as exc:
                log.error("Network change failed: %s – rolling back", exc)
                await self.rollback()

        asyncio.create_task(apply(), name="ess-network-apply")
        return self.pending.to_dict()

    async def confirm(self) -> None:
        pending = self.pending
        if not self.state() or pending is None:
            raise DomainError("There is no network change to confirm (it may already have been rolled back)", 409)
        await self.runner.run("net-confirm", pending.checkpoint)
        self.pending = None
        log.info("Network change on %s confirmed", pending.device)

    async def rollback(self) -> None:
        pending = self.pending
        if pending is None:
            return
        self.pending = None
        try:
            await self.runner.run("net-rollback", pending.checkpoint)
            log.info("Network change on %s rolled back", pending.device)
        except DomainError as exc:
            log.error("Rollback failed: %s", exc)

    async def _known_device(self, device: str) -> None:
        names = {d["device"] for d in await self.devices()}
        if device not in names:
            raise DomainError("Unknown network interface", 404)
