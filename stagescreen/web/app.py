"""Web application: HTML pages, JSON API (commands/queries) and the SSE live stream."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import socket
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from ..application import commands as c
from ..application import queries as q
from ..application.bus import DomainError
from ..application.control_handlers import verify_api_key
from ..container import Services
from ..i18n import LANGUAGES, Msg, catalog_for, current_language, set_language, t, text_of
from ..infrastructure.auth import SESSION_COOKIE, SESSION_MAX_AGE, verify_password
from ..infrastructure.backup import MAX_BACKUP_BYTES, create_backup
from ..infrastructure.logs import log_files
from ..infrastructure.media import SVG_CSP, font_face_css

log = logging.getLogger("ess.web")
HERE = Path(__file__).parent

PUBLIC_PATHS = ("/login", "/setup", "/static/", "/api/auth/", "/api/health")
# Reachable with the kiosk token (render pages in the kiosk browser) or a session.
KIOSK_PATHS = ("/render/", "/api/render/", "/media/", "/api/fonts.css", "/ndi/")
# Reachable with the kiosk token only (kiosk agent).
KIOSK_ONLY_PATHS = ("/api/kiosk/",)
KIOSK_HEADER = "x-ess-kiosk"
API_V1 = "/api/v1/"
CSRF_HEADER = "x-ess"
MAX_UPLOAD_BYTES = 26 * 2**20


def create_app(services: Services) -> Starlette:
    s = services
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
    vendor_ok = (HERE / "static" / "vendor" / "bootstrap" / "bootstrap.min.css").exists()

    env.globals["_"] = lambda text, **params: t(Msg(text, **params)) if params else t(text)

    def render(request: Request, template: str, **ctx: Any) -> HTMLResponse:
        lang = current_language()
        html = env.get_template(template).render(
            request=request, user=request.scope.get("user"), version=__version__, vendor_ok=vendor_ok,
            active=ctx.pop("active", ""), lang=lang, languages=LANGUAGES, js_catalog=catalog_for(lang),
            title=t(ctx.pop("title", "")), hostname=socket.gethostname(), **ctx,
        )
        return HTMLResponse(html)

    # ------------------------------------------------------------------ auth middleware

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
            path = request.url.path
            set_language(s.settings.get("ui.language", "en"))
            user = s.sessions.verify(request.cookies.get(SESSION_COOKIE))
            request.scope["user"] = user
            if path.startswith(PUBLIC_PATHS):
                return await call_next(request)
            kiosk = _kiosk_authed(request)
            request.scope["kiosk"] = kiosk
            if path.startswith(KIOSK_ONLY_PATHS):
                if not kiosk:
                    return JSONResponse({"error": t("Not logged in")}, status_code=401)
                return await call_next(request)
            if kiosk and path.startswith(KIOSK_PATHS) and request.method == "GET":
                return await call_next(request)
            if path.startswith(API_V1):
                api_key = _api_key_of(request)
                if api_key is not None:
                    ip = request.client.host if request.client else "?"
                    now = asyncio.get_running_loop().time()
                    recent = [ts for ts in key_failures.get(ip, []) if now - ts < 300]
                    if len(recent) >= 20:
                        return JSONResponse({"ok": False, "error": t("Too many failed attempts – please wait 5 minutes")}, status_code=429)
                    if verify_api_key(s, api_key):
                        key_failures.pop(ip, None)
                        request.scope["api_key"] = True
                        return await call_next(request)
                    key_failures[ip] = recent + [now]
                    log.warning("Rejected API key from %s", ip)
                    return JSONResponse({"ok": False, "error": t("Invalid API key")}, status_code=401)
                if not user:
                    return JSONResponse({"ok": False, "error": t("API key required")}, status_code=401)
                # with a session (trying the API in the browser) GET requests must not change anything
                if request.method == "GET" and _is_action(path):
                    return JSONResponse({"ok": False, "error": t("Actions via GET need an API key; use POST")}, status_code=403)
            if s.user_repo.count() == 0:
                return _deny(request, "/setup")
            if not user:
                return _deny(request, "/login?next=" + path)
            if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.headers.get(CSRF_HEADER) != "1":
                return JSONResponse({"error": t("Missing request header (CSRF protection)")}, status_code=403)
            return await call_next(request)

    key_failures: dict[str, list[float]] = {}

    def _api_key_of(request: Request) -> str | None:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return request.headers.get("x-api-key") or request.query_params.get("key")

    def _is_action(path: str) -> bool:
        return path.rstrip("/") != "/api/v1/status"

    def _kiosk_authed(request: Request) -> bool:
        token = request.headers.get(KIOSK_HEADER) or request.query_params.get("k") or ""
        return bool(token) and secrets.compare_digest(token, s.kiosk_token)

    def _deny(request: Request, location: str) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": t("Not logged in"), "redirect": location}, status_code=401)
        return RedirectResponse(location, status_code=303)

    # ------------------------------------------------------------------ helpers

    async def body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except (json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def api(fn: Callable[..., Awaitable[Any]]) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            try:
                result = await fn(request)
            except DomainError as exc:
                return JSONResponse({"error": t(text_of(exc))}, status_code=exc.status)
            except (KeyError, TypeError, ValueError) as exc:
                log.debug("bad request", exc_info=True)
                return JSONResponse({"error": t(Msg("Invalid request: {error}", error=str(exc)))}, status_code=400)
            if isinstance(result, Response):
                return result
            return JSONResponse({"ok": True, "result": result})
        return endpoint

    def command(factory: Callable[[Request, dict[str, Any]], Any]) -> Callable[[Request], Awaitable[Response]]:
        async def run(request: Request) -> Any:
            data = await body(request)
            return await s.commands.dispatch(factory(request, data))
        return api(run)

    def query(factory: Callable[[Request], Any]) -> Callable[[Request], Awaitable[Response]]:
        async def run(request: Request) -> Any:
            return await s.queries.dispatch(factory(request))
        return api(run)

    def p(request: Request, name: str) -> str:
        return request.path_params[name]

    # ------------------------------------------------------------------ pages

    def page(template: str, active: str, title: str) -> Callable[[Request], Awaitable[Response]]:
        async def handler(request: Request) -> Response:
            return render(request, template, active=active, title=title)
        return handler

    async def page_login(request: Request) -> Response:
        if s.user_repo.count() == 0:
            return RedirectResponse("/setup", status_code=303)
        return render(request, "login.html", title="Log in")

    async def page_setup(request: Request) -> Response:
        if s.user_repo.count() > 0:
            return RedirectResponse("/login", status_code=303)
        return render(request, "setup.html", title="Setup")

    # ------------------------------------------------------------------ auth API

    def _session_response(username: str) -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.set_cookie(SESSION_COOKIE, s.sessions.issue(username), max_age=SESSION_MAX_AGE, httponly=True, samesite="strict")
        return resp

    login_failures: dict[str, list[float]] = {}

    async def api_login(request: Request) -> Response:
        data = await body(request)
        ip = request.client.host if request.client else "?"
        now = asyncio.get_running_loop().time()
        recent = [ts for ts in login_failures.get(ip, []) if now - ts < 300]
        if len(recent) >= 10:
            return JSONResponse({"error": t("Too many failed attempts – please wait 5 minutes")}, status_code=429)
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        ok = await asyncio.to_thread(verify_password, password, s.user_repo.get_hash(username))
        if not ok:
            recent.append(now)
            login_failures[ip] = recent
            log.warning('Failed login for "%s" from %s', username, ip)
            return JSONResponse({"error": t("Wrong user name or password")}, status_code=401)
        login_failures.pop(ip, None)
        log.info("Login: %s (%s)", username, ip)
        return _session_response(username)

    async def api_setup(request: Request) -> Response:
        data = await body(request)
        try:
            await s.commands.dispatch(c.CreateAdmin(str(data.get("username") or ""), str(data.get("password") or "")))
        except DomainError as exc:
            return JSONResponse({"error": t(text_of(exc))}, status_code=exc.status)
        return _session_response(str(data.get("username")).strip())

    async def api_logout(request: Request) -> Response:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    async def api_health(request: Request) -> Response:
        return JSONResponse({"ok": True, "version": __version__})

    # ------------------------------------------------------------------ live stream (SSE)

    async def api_stream(request: Request) -> Response:
        queue = s.events.subscribe()

        async def gen():  # noqa: ANN202
            try:
                yield "retry: 2000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        yield await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                s.events.unsubscribe(queue)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ logs download

    async def api_log_download(request: Request) -> Response:
        files = log_files(s.config.log_dir)
        if not files:
            return PlainTextResponse(t("No log file yet."))
        name = request.query_params.get("file") or files[0].name
        match = next((f for f in files if f.name == name), None)
        if match is None:
            return PlainTextResponse(t("File not found"), status_code=404)
        return FileResponse(match, media_type="text/plain; charset=utf-8", filename=match.name)

    async def api_log_files(request: Request) -> Response:
        return JSONResponse({"files": [{"name": f.name, "size": f.stat().st_size} for f in log_files(s.config.log_dir)]})

    # ------------------------------------------------------------------ backup

    async def api_backup_download(request: Request) -> Response:
        data = await asyncio.to_thread(create_backup, s.config, s.db)
        name = f"event-stage-screen-{socket.gethostname()}-{time.strftime('%Y%m%d-%H%M')}.zip"
        log.info("Backup downloaded (%d kB)", len(data) // 1024)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def api_backup_file(request: Request) -> Response:
        name = request.path_params["name"]
        path = s.config.backup_dir / name
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.zip", name) or not path.is_file():
            return PlainTextResponse(t("File not found"), status_code=404)
        return FileResponse(path, media_type="application/zip", filename=name)

    async def api_backup_restore(request: Request) -> Response:
        if int(request.headers.get("content-length") or 0) > MAX_BACKUP_BYTES:
            return JSONResponse({"error": t("The backup file is too large")}, status_code=413)
        data = await request.body()

        async def run(_: Request) -> Any:
            return await s.commands.dispatch(c.RestoreBackup(data))
        return await api(run)(request)

    # ------------------------------------------------------------------ scenes, media, render, kiosk

    async def page_render(request: Request) -> Response:
        output_id = int(request.path_params["n"])
        html = env.get_template("render.html").render(output_id=output_id, version=__version__)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def api_scene_export(request: Request) -> Response:
        include = request.query_params.get("media") == "1"

        async def run(_: Request) -> Any:
            data = await s.queries.dispatch(q.ExportScene(p(request, "id"), include))
            safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in data["name"])[:40] or "scene"
            return Response(json.dumps(data, indent=1), media_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="{safe}.scene.json"'})
        return await api(run)(request)

    async def api_media_upload(request: Request) -> Response:
        if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES:
            return JSONResponse({"error": t("The file is too large")}, status_code=413)
        data = await request.body()
        name = unquote(request.headers.get("x-filename") or "file")
        family = unquote(request.headers.get("x-font-family") or "") or None

        async def run(_: Request) -> Any:
            return await s.commands.dispatch(c.UploadMedia(data, name, family))
        return await api(run)(request)

    async def media_file(request: Request) -> Response:
        item = s.media_repo.get(request.path_params["id"])
        if item is None:
            return PlainTextResponse(t("File not found"), status_code=404)
        headers = {"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"}
        if item["mime"] == "image/svg+xml":
            headers["Content-Security-Policy"] = SVG_CSP
        return FileResponse(s.media_store.path(item["filename"]), media_type=item["mime"], headers=headers)

    async def fonts_css(request: Request) -> Response:
        token = f"?k={s.kiosk_token}" if request.scope.get("kiosk") else ""
        css = font_face_css(s.media_repo.list("font"), HERE / "static" / "vendor" / "fonts",
                            lambda media_id: f"/media/{media_id}{token}")
        return Response(css, media_type="text/css", headers={"Cache-Control": "no-cache"})

    async def api_render_stream(request: Request) -> Response:
        queue = s.events.subscribe()

        async def gen():  # noqa: ANN202
            try:
                yield "retry: 2000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if payload.startswith("event: changed") and '"topic":"drafts"' not in payload:
                        yield payload
            finally:
                s.events.unsubscribe(queue)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def _ndi_hub(request: Request):
        from ..infrastructure.ndi import StreamFormat

        qp = request.query_params
        fmt = StreamFormat.from_params(qp.get("res"), qp.get("fps"), qp.get("q"))
        return s.ndi.hub(qp.get("source") or "", fmt)

    async def ndi_stream(request: Request) -> Response:
        try:
            hub = _ndi_hub(request)
        except DomainError as exc:
            return JSONResponse({"error": t(text_of(exc))}, status_code=exc.status)
        return StreamingResponse(hub.frames(), media_type="application/octet-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def ndi_snapshot(request: Request) -> Response:
        try:
            hub = _ndi_hub(request)
        except DomainError as exc:
            return JSONResponse({"error": t(text_of(exc))}, status_code=exc.status)
        hub.lease(10)
        for _ in range(30):  # wait up to 3 s for the first frame
            if hub.jpeg:
                break
            await asyncio.sleep(0.1)
        if not hub.jpeg:
            return JSONResponse({"error": t(hub.error or "No signal")}, status_code=503)
        age = time.time() - hub.last_frame_at
        return Response(hub.jpeg, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store", "X-Frame-Age": f"{age:.1f}"})

    async def api_thumbnail(request: Request) -> Response:
        async def run(_: Request) -> Any:
            item = await s.queries.dispatch(q.GetThumbnail(int(request.path_params["id"])))
            if item is None:
                return JSONResponse({"error": t("No thumbnail yet")}, status_code=404)
            mime = "image/png" if item["data"].startswith(b"\x89PNG") else "image/jpeg"
            return Response(item["data"], media_type=mime,
                            headers={"Cache-Control": "no-store", "X-Thumbnail-Age": f"{item['age']:.1f}"})
        return await api(run)(request)

    async def api_kiosk_thumbnail(request: Request) -> Response:
        data = await request.body()

        async def run(_: Request) -> Any:
            return await s.commands.dispatch(c.StoreThumbnail(int(request.path_params["n"]), data))
        return await api(run)(request)

    # ------------------------------------------------------------------ REST API v1 (remote control)

    async def v1_params(request: Request) -> dict[str, Any]:
        """JSON body for POST, query parameters for GET (key-authenticated)."""
        if request.method == "GET":
            return dict(request.query_params)
        return await body(request)

    def _bool(value: Any) -> bool | None:
        if value is None or value == "" or value == "toggle":
            return None
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("1", "true", "on", "yes")

    def v1(factory: Callable[[Request, dict[str, Any]], Any]) -> Callable[[Request], Awaitable[Response]]:
        async def run(request: Request) -> Any:
            data = await v1_params(request)
            return await s.commands.dispatch(factory(request, data))
        return api(run)

    def n_of(request: Request) -> int:
        return int(request.path_params["n"])

    async def api_kiosk_report(request: Request) -> Response:
        data = await body(request)
        await s.commands.dispatch(c.ReportKioskState(data))
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------ routes

    def _limit(value: Any) -> int | None:
        return None if value in (None, "") else int(value)

    _opt_int = _limit

    routes = [
        Route("/", page("dashboard.html", "dashboard", "Dashboard")),
        Route("/outputs", page("outputs.html", "outputs", "Outputs")),
        Route("/network", page("network.html", "network", "Network")),
        Route("/system", page("system.html", "system", "System")),
        Route("/logs", page("logs.html", "logs", "Logs")),
        Route("/login", page_login),
        Route("/setup", page_setup),

        Route("/api/health", api_health),
        Route("/api/auth/login", api_login, methods=["POST"]),
        Route("/api/auth/setup", api_setup, methods=["POST"]),
        Route("/api/auth/logout", api_logout, methods=["POST"]),
        Route("/api/stream", api_stream),

        # queries
        Route("/api/dashboard", query(lambda r: q.GetDashboard())),
        Route("/api/status", query(lambda r: q.GetStatus())),
        Route("/api/outputs", query(lambda r: q.GetOutputs())),
        Route("/api/network", query(lambda r: q.GetNetwork())),
        Route("/api/network/wifi", query(lambda r: q.GetWifiNetworks())),
        Route("/api/system", query(lambda r: q.GetSystemInfo())),
        Route("/api/system/time", query(lambda r: q.GetTimeSettings())),
        Route("/api/logs", query(lambda r: q.GetLogs(int(r.query_params.get("after", 0)), r.query_params.get("level", "DEBUG"),
                                                     r.query_params.get("text", ""), min(2000, int(r.query_params.get("limit", 500)))))),
        Route("/api/logs/files", api_log_files),
        Route("/api/logs/download", api_log_download),
        Route("/api/backup", api_backup_download),

        # commands
        Route("/api/outputs/{id:int}", command(lambda r, d: c.SaveOutput(p(r, "id"), d)), methods=["POST"]),
        Route("/api/network/ipv4", command(lambda r, d: c.ApplyIpv4(str(d["device"]), dict(d.get("ipv4") or {}))), methods=["POST"]),
        Route("/api/network/wifi", command(lambda r, d: c.ConnectWifi(str(d["device"]), str(d.get("ssid") or ""), str(d.get("password") or ""))), methods=["POST"]),
        Route("/api/network/confirm", command(lambda r, d: c.ConfirmNetworkChange()), methods=["POST"]),
        Route("/api/network/rollback", command(lambda r, d: c.RollbackNetworkChange()), methods=["POST"]),
        Route("/api/system/hostname", command(lambda r, d: c.SetHostname(str(d.get("hostname") or ""))), methods=["POST"]),
        Route("/api/system/timezone", command(lambda r, d: c.SetTimezone(str(d.get("timezone") or ""))), methods=["POST"]),
        Route("/api/system/ntp", command(lambda r, d: c.SetNtpServers([str(x) for x in d.get("servers") or []])), methods=["POST"]),
        Route("/api/system/ndi-limit", command(lambda r, d: c.SetNdiLimit(_limit(d.get("limit")))), methods=["POST"]),
        Route("/api/system/reboot", command(lambda r, d: c.Reboot()), methods=["POST"]),
        Route("/api/system/shutdown", command(lambda r, d: c.Shutdown()), methods=["POST"]),
        Route("/api/backup/restore", api_backup_restore, methods=["POST"]),
        Route("/api/ui/language", command(lambda r, d: c.SetLanguage(str(d.get("language", "")))), methods=["POST"]),
        Route("/api/account/password", command(lambda r, d: c.ChangePassword(r.scope["user"], d.get("current_password", ""), d.get("new_password", ""))), methods=["POST"]),

        # scenes
        Route("/scenes", page("scenes.html", "scenes", "Scenes")),
        Route("/media", page("media.html", "media", "Media")),
        Route("/render/{n:int}", page_render),
        Route("/api/scenes", query(lambda r: q.ListScenes())),
        Route("/api/scenes", command(lambda r, d: c.CreateScene(int(d["output_id"]), str(d.get("name") or ""),
                                                                 _opt_int(d.get("width")), _opt_int(d.get("height")))), methods=["POST"]),
        Route("/api/scenes/reorder", command(lambda r, d: c.ReorderScenes(int(d["output_id"]), list(d["scene_ids"]))), methods=["POST"]),
        Route("/api/scenes/import", command(lambda r, d: c.ImportScene(int(d["output_id"]), dict(d["data"]))), methods=["POST"]),
        Route("/api/scenes/{id}", query(lambda r: q.GetScene(p(r, "id")))),
        Route("/api/scenes/{id}/export", api_scene_export),
        Route("/api/scenes/{id}/draft", command(lambda r, d: c.SaveSceneDraft(p(r, "id"), dict(d["content"]))), methods=["POST"]),
        Route("/api/scenes/{id}/publish", command(lambda r, d: c.PublishScene(p(r, "id"), d.get("content"))), methods=["POST"]),
        Route("/api/scenes/{id}/discard", command(lambda r, d: c.DiscardSceneDraft(p(r, "id"))), methods=["POST"]),
        Route("/api/scenes/{id}/rename", command(lambda r, d: c.RenameScene(p(r, "id"), str(d.get("name") or ""))), methods=["POST"]),
        Route("/api/scenes/{id}/duplicate", command(lambda r, d: c.DuplicateScene(p(r, "id"), _opt_int(d.get("output_id")))), methods=["POST"]),
        Route("/api/scenes/{id}/delete", command(lambda r, d: c.DeleteScene(p(r, "id"))), methods=["POST"]),
        Route("/api/outputs/{id:int}/activate", command(lambda r, d: c.ActivateScene(p(r, "id"), d.get("scene_id"))), methods=["POST"]),
        Route("/api/splash", command(lambda r, d: c.SaveSplash(str(d.get("mode") or "info"), d.get("logo") or None)), methods=["POST"]),

        # media
        Route("/api/media", query(lambda r: q.ListMedia())),
        Route("/api/media", api_media_upload, methods=["POST"]),
        Route("/api/media/{id}/rename", command(lambda r, d: c.RenameMedia(p(r, "id"), str(d.get("name") or ""), d.get("family"))), methods=["POST"]),
        Route("/api/media/{id}/delete", command(lambda r, d: c.DeleteMedia(p(r, "id"))), methods=["POST"]),
        Route("/media/{id}", media_file),
        Route("/api/fonts", query(lambda r: q.GetFonts())),
        Route("/api/fonts.css", fonts_css),

        # eventrentmanager
        Route("/erm", page("erm.html", "erm", "eventrentmanager")),
        Route("/api/erm", query(lambda r: q.GetErmStatus())),
        Route("/api/erm/settings", command(lambda r, d: c.SaveErmSettings(str(d.get("base_url") or ""), bool(d.get("verify_tls", True)))), methods=["POST"]),
        Route("/api/erm/outputs/{id:int}", command(lambda r, d: c.SetErmToken(p(r, "id"), d.get("token"), str(d.get("source") or "own"))), methods=["POST"]),
        Route("/api/erm/test", command(lambda r, d: c.TestErmConnection(str(d.get("base_url") or ""), bool(d.get("verify_tls", True)),
                                                                        d.get("token") or None, _opt_int(d.get("output_id")))), methods=["POST"]),
        Route("/api/render/{n:int}/erm", query(lambda r: q.GetRenderErm(p(r, "n")))),

        # NDI
        Route("/ndi", page("ndi.html", "ndi", "NDI")),
        Route("/api/ndi", query(lambda r: q.GetNdiStatus())),
        Route("/api/ndi/sources", query(lambda r: q.GetNdiSources())),
        Route("/api/ndi/settings", command(lambda r, d: c.SetNdiDiscoveryServers([str(x) for x in d.get("discovery_servers") or []])), methods=["POST"]),
        Route("/ndi/stream", ndi_stream),
        Route("/ndi/snapshot", ndi_snapshot),

        # render pages and kiosk agent
        Route("/api/render/stream", api_render_stream),
        Route("/api/render/{n:int}/state", query(lambda r: q.GetRenderState(p(r, "n")))),
        Route("/api/kiosk/config", query(lambda r: q.GetKioskConfig())),
        Route("/api/kiosk/report", api_kiosk_report, methods=["POST"]),
        Route("/api/kiosk/thumbnail/{n:int}", api_kiosk_thumbnail, methods=["POST"]),

        # automation
        Route("/automation", page("automation.html", "automation", "Automation")),
        Route("/api/automation", query(lambda r: q.GetAutomation())),
        Route("/api/automation/{id:int}", command(lambda r, d: c.SaveAutomation(p(r, "id"), bool(d.get("enabled")), list(d.get("rules") or []))), methods=["POST"]),
        Route("/api/outputs/{id:int}/automatic", command(lambda r, d: c.ResumeAutomation(p(r, "id"))), methods=["POST"]),

        # REST API v1 (API key, see docs/api.md)
        Route("/api/v1/status", query(lambda r: q.GetRemoteStatus())),
        Route("/api/v1/outputs/{n:int}/scene", v1(lambda r, d: c.ActivateSceneRef(n_of(r), str(d.get("scene") or ""))), methods=["GET", "POST"]),
        Route("/api/v1/outputs/{n:int}/scene/next", v1(lambda r, d: c.CycleScene(n_of(r), 1)), methods=["GET", "POST"]),
        Route("/api/v1/outputs/{n:int}/scene/previous", v1(lambda r, d: c.CycleScene(n_of(r), -1)), methods=["GET", "POST"]),
        Route("/api/v1/outputs/{n:int}/automatic", v1(lambda r, d: c.ResumeAutomation(n_of(r))), methods=["GET", "POST"]),
        Route("/api/v1/outputs/{n:int}/blackout", v1(lambda r, d: c.SetBlackout(n_of(r), _bool(d.get("on")))), methods=["GET", "POST"]),
        Route("/api/v1/blackout", v1(lambda r, d: c.SetBlackout(None, _bool(d.get("on")))), methods=["GET", "POST"]),
        Route("/api/v1/outputs/{n:int}/testpattern", v1(lambda r, d: c.SetTestPattern(n_of(r), d.get("pattern") or None)), methods=["GET", "POST"]),
        Route("/api/v1/outputs/{n:int}/identify", v1(lambda r, d: c.IdentifyOutputs(n_of(r), float(d.get("seconds") or 6))), methods=["GET", "POST"]),
        Route("/api/v1/identify", v1(lambda r, d: c.IdentifyOutputs(None, float(d.get("seconds") or 6))), methods=["GET", "POST"]),
        Route("/api/v1/outputs/{n:int}/elements/{element}/visible",
              v1(lambda r, d: c.SetElementVisibility(n_of(r), r.path_params["element"], _bool(d.get("visible")))), methods=["GET", "POST"]),

        # updates and backups
        Route("/api/updates", query(lambda r: q.CheckForUpdates())),
        Route("/api/updates/install", command(lambda r, d: c.InstallUpdate(str(d.get("tag") or ""))), methods=["POST"]),
        Route("/api/backups", query(lambda r: q.ListBackups())),
        Route("/api/backups/{name}", api_backup_file),

        # API keys (admin)
        Route("/api/apikeys", query(lambda r: q.ListApiKeys())),
        Route("/api/apikeys", command(lambda r, d: c.CreateApiKey(str(d.get("name") or ""))), methods=["POST"]),
        Route("/api/apikeys/{id}/delete", command(lambda r, d: c.DeleteApiKey(p(r, "id"))), methods=["POST"]),

        # setup aids
        Route("/api/outputs/{id:int}/thumbnail", api_thumbnail),
        Route("/api/outputs/{id:int}/blackout", command(lambda r, d: c.SetBlackout(p(r, "id"), d.get("on"))), methods=["POST"]),
        Route("/api/outputs/blackout", command(lambda r, d: c.SetBlackout(None, d.get("on"))), methods=["POST"]),
        Route("/api/outputs/{id:int}/testpattern", command(lambda r, d: c.SetTestPattern(p(r, "id"), d.get("pattern") or None)), methods=["POST"]),
        Route("/api/outputs/{id:int}/identify", command(lambda r, d: c.IdentifyOutputs(p(r, "id"), float(d.get("seconds") or 6))), methods=["POST"]),
        Route("/api/outputs/identify", command(lambda r, d: c.IdentifyOutputs(None, float(d.get("seconds") or 6))), methods=["POST"]),

        Mount("/static", app=StaticFiles(directory=HERE / "static"), name="static"),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):  # noqa: ANN202
        await s.start()
        try:
            yield
        finally:
            await s.shutdown()

    return Starlette(routes=routes, middleware=[Middleware(AuthMiddleware)], lifespan=lifespan)
