"""Automatic scene switching per output.

Each output has an ordered list of rules (first match wins = highest priority).
A rule maps a trigger derived from the eventrentmanager state to a scene:

    urgent_message  an urgent message is active
    erm_offline     the ERM server has not been reachable for ``seconds``
    no_program      the display has no programme assigned
    finished        the programme has finished
    running         the programme is running
    pending         the programme has not started yet
    always          fallback, matches always

When a user activates a scene by hand (web UI or API) while automation is
enabled, the output switches to *manual override* until "back to automatic".
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ..domain.model import OUTPUT_IDS
from ..i18n import Msg
from .bus import DomainError

if TYPE_CHECKING:
    from ..container import Services

log = logging.getLogger("ess.automation")

TRIGGERS = ("urgent_message", "erm_offline", "no_program", "finished", "running", "pending", "always")
MAX_RULES = 12
TICK_S = 2.0


def normalize_rules(s: Services, output_id: int, rules: Any) -> list[dict[str, Any]]:
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        raise DomainError(Msg("At most {n} rules", n=MAX_RULES))
    result = []
    for r in rules:
        if not isinstance(r, dict) or r.get("trigger") not in TRIGGERS:
            raise DomainError("Unknown trigger")
        scene = s.scene_repo.get(str(r.get("scene_id") or ""))
        if scene is None or scene["output_id"] != output_id:
            raise DomainError("Please choose a scene of this output for every rule")
        rule = {"trigger": r["trigger"], "scene_id": scene["id"]}
        if r["trigger"] == "erm_offline":
            try:
                seconds = int(r.get("seconds") or 30)
            except (TypeError, ValueError):
                raise DomainError("Invalid number of seconds") from None
            if not 5 <= seconds <= 3600:
                raise DomainError("The offline time must be between 5 and 3600 seconds")
            rule["seconds"] = seconds
        result.append(rule)
    return result


class Automation:
    def __init__(self, services: Services):
        self.s = services
        self.task: asyncio.Task | None = None
        self.current: dict[int, dict[str, Any] | None] = {n: None for n in OUTPUT_IDS}

    # ------------------------------------------------------------------ configuration
    def config(self, output_id: int) -> dict[str, Any]:
        cfg = self.s.settings.get(f"automation.{output_id}") or {}
        return {"enabled": bool(cfg.get("enabled")), "rules": list(cfg.get("rules") or [])}

    def save(self, output_id: int, enabled: bool, rules: list[dict[str, Any]]) -> None:
        self.s.settings.set(f"automation.{output_id}", {"enabled": bool(enabled), "rules": rules})
        if not enabled:
            self.s.settings.delete(f"automation.{output_id}.manual")
        self.evaluate_all()

    def manual(self, output_id: int) -> bool:
        return bool(self.s.settings.get(f"automation.{output_id}.manual"))

    def on_manual_activation(self, output_id: int) -> None:
        if self.config(output_id)["enabled"]:
            self.s.settings.set(f"automation.{output_id}.manual", True)
            log.info("Output %d: manual override – automatic switching paused", output_id)

    def resume(self, output_id: int) -> None:
        self.s.settings.delete(f"automation.{output_id}.manual")
        log.info("Output %d: back to automatic scene switching", output_id)
        self.evaluate(output_id, force=True)

    # ------------------------------------------------------------------ evaluation
    def state_flags(self, output_id: int) -> dict[str, Any]:
        """Which triggers are currently true for an output."""
        flags = {t: False for t in TRIGGERS}
        flags["always"] = True
        poller = self.s.erm.poller_for(output_id)
        flags["offline_for"] = 0.0
        if poller is None:
            return flags
        st = poller.state
        if st.status != "online":
            flags["offline_for"] = time.time() - (st.offline_since or time.time())
            data = st.data  # last known state still counts for the other triggers
        else:
            data = st.data
        if not data:
            return flags
        rs = data.get("runSheet")
        if st.status == "online" and rs is None:
            flags["no_program"] = True
        if rs:
            state = (rs.get("live") or {}).get("state")
            if state in ("pending", "running", "finished"):
                flags[state] = True
            flags["urgent_message"] = any(m.get("urgent") for m in rs.get("messages") or [])
        return flags

    def match(self, output_id: int) -> dict[str, Any] | None:
        flags = self.state_flags(output_id)
        for index, rule in enumerate(self.config(output_id)["rules"]):
            trigger = rule["trigger"]
            hit = (flags["offline_for"] >= rule.get("seconds", 30) and self.s.erm.poller_for(output_id) is not None) \
                if trigger == "erm_offline" else flags.get(trigger, False)
            if hit and self.s.scene_repo.get(rule["scene_id"]) is not None:
                return {"index": index, **rule}
        return None

    def evaluate(self, output_id: int, force: bool = False) -> None:
        cfg = self.config(output_id)
        hit = self.match(output_id) if cfg["enabled"] else None
        self.current[output_id] = hit
        if not cfg["enabled"] or (self.manual(output_id) and not force) or hit is None:
            return
        key = f"scene.active.{output_id}"
        if self.s.settings.get(key) != hit["scene_id"]:
            self.s.settings.set(key, hit["scene_id"])
            scene = self.s.scene_repo.get(hit["scene_id"])
            log.info('Output %d: automatic switch to "%s" (%s)', output_id, scene["name"] if scene else "?", hit["trigger"])
            self.s.notify_change("scenes")

    def evaluate_all(self) -> None:
        for n in OUTPUT_IDS:
            try:
                self.evaluate(n)
            except Exception:  # pragma: no cover - never break the loop
                log.exception("Automation for output %d failed", n)

    async def run(self) -> None:
        while True:
            self.evaluate_all()
            await asyncio.sleep(TICK_S)

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name="ess-automation")

    def stop(self) -> None:
        if self.task:
            self.task.cancel()
            self.task = None

    def view(self, output_id: int) -> dict[str, Any]:
        cfg = self.config(output_id)
        return {**cfg, "manual": self.manual(output_id), "current": self.current.get(output_id)}
