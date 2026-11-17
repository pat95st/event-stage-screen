"""Starlette app serving the output pages, video transports and metrics API."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import media
from .metrics import Recorder, sample_system

log = logging.getLogger("spike.server")
STATIC = Path(__file__).parent / "static"


@dataclass
class Settings:
    data_dir: Path = Path(os.environ.get("SPIKE_DATA_DIR", "data"))
    scenario: str = os.environ.get("SPIKE_SCENARIO", "manual")
    with_gstreamer: bool = os.environ.get("SPIKE_NO_GST") != "1"
    hubs: dict = field(default_factory=dict)


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or Settings()
    recorder = Recorder(settings.data_dir, settings.scenario)

    async def index(_request: Request):
        return FileResponse(STATIC / "index.html")

    async def output(_request: Request):
        return FileResponse(STATIC / "output.html", headers={"Cache-Control": "no-store"})

    async def sources(_request: Request):
        names = await asyncio.to_thread(media.discover_ndi_sources) if settings.with_gstreamer else []
        return JSONResponse({"ndi": media.ndi_available() if settings.with_gstreamer else False,
                             "sources": [media.TEST_SOURCE, *names]})

    async def environment(_request: Request):
        info = media.describe_environment() if settings.with_gstreamer else {}
        return JSONResponse(info)

    async def post_metrics(request: Request):
        data = await request.json()
        recorder.write("browser", data)
        return JSONResponse({"ok": True})

    async def mjpeg(request: Request):
        from .mjpeg import MjpegHub

        source = request.query_params.get("source", media.TEST_SOURCE)
        fmt = media.VideoFormat.parse(request.query_params.get("format"))
        quality = int(request.query_params.get("quality", "80"))
        key = (source, fmt, quality)
        hub = settings.hubs.get(key)
        if hub is None:
            hub = settings.hubs[key] = MjpegHub(source, fmt, quality)
        return StreamingResponse(hub.frames(), media_type="application/octet-stream",
                                 headers={"Cache-Control": "no-store"})

    async def webrtc_ws(ws: WebSocket):
        from .webrtc import WebRtcSession

        await ws.accept()
        source = ws.query_params.get("source", media.TEST_SOURCE)
        fmt = media.VideoFormat.parse(ws.query_params.get("format"))
        codec = ws.query_params.get("codec", "h264")
        session = WebRtcSession(source, fmt, codec)

        async def pump():
            while True:
                await ws.send_text(await session.outbox.get())

        sender = asyncio.create_task(pump())
        try:
            session.start()
            while True:
                session.handle(await ws.receive_text())
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            session.stop()

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        recorder.write_environment(media.describe_environment() if settings.with_gstreamer else {})
        task = asyncio.create_task(sample_system(recorder))
        try:
            yield
        finally:
            task.cancel()

    routes = [
        Route("/", index),
        Route("/output/{n:int}", output),
        Route("/api/sources", sources),
        Route("/api/environment", environment),
        Route("/api/metrics", post_metrics, methods=["POST"]),
        Route("/stream/mjpeg", mjpeg),
        WebSocketRoute("/ws/webrtc", webrtc_ws),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    return Starlette(routes=routes, lifespan=lifespan)
