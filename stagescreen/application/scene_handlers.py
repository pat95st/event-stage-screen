"""Handlers for scenes, the media library, the render pages and the kiosk agent."""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import socket
import time
from typing import TYPE_CHECKING, Any

from .. import __version__
from ..domain.model import OUTPUT_IDS
from ..domain.scene import empty_scene, fonts_used, media_ids, normalize_scene, validate_scene_name
from ..i18n import Msg
from ..infrastructure import system
from ..infrastructure.media import BUNDLED_FONTS, SYSTEM_FONTS
from . import commands as c
from . import queries as q
from .bus import CommandBus, DomainError, NotFound, QueryBus

if TYPE_CHECKING:
    from ..container import Services

log = logging.getLogger("ess.scenes")

SPLASH_MODES = ("info", "logo", "black")
EXPORT_FORMAT = "event-stage-screen-scene"


def new_scene_id() -> str:
    return "sc_" + secrets.token_hex(6)


def register_scene_handlers(commands: CommandBus, queries: QueryBus, s: Services) -> None:
    # ------------------------------------------------------------ helpers

    def scene_or_404(scene_id: str) -> dict[str, Any]:
        scene = s.scene_repo.get(scene_id)
        if scene is None:
            raise NotFound("Scene not found")
        return scene

    def check_output(output_id: int) -> int:
        if output_id not in OUTPUT_IDS:
            raise DomainError(Msg("Output {n} does not exist", n=output_id), 404)
        return output_id

    def check_media_refs(content: dict[str, Any]) -> None:
        for media_id in media_ids(content):
            item = s.media_repo.get(media_id)
            if item is None or item["kind"] != "image":
                raise DomainError("The scene references an image that does not exist")

    def active_scene_id(output_id: int) -> str | None:
        return s.settings.get(f"scene.active.{output_id}")

    def changed(topic: str = "scenes") -> None:
        s.notify_change(topic)

    def default_size(output_id: int) -> tuple[int, int]:
        cfg = s.output_repo.get(output_id)
        if cfg.mode:
            return cfg.mode.width, cfg.mode.height
        current = s.kiosk_current_mode(cfg.connector)
        if current:
            return current["width"], current["height"]
        return 1920, 1080

    # ------------------------------------------------------------ scene commands

    def create_scene(cmd: c.CreateScene) -> dict:
        output_id = check_output(int(cmd.output_id))
        name = validate_scene_name(cmd.name)
        w, h = (cmd.width, cmd.height) if cmd.width and cmd.height else default_size(output_id)
        content = empty_scene(int(w), int(h))
        scene_id = new_scene_id()
        s.scene_repo.create(scene_id, output_id, name, content)
        log.info('Scene "%s" created for output %d (%d×%d)', name, output_id, content["width"], content["height"])
        if active_scene_id(output_id) is None:
            s.settings.set(f"scene.active.{output_id}", scene_id)
        changed()
        return s.scene_repo.get(scene_id)

    def save_draft(cmd: c.SaveSceneDraft) -> dict:
        scene_or_404(cmd.scene_id)
        content = normalize_scene(cmd.content)
        check_media_refs(content)
        s.scene_repo.save_draft(cmd.scene_id, content)
        changed("drafts")
        return {"saved": True}

    def publish(cmd: c.PublishScene) -> dict:
        scene = scene_or_404(cmd.scene_id)
        if cmd.content is not None:
            content = normalize_scene(cmd.content)
        elif scene["draft"] is not None:
            content = scene["draft"]
        else:
            return {"published": False}
        check_media_refs(content)
        s.scene_repo.publish(cmd.scene_id, content)
        log.info('Scene "%s" published', scene["name"])
        changed()
        return {"published": True}

    def discard(cmd: c.DiscardSceneDraft) -> None:
        scene_or_404(cmd.scene_id)
        s.scene_repo.save_draft(cmd.scene_id, None)
        changed("drafts")

    def rename(cmd: c.RenameScene) -> None:
        scene_or_404(cmd.scene_id)
        s.scene_repo.rename(cmd.scene_id, validate_scene_name(cmd.name))
        changed()

    def duplicate(cmd: c.DuplicateScene) -> dict:
        scene = scene_or_404(cmd.scene_id)
        output_id = check_output(int(cmd.output_id or scene["output_id"]))
        scene_id = new_scene_id()
        name = (scene["name"] + " (copy)")[:60]
        s.scene_repo.create(scene_id, output_id, name, scene["draft"] or scene["live"])
        changed()
        return s.scene_repo.get(scene_id)

    def delete(cmd: c.DeleteScene) -> None:
        scene = scene_or_404(cmd.scene_id)
        s.scene_repo.delete(cmd.scene_id)
        if active_scene_id(scene["output_id"]) == cmd.scene_id:
            s.settings.delete(f"scene.active.{scene['output_id']}")
        log.info('Scene "%s" deleted', scene["name"])
        changed()

    def reorder(cmd: c.ReorderScenes) -> None:
        check_output(int(cmd.output_id))
        s.scene_repo.reorder(int(cmd.output_id), [str(x) for x in cmd.scene_ids])
        changed()

    def activate(cmd: c.ActivateScene) -> None:
        output_id = check_output(int(cmd.output_id))
        if cmd.scene_id is None:
            s.settings.delete(f"scene.active.{output_id}")
            log.info("Output %d: no scene (splash screen)", output_id)
        else:
            scene = scene_or_404(cmd.scene_id)
            if scene["output_id"] != output_id:
                raise DomainError("The scene belongs to another output")
            s.settings.set(f"scene.active.{output_id}", cmd.scene_id)
            log.info('Output %d: scene "%s" is live', output_id, scene["name"])
        changed()

    def import_scene(cmd: c.ImportScene) -> dict:
        output_id = check_output(int(cmd.output_id))
        data = cmd.data
        if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT:
            raise DomainError("This is not a scene export")
        id_map: dict[str, str] = {}
        for item in data.get("media") or []:
            raw = base64.b64decode(item.get("data") or "", validate=True)
            existing = s.media_repo.by_sha(hashlib.sha256(raw).hexdigest())
            if existing:
                id_map[item.get("id")] = existing["id"]
                continue
            stored = s.media_store.save(raw, item.get("name") or "image", item.get("family"))
            s.media_repo.add(stored)
            id_map[item.get("id")] = stored["id"]
        content = dict(data.get("scene") or {})
        for el in content.get("elements") or []:
            if isinstance(el, dict) and el.get("type") == "image":
                props = el.setdefault("props", {})
                if props.get("media_id"):
                    props["media_id"] = id_map.get(props["media_id"], props["media_id"])
                    if s.media_repo.get(props["media_id"]) is None:
                        props["media_id"] = None
        content = normalize_scene(content)
        scene_id = new_scene_id()
        name = validate_scene_name(data.get("name") or "Imported scene")
        s.scene_repo.create(scene_id, output_id, name, content)
        log.info('Scene "%s" imported for output %d', name, output_id)
        changed()
        if id_map:
            changed("media")
        return s.scene_repo.get(scene_id)

    # ------------------------------------------------------------ media commands

    def upload_media(cmd: c.UploadMedia) -> dict:
        item = s.media_store.save(cmd.data, cmd.name, cmd.family)
        existing = s.media_repo.by_sha(item["sha256"])
        if existing:
            s.media_store.delete(item["filename"])
            return existing
        s.media_repo.add(item)
        log.info('Media "%s" uploaded (%s, %d kB)', item["name"], item["mime"], item["size"] // 1024)
        changed("media")
        return s.media_repo.get(item["id"])

    def rename_media(cmd: c.RenameMedia) -> None:
        item = s.media_repo.get(cmd.media_id)
        if item is None:
            raise NotFound("File not found")
        name = str(cmd.name or "").strip()[:120]
        if not name:
            raise DomainError("Name: 1–120 characters")
        family = None
        if item["kind"] == "font" and cmd.family is not None:
            from ..domain.scene import FONT_RE

            family = cmd.family.strip()
            if not FONT_RE.fullmatch(family):
                raise DomainError(Msg('Invalid font name "{font}"', font=family[:40]))
        s.media_repo.rename(cmd.media_id, name, family)
        changed("media")

    def delete_media(cmd: c.DeleteMedia) -> None:
        item = s.media_repo.get(cmd.media_id)
        if item is None:
            raise NotFound("File not found")
        users = [sc["name"] for sc in s.scene_repo.list()
                 if cmd.media_id in media_ids(sc["live"]) | media_ids(sc["draft"] or {})]
        if item["kind"] == "image" and s.settings.get("splash.logo") == cmd.media_id:
            users.append("Splash screen")
        if users:
            raise DomainError(Msg("The file is still used by: {names}", names=", ".join(users)), 409)
        s.media_repo.delete(cmd.media_id)
        s.media_store.delete(item["filename"])
        log.info('Media "%s" deleted', item["name"])
        changed("media")

    def save_splash(cmd: c.SaveSplash) -> None:
        if cmd.mode not in SPLASH_MODES:
            raise DomainError("Unknown splash mode")
        if cmd.logo_media_id:
            item = s.media_repo.get(cmd.logo_media_id)
            if item is None or item["kind"] != "image":
                raise DomainError("The selected logo does not exist")
            s.settings.set("splash.logo", cmd.logo_media_id)
        else:
            s.settings.delete("splash.logo")
        s.settings.set("splash.mode", cmd.mode)
        changed("splash")

    def report_kiosk(cmd: c.ReportKioskState) -> None:
        report = cmd.report if isinstance(cmd.report, dict) else {}
        before = s.kiosk_state.get("outputs") if s.kiosk_state else None
        s.kiosk_state = {**report, "received_at": time.time()}
        if before != report.get("outputs"):
            changed("outputs")

    # ------------------------------------------------------------ queries

    def scene_summary(sc: dict[str, Any], active: str | None) -> dict[str, Any]:
        content = sc["draft"] or sc["live"]
        return {"id": sc["id"], "output_id": sc["output_id"], "name": sc["name"], "has_draft": sc["draft"] is not None,
                "width": content["width"], "height": content["height"], "elements": len(content["elements"]),
                "active": sc["id"] == active, "updated_at": sc["updated_at"]}

    def list_scenes(_: q.ListScenes) -> dict:
        outputs = []
        for cfg in s.output_repo.all():
            active = active_scene_id(cfg.id)
            outputs.append({
                "id": cfg.id, "name": cfg.name, "mirror_of": cfg.mirror_of, "active": active,
                "scenes": [scene_summary(sc, active) for sc in s.scene_repo.list(cfg.id)],
            })
        return {"outputs": outputs}

    def get_scene(query: q.GetScene) -> dict:
        scene = scene_or_404(query.scene_id)
        scene["active"] = active_scene_id(scene["output_id"]) == scene["id"]
        return scene

    def export_scene(query: q.ExportScene) -> dict:
        scene = scene_or_404(query.scene_id)
        content = scene["draft"] or scene["live"]
        media = []
        if query.include_media:
            for media_id in sorted(media_ids(content)):
                item = s.media_repo.get(media_id)
                if item:
                    raw = s.media_store.path(item["filename"]).read_bytes()
                    media.append({"id": media_id, "name": item["name"], "mime": item["mime"],
                                  "data": base64.b64encode(raw).decode("ascii")})
        return {"format": EXPORT_FORMAT, "version": 1, "app_version": __version__, "name": scene["name"],
                "scene": content, "media": media, "fonts": sorted(fonts_used(content))}

    def list_media(_: q.ListMedia) -> dict:
        items = s.media_repo.list()
        for item in items:
            item["url"] = f"/media/{item['id']}"
        return {"items": items}

    def fonts(_: q.GetFonts) -> dict:
        uploaded = sorted({m["family"] for m in s.media_repo.list("font") if m.get("family")})
        bundled = [f for f in BUNDLED_FONTS if s.bundled_font_available(f)]
        return {"bundled": bundled, "uploaded": uploaded, "system": SYSTEM_FONTS}

    def render_state(query: q.GetRenderState) -> dict:
        output_id = check_output(int(query.output_id))
        cfg = s.output_repo.get(output_id)
        source = cfg.mirror_of or output_id
        scene_id = active_scene_id(source)
        scene = s.scene_repo.get(scene_id) if scene_id else None
        logo = s.settings.get("splash.logo")
        return {
            "output_id": output_id,
            "enabled": cfg.enabled,
            "source_output": source,
            "mirror": ({"scaling": cfg.mirror.scaling.value, "rotation": cfg.mirror.rotation,
                        "flip_h": cfg.mirror.flip_h, "flip_v": cfg.mirror.flip_v} if cfg.mirror_of else None),
            "scene_id": scene["id"] if scene else None,
            "scene_name": scene["name"] if scene else None,
            "scene": scene["live"] if scene else None,
            "splash": {"mode": s.settings.get("splash.mode", "info"), "logo": logo},
            "hostname": socket.gethostname(),
            "ip_addresses": system.ip_addresses(),
            "port": s.config.port,
            "output_name": cfg.name,
            "timezone": s.timezone(),
            "locale": s.settings.get("ui.language", "en"),
            "erm": {**s.erm.render_data(source), "now_ms": round(time.time() * 1000)},
        }

    def kiosk_config(_: q.GetKioskConfig) -> dict:
        outputs = []
        for cfg in s.output_repo.all():
            outputs.append({
                "id": cfg.id, "connector": cfg.connector, "enabled": cfg.enabled,
                "mode": ({"width": cfg.mode.width, "height": cfg.mode.height, "refresh": cfg.mode.refresh}
                         if cfg.mode else None),
                "custom_mode": cfg.custom_mode, "rotation": cfg.rotation,
                "url": f"http://127.0.0.1:{s.config.port}/render/{cfg.id}",
            })
        return {"outputs": outputs, "token": s.kiosk_token}

    for message, handler in [
        (c.CreateScene, create_scene), (c.SaveSceneDraft, save_draft), (c.PublishScene, publish),
        (c.DiscardSceneDraft, discard), (c.RenameScene, rename), (c.DuplicateScene, duplicate),
        (c.DeleteScene, delete), (c.ReorderScenes, reorder), (c.ActivateScene, activate), (c.ImportScene, import_scene),
        (c.UploadMedia, upload_media), (c.RenameMedia, rename_media), (c.DeleteMedia, delete_media),
        (c.SaveSplash, save_splash), (c.ReportKioskState, report_kiosk),
    ]:
        commands.register(message, handler)
    for message, handler in [
        (q.ListScenes, list_scenes), (q.GetScene, get_scene), (q.ExportScene, export_scene),
        (q.ListMedia, list_media), (q.GetFonts, fonts), (q.GetRenderState, render_state),
        (q.GetKioskConfig, kiosk_config),
    ]:
        queries.register(message, handler)
