"""Per-client WebRTC sender based on ``webrtcbin`` with WebSocket signalling.

Signalling messages (JSON, both directions):
    {"sdp": {"type": "offer"|"answer", "sdp": "..."}}
    {"ice": {"candidate": "...", "sdpMLineIndex": 0}}
    {"error": "..."}             (server -> browser only)
"""

from __future__ import annotations

import asyncio
import json
import logging

from .media import VideoFormat, gst_init, webrtc_description
from .runtime import attach_timestamp_overlay, ensure_glib_loop, watch_bus

log = logging.getLogger("spike.webrtc")


class WebRtcSession:
    def __init__(self, source: str, fmt: VideoFormat, codec: str = "h264") -> None:
        self.source = source
        self.fmt = fmt
        self.codec = codec
        self.outbox: asyncio.Queue[str] = asyncio.Queue()
        self._loop = asyncio.get_running_loop()
        self.pipeline = None
        self.webrtc = None

    # -- outgoing -------------------------------------------------------
    def _send(self, payload: dict) -> None:
        self._loop.call_soon_threadsafe(self.outbox.put_nowait, json.dumps(payload))

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        ensure_glib_loop()
        Gst = gst_init()
        desc = webrtc_description(self.source, self.fmt, self.codec)
        log.info("starting WebRTC pipeline: %s", desc)
        self.pipeline = Gst.parse_launch(desc)
        attach_timestamp_overlay(self.pipeline)
        watch_bus(self.pipeline, "webrtc", on_error=lambda m: self._send({"error": m}))
        self.webrtc = self.pipeline.get_by_name("webrtc")
        self.webrtc.connect("on-negotiation-needed", self._on_negotiation_needed)
        self.webrtc.connect("on-ice-candidate", self._on_ice_candidate)
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        if self.pipeline is not None:
            Gst = gst_init()
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    # -- webrtcbin callbacks (GStreamer threads) ------------------------
    def _on_negotiation_needed(self, element) -> None:
        Gst = gst_init()
        promise = Gst.Promise.new_with_change_func(self._on_offer_created, element, None)
        element.emit("create-offer", None, promise)

    def _on_offer_created(self, promise, element, _data) -> None:
        Gst = gst_init()
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value("offer")
        element.emit("set-local-description", offer, Gst.Promise.new())
        self._send({"sdp": {"type": "offer", "sdp": offer.sdp.as_text()}})

    def _on_ice_candidate(self, _element, mline: int, candidate: str) -> None:
        self._send({"ice": {"candidate": candidate, "sdpMLineIndex": mline}})

    # -- incoming -------------------------------------------------------
    def handle(self, message: str) -> None:
        Gst = gst_init()
        from gi.repository import GstSdp, GstWebRTC  # type: ignore

        msg = json.loads(message)
        if "sdp" in msg and msg["sdp"].get("type") == "answer":
            res, sdp = GstSdp.SDPMessage.new_from_text(msg["sdp"]["sdp"])
            if res != GstSdp.SDPResult.OK:
                raise ValueError("invalid SDP answer")
            answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdp)
            self.webrtc.emit("set-remote-description", answer, Gst.Promise.new())
        elif "ice" in msg and msg["ice"].get("candidate"):
            ice = msg["ice"]
            self.webrtc.emit("add-ice-candidate", int(ice.get("sdpMLineIndex") or 0), ice["candidate"])
