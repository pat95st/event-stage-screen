"""NDI input: GStreamer ``ndisrc`` → MJPEG stream for the browser renderer.

One :class:`MjpegHub` (GStreamer pipeline) exists per source and stream
format and is shared by all viewers. It starts with the first viewer (or
snapshot lease), stops a few seconds after the last one, and restarts with
back-off when the source disappears.

Wire format of ``/ndi/stream`` (chunked HTTP response), repeated per frame:

    uint32 big-endian  JPEG length
    float64 big-endian capture wall-clock time in milliseconds
    bytes              JPEG image

GStreamer (PyGObject) and the NDI plugin/runtime are optional: without them
the service reports NDI as unavailable and everything else keeps working.
Sources named ``test:<pattern>`` (videotestsrc) are allowed only when
``allow_test_sources`` is set (development and tests).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import struct
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..application.bus import DomainError
from ..i18n import Msg

log = logging.getLogger("ess.ndi")

HEADER = struct.Struct(">Id")
RESOLUTIONS = {"native": None, "1080": 1080, "720": 720, "540": 540, "360": 360}
TEST_PATTERNS = ("smpte", "ball", "snow", "bar", "pinwheel")
IDLE_STOP_S = 5.0
SOURCE_RE = re.compile(r"^[^\x00-\x1f\"]{1,200}$")


def pack_frame(jpeg: bytes, capture_ms: float) -> bytes:
    return HEADER.pack(len(jpeg), capture_ms) + jpeg


def unpack_frames(buffer: bytes) -> tuple[list[tuple[float, bytes]], bytes]:
    frames = []
    while len(buffer) >= HEADER.size:
        length, ts = HEADER.unpack_from(buffer)
        end = HEADER.size + length
        if len(buffer) < end:
            break
        frames.append((ts, buffer[HEADER.size:end]))
        buffer = buffer[end:]
    return frames, buffer


@dataclass(frozen=True)
class StreamFormat:
    resolution: str = "1080"
    fps: int = 30
    quality: int = 80

    @classmethod
    def from_params(cls, resolution: str | None, fps: Any, quality: Any) -> StreamFormat:
        res = resolution if resolution in RESOLUTIONS else "1080"
        try:
            fps_i = min(60, max(1, int(fps or 30)))
            q_i = min(100, max(10, int(quality or 80)))
        except (TypeError, ValueError):
            raise DomainError("Invalid stream parameters") from None
        return cls(res, fps_i, q_i)


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pipeline_description(source: str, fmt: StreamFormat) -> str:
    """GStreamer launch line ending in ``appsink name=sink``."""
    height = RESOLUTIONS[fmt.resolution]
    scale = f"videoscale ! video/x-raw,height={height},pixel-aspect-ratio=1/1 ! " if height else ""
    if source.startswith("test:"):
        pattern = source[5:] if source[5:] in TEST_PATTERNS else "smpte"
        src = (f"videotestsrc is-live=true pattern={pattern} ! video/x-raw,width=1920,height=1080,framerate=30/1 ! "
               "timeoverlay font-desc=\"Sans 48\" ! ")
    else:
        src = (f"ndisrc ndi-name={quote(source)} timeout=5000 ! ndisrcdemux name=demux "
               "demux.audio ! queue ! fakesink sync=false async=false "
               "demux.video ! ")
    return (
        f"{src}queue max-size-buffers=2 leaky=downstream ! videoconvert ! {scale}"
        f"videorate drop-only=true max-rate={fmt.fps} ! "
        f"jpegenc quality={fmt.quality} ! "
        "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
    )


# ---------------------------------------------------------------------- GStreamer access

_gst = None
_glib_loop_started = False
_gst_lock = threading.Lock()


def gst():
    """Return the initialised ``Gst`` module or None if PyGObject/GStreamer is missing."""
    global _gst, _glib_loop_started
    with _gst_lock:
        if _gst is None:
            try:
                import gi  # type: ignore

                gi.require_version("Gst", "1.0")
                from gi.repository import GLib, Gst  # type: ignore

                Gst.init(None)
                _gst = Gst
                if not _glib_loop_started:
                    threading.Thread(target=GLib.MainLoop().run, name="ess-glib", daemon=True).start()
                    _glib_loop_started = True
            except (ImportError, ValueError) as exc:
                log.info("GStreamer not available: %s", exc)
                _gst = False
        return _gst or None


def element_available(name: str) -> bool:
    Gst = gst()
    return bool(Gst and Gst.ElementFactory.find(name))


# ---------------------------------------------------------------------- hub

@dataclass
class MjpegHub:
    source: str
    fmt: StreamFormat
    loop: asyncio.AbstractEventLoop
    viewers: int = 0
    lease_until: float = 0.0
    frame: bytes | None = None       # packed (header + JPEG)
    jpeg: bytes | None = None        # latest JPEG for snapshots
    seq: int = 0
    last_frame_at: float = 0.0
    error: str | None = None
    restarts: int = 0
    fps: float = 0.0
    pipeline: Any = None
    _cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    _fps_window: list[float] = field(default_factory=list)
    _stop_handle: asyncio.TimerHandle | None = None
    _restart_handle: asyncio.TimerHandle | None = None
    _backoff: float = 2.0

    @property
    def running(self) -> bool:
        return self.pipeline is not None

    # -- pipeline (event loop thread) --------------------------------------
    def start(self) -> None:
        Gst = gst()
        if Gst is None or self.pipeline is not None:
            return
        desc = pipeline_description(self.source, self.fmt)
        try:
            pipeline = Gst.parse_launch(desc)
        except Exception as exc:  # GLib.Error
            self.error = str(exc)
            log.error("NDI pipeline for %s could not be created: %s", self.source, exc)
            return
        sink = pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        self.pipeline = pipeline
        pipeline.set_state(Gst.State.PLAYING)
        log.info("NDI stream started: %s (%s, %d fps)", self.source, self.fmt.resolution, self.fmt.fps)

    def stop(self) -> None:
        if self.pipeline is not None:
            Gst = gst()
            bus = self.pipeline.get_bus()
            bus.remove_signal_watch()
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            log.info("NDI stream stopped: %s", self.source)
        for handle in (self._stop_handle, self._restart_handle):
            if handle:
                handle.cancel()
        self._stop_handle = self._restart_handle = None

    def _schedule_restart(self, reason: str) -> None:
        self.error = reason
        if self.pipeline is not None:
            Gst = gst()
            self.pipeline.get_bus().remove_signal_watch()
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        if not self.wanted():
            return
        self.restarts += 1
        delay = self._backoff
        self._backoff = min(30.0, self._backoff * 2)
        log.warning("NDI source %s: %s – retrying in %.0f s", self.source, reason, delay)
        self._restart_handle = self.loop.call_later(delay, self._restart)

    def _restart(self) -> None:
        self._restart_handle = None
        if self.wanted():
            self.start()

    def wanted(self) -> bool:
        return self.viewers > 0 or time.time() < self.lease_until

    def check_idle(self) -> None:
        """Stop the pipeline once nobody needs it any more."""
        if self._stop_handle:
            self._stop_handle.cancel()

        def stop_if_idle() -> None:
            self._stop_handle = None
            if self.wanted():
                self.check_idle()
            else:
                self.stop()

        delay = max(IDLE_STOP_S, self.lease_until - time.time())
        self._stop_handle = self.loop.call_later(delay, stop_if_idle)

    # -- GStreamer callbacks (streaming / GLib threads) ---------------------
    def _on_sample(self, sink):
        Gst = gst()
        sample = sink.emit("pull-sample")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                data = bytes(info.data)
            finally:
                buf.unmap(info)
            self.loop.call_soon_threadsafe(self._publish, data, time.time() * 1000.0)
        return Gst.FlowReturn.OK

    def _on_message(self, _bus, msg) -> None:
        Gst = gst()
        if msg.type == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            self.loop.call_soon_threadsafe(self._schedule_restart, err.message)
        elif msg.type == Gst.MessageType.EOS:
            self.loop.call_soon_threadsafe(self._schedule_restart, "end of stream")

    def _publish(self, jpeg: bytes, capture_ms: float) -> None:
        now = time.time()
        self.jpeg = jpeg
        self.frame = pack_frame(jpeg, capture_ms)
        self.seq += 1
        self.last_frame_at = now
        self.error = None
        self._backoff = 2.0
        self._fps_window = [t for t in self._fps_window if now - t < 2.0] + [now]
        self.fps = round(len(self._fps_window) / 2.0, 1)
        asyncio.ensure_future(self._notify())

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    # -- consumers ----------------------------------------------------------
    async def frames(self) -> AsyncIterator[bytes]:
        self.viewers += 1
        self.start()
        seen = self.seq if self.frame is None else self.seq - 1  # send the latest frame immediately
        try:
            while True:
                last = seen
                async with self._cond:
                    await self._cond.wait_for(lambda last=last: self.seq != last)
                seen = self.seq
                if self.frame:
                    yield self.frame
        finally:
            self.viewers -= 1
            self.check_idle()

    def lease(self, seconds: float = 10.0) -> None:
        self.lease_until = max(self.lease_until, time.time() + seconds)
        self.start()
        self.check_idle()

    def status(self) -> dict[str, Any]:
        age = time.time() - self.last_frame_at if self.last_frame_at else None
        return {"source": self.source, "resolution": self.fmt.resolution, "fps_limit": self.fmt.fps,
                "quality": self.fmt.quality, "running": self.running, "viewers": self.viewers, "fps": self.fps,
                "last_frame_age_s": round(age, 1) if age is not None else None, "error": self.error,
                "restarts": self.restarts}


# ---------------------------------------------------------------------- service

class NdiService:
    def __init__(self, settings, limit: Callable[[], int], home_dir: Path, allow_test_sources: bool = False):
        self.settings = settings
        self.limit = limit
        self.home_dir = home_dir
        self.allow_test_sources = allow_test_sources
        self.hubs: dict[tuple[str, StreamFormat], MjpegHub] = {}
        self._sources_cache: tuple[float, list[str]] = (0.0, [])
        self._discover_lock = threading.Lock()

    # -- availability --------------------------------------------------------
    def capabilities(self) -> dict[str, bool]:
        return {"gstreamer": gst() is not None, "plugin": element_available("ndisrc"),
                "jpeg": element_available("jpegenc")}

    def available(self) -> bool:
        caps = self.capabilities()
        return caps["gstreamer"] and caps["jpeg"] and (caps["plugin"] or self.allow_test_sources)

    # -- settings ------------------------------------------------------------
    @property
    def discovery_servers(self) -> list[str]:
        return list(self.settings.get("ndi.discovery_servers", []) or [])

    def set_discovery_servers(self, servers: list[str]) -> list[str]:
        clean = []
        for s in servers:
            s = (s or "").strip()
            if not s:
                continue
            if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,255}", s):
                raise DomainError(Msg('Invalid discovery server "{server}"', server=s[:40]))
            clean.append(s)
        if len(clean) > 4:
            raise DomainError("At most 4 discovery servers")
        self.settings.set("ndi.discovery_servers", clean)
        self.write_ndi_config()
        for hub in self.hubs.values():  # receivers pick up the configuration when they are created
            if hub.running:
                hub.stop()
                hub.start()
        return clean

    def write_ndi_config(self) -> Path:
        """The NDI runtime reads discovery servers from ~/.ndi/ndi-config.v1.json."""
        path = self.home_dir / ".ndi" / "ndi-config.v1.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            config: dict[str, Any] = {"ndi": {"networks": {}}}
            if self.discovery_servers:
                config["ndi"]["networks"]["discovery"] = ",".join(self.discovery_servers)
            path.write_text(json.dumps(config, indent=2))
        except OSError as exc:
            log.warning("Could not write %s: %s", path, exc)
        return path

    # -- discovery -----------------------------------------------------------
    def discover(self, timeout_s: float = 2.0) -> list[str]:
        """NDI source names visible on the network (cached for 5 s). Blocking."""
        ts, cached = self._sources_cache
        if time.time() - ts < 5:
            return cached
        with self._discover_lock:
            names: list[str] = []
            Gst = gst()
            if Gst is not None and element_available("ndisrc"):
                monitor = Gst.DeviceMonitor.new()
                monitor.add_filter("Video/Source/Network", None)
                if monitor.start():
                    try:
                        time.sleep(timeout_s)
                        for dev in monitor.get_devices() or []:
                            props = dev.get_properties()
                            name = props.get_string("ndi-name") if props else None
                            names.append(name or dev.get_display_name())
                    finally:
                        monitor.stop()
            names = sorted(set(names))
            if self.allow_test_sources:
                names += [f"test:{p}" for p in TEST_PATTERNS]
            self._sources_cache = (time.time(), names)
            return names

    # -- streams -------------------------------------------------------------
    def validate_source(self, source: str) -> str:
        source = (source or "").strip()
        if not SOURCE_RE.fullmatch(source):
            raise DomainError("Invalid NDI source name")
        if source.startswith("test:") and not self.allow_test_sources:
            raise DomainError("Test sources are only available in development mode")
        return source

    def hub(self, source: str, fmt: StreamFormat) -> MjpegHub:
        if not self.available():
            raise DomainError("NDI is not installed on this device", 503)
        source = self.validate_source(source)
        key = (source, fmt)
        hub = self.hubs.get(key)
        if hub is None:
            active = {s for (s, _f), h in self.hubs.items() if h.wanted() or h.running}
            if source not in active and len(active) >= self.limit():
                raise DomainError(Msg("NDI source limit reached ({n} at the same time)", n=self.limit()), 429)
            hub = MjpegHub(source, fmt, asyncio.get_running_loop())
            self.hubs[key] = hub
        return hub

    def cleanup(self) -> None:
        for key, hub in list(self.hubs.items()):
            if not hub.running and not hub.wanted():
                del self.hubs[key]

    def status(self) -> dict[str, Any]:
        self.cleanup()
        return {**self.capabilities(), "available": self.available(), "limit": self.limit(),
                "discovery_servers": self.discovery_servers, "test_sources": self.allow_test_sources,
                "streams": [h.status() for h in self.hubs.values()]}

    def shutdown(self) -> None:
        for hub in self.hubs.values():
            hub.stop()
