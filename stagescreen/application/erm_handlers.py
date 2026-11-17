"""Handlers for the eventrentmanager connection."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from ..domain.model import OUTPUT_IDS
from ..i18n import Msg, t
from ..infrastructure.erm import normalize_base_url, validate_token
from . import commands as c
from . import queries as q
from .bus import CommandBus, DomainError, QueryBus

if TYPE_CHECKING:
    from ..container import Services

log = logging.getLogger("ess.erm")

STATUS_TEXT = {
    "unconfigured": "not configured",
    "connecting": "connecting …",
    "online": "connected",
    "offline": "not reachable",
    "unauthorized": "token rejected",
}


def register_erm_handlers(commands: CommandBus, queries: QueryBus, s: Services) -> None:
    def save_settings(cmd: c.SaveErmSettings) -> dict:
        url = normalize_base_url(cmd.base_url)
        s.settings.set("erm.base_url", url)
        s.settings.set("erm.verify_tls", bool(cmd.verify_tls))
        log.info("eventrentmanager server: %s%s", url or "(none)", "" if cmd.verify_tls else " (TLS not verified)")
        s.erm.reconfigure()
        return {"base_url": url}

    def set_token(cmd: c.SetErmToken) -> None:
        output_id = int(cmd.output_id)
        if output_id not in OUTPUT_IDS:
            raise DomainError(Msg("Output {n} does not exist", n=output_id), 404)
        source = "output1" if output_id == 2 and cmd.source == "output1" else "own"
        s.settings.set(f"erm.source.{output_id}", source)
        if cmd.token == "":
            s.settings.delete(f"erm.token.{output_id}")
            log.info("Output %d: display token removed", output_id)
        elif cmd.token is not None:
            s.settings.set(f"erm.token.{output_id}", validate_token(cmd.token))
            log.info("Output %d: display token saved", output_id)
        s.erm.reconfigure()

    async def test(cmd: c.TestErmConnection) -> dict:
        token = cmd.token or (s.erm.token_for(int(cmd.output_id)) if cmd.output_id else None)
        if not token:
            raise DomainError("Please enter a display token first")
        return await s.erm.test(cmd.base_url, token, cmd.verify_tls)

    def status(_: q.GetErmStatus) -> dict[str, Any]:
        outputs = []
        for n in OUTPUT_IDS:
            st = s.erm.output_status(n)
            st["status_text"] = t(STATUS_TEXT.get(st["status"], st["status"]))
            if st.get("message"):
                st["message"] = t(st["message"])
            outputs.append(st)
        return {"base_url": s.erm.base_url, "verify_tls": s.erm.verify_tls, "outputs": outputs}

    def render_erm(query: q.GetRenderErm) -> dict[str, Any]:
        output_id = int(query.output_id)
        if output_id not in OUTPUT_IDS:
            raise DomainError(Msg("Output {n} does not exist", n=output_id), 404)
        cfg = s.output_repo.get(output_id)
        data = s.erm.render_data(cfg.mirror_of or output_id)
        data["now_ms"] = round(time.time() * 1000)  # lets remote browsers correct their own clock
        return data

    for message, handler in [(c.SaveErmSettings, save_settings), (c.SetErmToken, set_token), (c.TestErmConnection, test)]:
        commands.register(message, handler)
    for message, handler in [(q.GetErmStatus, status), (q.GetRenderErm, render_erm)]:
        queries.register(message, handler)
