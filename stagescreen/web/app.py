"""Web application: HTML pages, JSON API (commands/queries) and the SSE live stream."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

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
from ..container import Services
from ..i18n import LANGUAGES, Msg, catalog_for, current_language, set_language, t, text_of
from ..infrastructure.auth import SESSION_COOKIE, SESSION_MAX_AGE, verify_password
from ..infrastructure.backup import MAX_BACKUP_BYTES, create_backup
from ..infrastructure.logs import log_files

log = logging.getLogger("ess.web")
HERE = Path(__file__).parent

PUBLIC_PATHS = ("/login", "/setup", "/static/", "/api/auth/", "/api/health")
CSRF_HEADER = "x-ess"


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
            if s.user_repo.count() == 0:
                return _deny(request, "/setup")
            if not user:
                return _deny(request, "/login?next=" + path)
            if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.headers.get(CSRF_HEADER) != "1":
                return JSONResponse({"error": t("Missing request header (CSRF protection)")}, status_code=403)
            return await call_next(request)

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

    async def api_backup_restore(request: Request) -> Response:
        if int(request.headers.get("content-length") or 0) > MAX_BACKUP_BYTES:
            return JSONResponse({"error": t("The backup file is too large")}, status_code=413)
        data = await request.body()

        async def run(_: Request) -> Any:
            return await s.commands.dispatch(c.RestoreBackup(data))
        return await api(run)(request)

    # ------------------------------------------------------------------ routes

    def _limit(value: Any) -> int | None:
        return None if value in (None, "") else int(value)

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
