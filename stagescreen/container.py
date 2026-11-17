"""Composition root: builds and wires all services."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import time

from .application.bus import CommandBus, QueryBus
from .application.command_handlers import register_command_handlers
from .application.query_handlers import register_query_handlers
from .config import AppConfig
from .infrastructure import system
from .infrastructure.auth import SessionManager
from .infrastructure.db import Database
from .infrastructure.events import EventHub
from .infrastructure.logs import ring
from .infrastructure.network import NetworkService
from .infrastructure.privileged import NullRunner, PrivilegedRunner, SudoHelper
from .infrastructure.repositories import OutputRepository, SettingsRepository, UserRepository

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

        secret = self.settings.get("auth.secret")
        if not secret:
            secret = secrets.token_urlsafe(48)
            self.settings.set("auth.secret", secret)
        self.sessions = SessionManager(secret)

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
        self._status_task: asyncio.Task | None = None
        self._change_pending = False

    # ------------------------------------------------------------------ helpers used by handlers

    def ndi_limit(self) -> int:
        custom = self.settings.get("ndi.max_sources")
        return int(custom) if custom is not None else system.default_ndi_limit(self.model_family)

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
