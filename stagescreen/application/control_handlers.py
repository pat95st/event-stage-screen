"""Handlers for operating the outputs: setup aids, thumbnails, automation, API keys and updates."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..domain.model import OUTPUT_IDS
from ..i18n import Msg
from ..infrastructure.backup import create_backup
from ..infrastructure.updates import check_latest, list_backups, parse_version, prune_backups
from . import commands as c
from . import queries as q
from .automation import TRIGGERS, normalize_rules
from .bus import CommandBus, DomainError, QueryBus

if TYPE_CHECKING:
    from ..container import Services

log = logging.getLogger("ess.control")

TEST_PATTERNS = ("bars", "grid", "edges")
MAX_THUMBNAIL_BYTES = 2 * 2**20
THUMBNAIL_WANTED_S = 30


def register_control_handlers(commands: CommandBus, queries: QueryBus, s: Services) -> None:
    def targets(output_id: int | None) -> list[int]:
        if output_id is None:
            return list(OUTPUT_IDS)
        if int(output_id) not in OUTPUT_IDS:
            raise DomainError(Msg("Output {n} does not exist", n=output_id), 404)
        return [int(output_id)]

    # ------------------------------------------------------------ setup aids

    def blackout(cmd: c.SetBlackout) -> dict:
        outs = targets(cmd.output_id)
        new = (not all(s.overlays[n]["blackout"] for n in outs)) if cmd.on is None else bool(cmd.on)
        for n in outs:
            s.overlays[n]["blackout"] = new
        log.info("Blackout %s on output(s) %s", "on" if new else "off", ", ".join(map(str, outs)))
        s.notify_change("overlay")
        return {"blackout": new}

    def test_pattern(cmd: c.SetTestPattern) -> dict:
        pattern = cmd.pattern or None
        if pattern not in (None, *TEST_PATTERNS):
            raise DomainError("Unknown test pattern")
        for n in targets(cmd.output_id):
            s.overlays[n]["test_pattern"] = pattern
        s.notify_change("overlay")
        return {"test_pattern": pattern}

    def identify(cmd: c.IdentifyOutputs) -> None:
        until = time.time() + min(30.0, max(1.0, float(cmd.seconds or 6)))
        for n in targets(cmd.output_id):
            s.overlays[n]["identify_until"] = until
        s.notify_change("overlay")

    def store_thumbnail(cmd: c.StoreThumbnail) -> None:
        targets(cmd.output_id)
        if not cmd.data.startswith((b"\xff\xd8", b"\x89PNG\r\n\x1a\n")) or len(cmd.data) > MAX_THUMBNAIL_BYTES:
            raise DomainError("Invalid thumbnail")
        s.thumbnails[int(cmd.output_id)] = (cmd.data, time.time())

    def thumbnail(query: q.GetThumbnail) -> dict[str, Any] | None:
        n = targets(query.output_id)[0]
        s.thumbnail_wanted_until = time.time() + THUMBNAIL_WANTED_S
        item = s.thumbnails.get(n)
        if not item:
            return None
        return {"data": item[0], "age": time.time() - item[1]}

    # ------------------------------------------------------------ automation

    def save_automation(cmd: c.SaveAutomation) -> dict:
        n = targets(cmd.output_id)[0]
        rules = normalize_rules(s, n, cmd.rules)
        s.automation.save(n, cmd.enabled, rules)
        log.info("Output %d: automatic switching %s (%d rules)", n, "on" if cmd.enabled else "off", len(rules))
        s.notify_change("scenes")
        return s.automation.view(n)

    def resume(cmd: c.ResumeAutomation) -> dict:
        n = targets(cmd.output_id)[0]
        if not s.automation.config(n)["enabled"]:
            raise DomainError("Automatic switching is not enabled for this output")
        s.automation.resume(n)
        s.notify_change("scenes")
        return s.automation.view(n)

    def automation_view(_: q.GetAutomation) -> dict[str, Any]:
        outputs = []
        for cfg in s.output_repo.all():
            outputs.append({"id": cfg.id, "name": cfg.name, "mirror_of": cfg.mirror_of,
                            "erm_configured": s.erm.poller_for(cfg.id) is not None,
                            "active": s.settings.get(f"scene.active.{cfg.id}"),
                            "scenes": [{"id": sc["id"], "name": sc["name"]} for sc in s.scene_repo.list(cfg.id)],
                            "flags": {k: v for k, v in s.automation.state_flags(cfg.id).items() if k != "offline_for"},
                            "offline_for": round(s.automation.state_flags(cfg.id)["offline_for"]),
                            **s.automation.view(cfg.id)})
        return {"outputs": outputs, "triggers": list(TRIGGERS)}

    # ------------------------------------------------------------ API keys

    def create_api_key(cmd: c.CreateApiKey) -> dict:
        name = str(cmd.name or "").strip()
        if not 1 <= len(name) <= 40:
            raise DomainError("Name: 1–40 characters")
        keys = s.settings.get("api.keys", []) or []
        if len(keys) >= 10:
            raise DomainError("At most 10 API keys")
        plain = "ess_" + secrets.token_urlsafe(32)
        item = {"id": secrets.token_hex(6), "name": name, "hash": hash_key(plain), "hint": plain[:8] + "…" + plain[-4:],
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "last_used_at": None}
        s.settings.set("api.keys", keys + [item])
        log.info('API key "%s" created', name)
        return {"id": item["id"], "name": name, "key": plain}

    def delete_api_key(cmd: c.DeleteApiKey) -> None:
        keys = s.settings.get("api.keys", []) or []
        rest = [k for k in keys if k["id"] != cmd.key_id]
        if len(rest) == len(keys):
            raise DomainError("API key not found", 404)
        s.settings.set("api.keys", rest)
        log.info("API key %s deleted", cmd.key_id)

    def list_api_keys(_: q.ListApiKeys) -> dict:
        keys = s.settings.get("api.keys", []) or []
        return {"keys": [{k: v for k, v in key.items() if k != "hash"} for key in keys]}

    # ------------------------------------------------------------ remote control

    def scenes_of(n: int) -> list[dict[str, Any]]:
        return s.scene_repo.list(n)

    def activate_ref(cmd: c.ActivateSceneRef) -> dict:
        n = targets(cmd.output_id)[0]
        ref = str(cmd.scene or "").strip()
        scenes = scenes_of(n)
        scene = next((sc for sc in scenes if sc["id"] == ref), None) or \
            next((sc for sc in scenes if sc["name"].casefold() == ref.casefold()), None)
        if scene is None:
            raise DomainError(Msg('Scene "{name}" not found on output {n}', name=ref[:60], n=n), 404)
        s.commands_sync_activate(n, scene["id"])
        return {"output": n, "scene_id": scene["id"], "scene": scene["name"]}

    def cycle(cmd: c.CycleScene) -> dict:
        n = targets(cmd.output_id)[0]
        scenes = scenes_of(n)
        if not scenes:
            raise DomainError("This output has no scenes", 404)
        ids = [sc["id"] for sc in scenes]
        active = s.settings.get(f"scene.active.{n}")
        index = ids.index(active) if active in ids else -1
        scene = scenes[(index + (1 if cmd.step >= 0 else -1)) % len(scenes)]
        s.commands_sync_activate(n, scene["id"])
        return {"output": n, "scene_id": scene["id"], "scene": scene["name"]}

    def element_visibility(cmd: c.SetElementVisibility) -> dict:
        n = targets(cmd.output_id)[0]
        cfg = s.output_repo.get(n)
        scene_id = s.settings.get(f"scene.active.{cfg.mirror_of or n}")
        scene = s.scene_repo.get(scene_id) if scene_id else None
        if scene is None:
            raise DomainError("No scene is live on this output", 404)
        ref = str(cmd.element or "").strip()

        def find(content: dict) -> dict | None:
            els = content.get("elements", [])
            return next((e for e in els if e["id"] == ref), None) or \
                next((e for e in els if e["name"].casefold() == ref.casefold()), None)

        el = find(scene["live"])
        if el is None:
            raise DomainError(Msg('Element "{name}" not found in the live scene', name=ref[:60]), 404)
        visible = (not el["visible"]) if cmd.visible is None else bool(cmd.visible)
        el["visible"] = visible
        s.scene_repo.publish(scene["id"], scene["live"])
        if scene["draft"] is not None:  # keep an open draft consistent
            d = find(scene["draft"])
            if d is not None:
                d["visible"] = visible
            s.scene_repo.save_draft(scene["id"], scene["draft"])
        s.notify_change("scenes")
        return {"element": el["name"], "visible": visible}

    def remote_status(_: q.GetRemoteStatus) -> dict:
        outputs = []
        for cfg in s.output_repo.all():
            active = s.settings.get(f"scene.active.{cfg.id}")
            outputs.append({
                "id": cfg.id, "name": cfg.name, "enabled": cfg.enabled, "mirror_of": cfg.mirror_of,
                "scene_id": active, "scene": next((sc["name"] for sc in scenes_of(cfg.id) if sc["id"] == active), None),
                "scenes": [{"id": sc["id"], "name": sc["name"]} for sc in scenes_of(cfg.id)],
                "automation": {k: v for k, v in s.automation.view(cfg.id).items() if k in ("enabled", "manual")},
                **overlay_view(s, cfg.id),
                "erm": s.erm.output_status(cfg.id)["status"],
            })
        return {"version": __version__, "outputs": outputs}

    # ------------------------------------------------------------ updates

    async def check_updates(_: q.CheckForUpdates) -> dict:
        import asyncio

        result = await asyncio.to_thread(check_latest, s.config.update_repo)
        s.update_check = result
        return result

    async def install_update(cmd: c.InstallUpdate) -> dict:
        import asyncio

        tag = str(cmd.tag or "").strip()
        if parse_version(tag) is None:
            raise DomainError("Invalid version")
        s.config.backup_dir.mkdir(parents=True, exist_ok=True)
        name = f"pre-update-{__version__}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        data = await asyncio.to_thread(create_backup, s.config, s.db)
        (s.config.backup_dir / name).write_bytes(data)
        prune_backups(s.config.backup_dir)
        log.warning("Installing update %s (backup %s)", tag, name)
        unit = await s.privileged.run("update", tag, timeout=60)
        return {"tag": tag, "backup": name, "unit": unit}

    def backups(_: q.ListBackups) -> dict:
        return {"backups": list_backups(s.config.backup_dir)}

    for message, handler in [
        (c.InstallUpdate, install_update),
        (c.CreateApiKey, create_api_key), (c.DeleteApiKey, delete_api_key), (c.ActivateSceneRef, activate_ref),
        (c.CycleScene, cycle), (c.SetElementVisibility, element_visibility),
        (c.SaveAutomation, save_automation), (c.ResumeAutomation, resume),
        (c.SetBlackout, blackout), (c.SetTestPattern, test_pattern), (c.IdentifyOutputs, identify),
        (c.StoreThumbnail, store_thumbnail),
    ]:
        commands.register(message, handler)
    for message, handler in [(q.GetThumbnail, thumbnail), (q.GetAutomation, automation_view),
                             (q.ListApiKeys, list_api_keys), (q.GetRemoteStatus, remote_status),
                             (q.CheckForUpdates, check_updates), (q.ListBackups, backups)]:
        queries.register(message, handler)


def overlay_view(s: Services, output_id: int) -> dict[str, Any]:
    o = s.overlays[output_id]
    remaining = max(0.0, o["identify_until"] - time.time())
    return {"blackout": o["blackout"], "test_pattern": o["test_pattern"], "identify_ms": round(remaining * 1000)}


def hash_key(plain: str) -> str:
    return hashlib.sha256(plain.encode()).hexdigest()


def verify_api_key(s: Services, plain: str | None) -> dict[str, Any] | None:
    """Return the matching API key entry (and record its use) or None."""
    if not plain or not plain.startswith("ess_"):
        return None
    digest = hash_key(plain)
    keys = s.settings.get("api.keys", []) or []
    for key in keys:
        if hmac.compare_digest(key["hash"], digest):
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            if (key.get("last_used_at") or "")[:16] != now[:16]:  # write at most once a minute
                key["last_used_at"] = now
                s.settings.set("api.keys", keys)
            return key
    return None
