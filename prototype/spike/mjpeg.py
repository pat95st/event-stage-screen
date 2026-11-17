"""Shared MJPEG hub: one GStreamer pipeline per (source, format), many viewers.

Wire format of ``/stream/mjpeg`` (a chunked HTTP response), repeated per frame:

    uint32 big-endian  payload length (JPEG bytes)
    float64 big-endian capture wall-clock time in milliseconds since epoch
    bytes              JPEG image

The capture time is taken when the raw frame leaves the source, so the
browser can compute capture-to-paint latency against ``Date.now()`` (both run
on the same machine and share the clock).
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time

from .media import VideoFormat, gst_init, mjpeg_description
from .runtime import attach_timestamp_overlay, ensure_glib_loop, watch_bus

log = logging.getLogger("spike.mjpeg")

HEADER = struct.Struct(">Id")


def pack_frame(jpeg: bytes, capture_ms: float) -> bytes:
    return HEADER.pack(len(jpeg), capture_ms) + jpeg


def unpack_frames(buffer: bytes) -> tuple[list[tuple[float, bytes]], bytes]:
    """Split a byte buffer into complete frames; returns (frames, remainder)."""
    frames = []
    while len(buffer) >= HEADER.size:
        length, ts = HEADER.unpack_from(buffer)
        end = HEADER.size + length
        if len(buffer) < end:
            break
        frames.append((ts, buffer[HEADER.size:end]))
        buffer = buffer[end:]
    return frames, buffer


class MjpegHub:
    def __init__(self, source: str, fmt: VideoFormat, quality: int = 80) -> None:
        self.source = source
        self.fmt = fmt
        self.quality = quality
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cond = asyncio.Condition()
        self._frame: bytes | None = None
        self._seq = 0
        self._viewers = 0
        self._capture_times: dict[int, float] = {}
        self.pipeline = None

    # -- pipeline -------------------------------------------------------
    def _start(self) -> None:
        ensure_glib_loop()
        Gst = gst_init()
        desc = mjpeg_description(self.source, self.fmt, self.quality)
        log.info("starting MJPEG pipeline: %s", desc)
        self.pipeline = Gst.parse_launch(desc)
        attach_timestamp_overlay(self.pipeline)
        watch_bus(self.pipeline, "mjpeg")
        # Remember capture times by PTS at the output of the source chain.
        sink = self.pipeline.get_by_name("sink")
        jpegenc_sink = sink.get_static_pad("sink").get_peer().get_parent_element().get_static_pad("sink")

        def probe(_pad, info):
            buf = info.get_buffer()
            self._capture_times[buf.pts] = time.time() * 1000.0
            if len(self._capture_times) > 64:
                for key in sorted(self._capture_times)[:-32]:
                    self._capture_times.pop(key, None)
            return Gst.PadProbeReturn.OK

        jpegenc_sink.add_probe(Gst.PadProbeType.BUFFER, probe)
        sink.connect("new-sample", self._on_sample)
        self.pipeline.set_state(Gst.State.PLAYING)

    def _stop(self) -> None:
        if self.pipeline is not None:
            Gst = gst_init()
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    def _on_sample(self, sink):
        Gst = gst_init()
        sample = sink.emit("pull-sample")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                data = bytes(info.data)
            finally:
                buf.unmap(info)
            ts = self._capture_times.pop(buf.pts, time.time() * 1000.0)
            if self._loop is not None:
                self._loop.call_soon_threadsafe(asyncio.ensure_future, self._publish(pack_frame(data, ts)))
        return Gst.FlowReturn.OK

    async def _publish(self, frame: bytes) -> None:
        async with self._cond:
            self._frame = frame
            self._seq += 1
            self._cond.notify_all()

    # -- viewers --------------------------------------------------------
    async def frames(self):
        """Async generator yielding packed frames; always the newest one."""
        self._loop = asyncio.get_running_loop()
        self._viewers += 1
        if self._viewers == 1:
            self._start()
        seen = self._seq
        try:
            while True:
                async with self._cond:
                    await self._cond.wait_for(lambda: self._seq != seen)
                    seen = self._seq
                    frame = self._frame
                yield frame
        finally:
            self._viewers -= 1
            if self._viewers == 0:
                self._stop()
