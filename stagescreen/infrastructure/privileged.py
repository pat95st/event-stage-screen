"""Running privileged system actions.

The web service runs as the unprivileged user ``ess``. Actions that need root
(host name, time zone, NTP, network, reboot) are executed through the root
helper ``deploy/ess-helper`` via ``sudo -n``. The helper validates every
argument again; it is the actual security boundary.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ..application.bus import DomainError
from ..i18n import Msg

log = logging.getLogger("ess.privileged")


class PrivilegedRunner:
    async def run(self, action: str, *args: str, stdin: str | None = None, timeout: float = 60) -> str:
        raise NotImplementedError


class SudoHelper(PrivilegedRunner):
    def __init__(self, helper_path: str):
        self.helper_path = helper_path

    async def run(self, action: str, *args: str, stdin: str | None = None, timeout: float = 60) -> str:
        cmd = ["sudo", "-n", self.helper_path, action, *args]
        log.info("privileged: %s %s", action, " ".join(args))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise DomainError(Msg("System action failed: {error}", error=str(exc)), 500) from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin is not None else None), timeout)
        except TimeoutError:
            proc.kill()
            raise DomainError(Msg("System action timed out: {action}", action=action), 504) from None
        if proc.returncode != 0:
            message = (err or out).decode(errors="replace").strip().splitlines()
            raise DomainError(Msg("System action failed: {error}", error=message[-1] if message else action), 500)
        return out.decode(errors="replace").strip()


@dataclass
class NullRunner(PrivilegedRunner):
    """Development/test backend: records calls instead of changing the system."""

    calls: list[tuple[str, tuple[str, ...], str | None]] = field(default_factory=list)
    responses: dict[str, str] = field(default_factory=dict)

    async def run(self, action: str, *args: str, stdin: str | None = None, timeout: float = 60) -> str:
        self.calls.append((action, args, stdin))
        log.info("privileged (null backend, not executed): %s %s", action, " ".join(args))
        if action == "net-checkpoint":
            return self.responses.get(action, "/org/freedesktop/NetworkManager/Checkpoint/1")
        return self.responses.get(action, "")
