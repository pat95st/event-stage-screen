"""GStreamer pipeline building blocks for the spike.

Pure string builders live at the top so they can be unit tested without
GStreamer being installed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

TEST_SOURCE = "test"


@dataclass(frozen=True)
class VideoFormat:
    width: int = 1280
    height: int = 720
    fps: int = 30

    @classmethod
    def parse(cls, text: str | None) -> "VideoFormat":
        """Parse ``WIDTHxHEIGHT@FPS`` (e.g. ``1920x1080@30``)."""
        if not text:
            return cls()
        size, _, fps = text.partition("@")
        w, _, h = size.lower().partition("x")
        return cls(int(w), int(h), int(fps or 30))

    @property
    def caps(self) -> str:
        return f"video/x-raw,width={self.width},height={self.height},framerate={self.fps}/1"


def quote(value: str) -> str:
    """Quote a property value for ``Gst.parse_launch``."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def source_description(source: str, fmt: VideoFormat) -> str:
    """Return a launch fragment producing raw video in ``fmt``.

    The test source burns a wall-clock timestamp into the picture (element
    ``stamp``) so glass-to-glass latency can be measured with a camera.
    """
    if source == TEST_SOURCE:
        return (
            f"videotestsrc is-live=true pattern=ball ! {fmt.caps} ! "
            "textoverlay name=stamp font-desc=\"Monospace 40\" "
            "valignment=top halignment=left shaded-background=true ! "
            "videoconvert"
        )
    return (
        f"ndisrc ndi-name={quote(source)} ! ndisrcdemux name=demux "
        "demux.video ! queue max-size-buffers=2 leaky=downstream ! "
        f"videoconvert ! videoscale ! videorate drop-only=true ! {fmt.caps}"
    )


def h264_encoder(prefer_hw: bool | None = None, bitrate_kbps: int = 6000) -> str:
    """Pick the H.264 encoder fragment (input: raw video, output: video/x-h264).

    The Pi 4 has a V4L2 hardware encoder (/dev/video11); the Pi 5 has none,
    so x264 in zero-latency mode is used there.
    """
    if prefer_hw is None:
        prefer_hw = _has_element("v4l2h264enc") and Path("/dev/video11").exists()
    if prefer_hw:
        return (
            "videoconvert ! video/x-raw,format=I420 ! "
            "v4l2h264enc extra-controls=\"controls,repeat_sequence_header=1,"
            f"video_bitrate={bitrate_kbps * 1000},h264_i_frame_period=30\" ! "
            "video/x-h264,level=(string)4,profile=(string)constrained-baseline"
        )
    return (
        "videoconvert ! video/x-raw,format=I420 ! "
        f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate_kbps} "
        "key-int-max=30 ! video/x-h264,profile=constrained-baseline"
    )


def webrtc_description(source: str, fmt: VideoFormat, codec: str = "h264") -> str:
    """Full per-client WebRTC sender pipeline ending in ``webrtcbin name=webrtc``."""
    src = source_description(source, fmt)
    if codec == "vp8":
        enc = (
            "videoconvert ! vp8enc deadline=1 cpu-used=8 target-bitrate=6000000 "
            "keyframe-max-dist=30 error-resilient=partitions ! "
            "rtpvp8pay pt=96 ! application/x-rtp,media=video,encoding-name=VP8,payload=96"
        )
    else:
        enc = (
            f"{h264_encoder()} ! h264parse config-interval=-1 ! "
            "rtph264pay config-interval=-1 aggregate-mode=zero-latency pt=96 ! "
            "application/x-rtp,media=video,encoding-name=H264,payload=96"
        )
    return (
        f"{src} ! queue max-size-buffers=2 leaky=downstream ! {enc} ! "
        "webrtcbin name=webrtc bundle-policy=max-bundle latency=0"
    )


def mjpeg_description(source: str, fmt: VideoFormat, quality: int = 80) -> str:
    """Shared MJPEG pipeline ending in ``appsink name=sink``."""
    return (
        f"{source_description(source, fmt)} ! "
        "queue max-size-buffers=1 leaky=downstream ! videoconvert ! "
        f"jpegenc quality={quality} ! "
        "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
    )


def _has_element(name: str) -> bool:
    try:
        from gi.repository import Gst  # type: ignore

        return Gst.ElementFactory.find(name) is not None
    except Exception:  # pragma: no cover - GStreamer missing
        return False


def ndi_available() -> bool:
    return _has_element("ndisrc")


def gst_init():
    """Initialise GStreamer and return the ``Gst`` module."""
    import gi  # type: ignore

    gi.require_version("Gst", "1.0")
    gi.require_version("GstWebRTC", "1.0")
    gi.require_version("GstSdp", "1.0")
    from gi.repository import Gst, GstSdp, GstWebRTC  # type: ignore  # noqa: F401

    # Importing GstWebRTC/GstSdp registers their boxed types, so values such
    # as the "offer" in a promise reply are properly typed in Python.
    if not Gst.is_initialized():
        Gst.init(None)
    return Gst


def discover_ndi_sources(timeout_s: float = 2.0) -> list[str]:
    """List NDI source names visible on the network (mDNS / discovery server)."""
    if not ndi_available():
        return []
    Gst = gst_init()
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Video/Source/Network", None)
    if not monitor.start():
        return []
    try:
        import time

        time.sleep(timeout_s)
        names = []
        for dev in monitor.get_devices() or []:
            props = dev.get_properties()
            name = props.get_string("ndi-name") if props else None
            names.append(name or dev.get_display_name())
        return sorted(set(names))
    finally:
        monitor.stop()


def describe_environment() -> dict:
    """Collect facts that matter for the report (GStreamer version, encoders)."""
    info: dict = {"ndi_runtime_env": os.environ.get("NDI_RUNTIME_DIR_V6")}
    try:
        Gst = gst_init()
        info["gstreamer"] = Gst.version_string()
        for el in ("ndisrc", "webrtcbin", "v4l2h264enc", "x264enc", "vp8enc", "jpegenc"):
            info[el] = Gst.ElementFactory.find(el) is not None
        info["v4l2_encoder_device"] = Path("/dev/video11").exists()
    except Exception as exc:  # pragma: no cover
        info["gstreamer_error"] = str(exc)
    return info


__all__ = [
    "TEST_SOURCE",
    "VideoFormat",
    "describe_environment",
    "discover_ndi_sources",
    "gst_init",
    "h264_encoder",
    "mjpeg_description",
    "ndi_available",
    "quote",
    "source_description",
    "webrtc_description",
]
