"""Shared GStreamer runtime helpers: GLib main loop thread and timestamp burn-in."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from .media import gst_init

log = logging.getLogger("spike.runtime")

_loop_lock = threading.Lock()
_loop_started = False


def ensure_glib_loop() -> None:
    """Run a GLib main loop in a daemon thread (needed for bus/signal dispatch)."""
    global _loop_started
    with _loop_lock:
        if _loop_started:
            return
        gst_init()
        from gi.repository import GLib  # type: ignore

        loop = GLib.MainLoop()
        threading.Thread(target=loop.run, name="glib-loop", daemon=True).start()
        _loop_started = True


def wall_clock_text(ts: float | None = None) -> str:
    """Format a wall-clock time as ``HH:MM:SS.mmm`` (same format as the output page)."""
    ts = time.time() if ts is None else ts
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


def attach_timestamp_overlay(pipeline) -> None:
    """Update the ``stamp`` textoverlay with the wall clock for every frame."""
    Gst = gst_init()
    stamp = pipeline.get_by_name("stamp")
    if stamp is None:
        return
    pad = stamp.get_static_pad("video_sink")

    def probe(_pad, _info):
        stamp.set_property("text", wall_clock_text())
        return Gst.PadProbeReturn.OK

    pad.add_probe(Gst.PadProbeType.BUFFER, probe)


def watch_bus(pipeline, name: str, on_error=None) -> None:
    """Log errors/warnings from a pipeline bus."""
    ensure_glib_loop()
    Gst = gst_init()
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_message(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log.error("[%s] %s (%s)", name, err.message, dbg)
            if on_error:
                on_error(err.message)
        elif msg.type == Gst.MessageType.WARNING:
            err, dbg = msg.parse_warning()
            log.warning("[%s] %s", name, err.message)

    bus.connect("message", on_message)
