from spike import media
from spike.mjpeg import pack_frame, unpack_frames
from spike.report import render_markdown, summarise


def test_video_format_parse():
    fmt = media.VideoFormat.parse("1920x1080@25")
    assert (fmt.width, fmt.height, fmt.fps) == (1920, 1080, 25)
    assert media.VideoFormat.parse(None) == media.VideoFormat()
    assert media.VideoFormat.parse("640x360").fps == 30


def test_quote_escapes():
    assert media.quote('PC "A" (OBS)') == '"PC \\"A\\" (OBS)"'


def test_source_descriptions():
    fmt = media.VideoFormat(1280, 720, 30)
    assert "videotestsrc" in media.source_description("test", fmt)
    ndi = media.source_description("STUDIO (Cam 1)", fmt)
    assert 'ndi-name="STUDIO (Cam 1)"' in ndi and "ndisrcdemux" in ndi


def test_encoder_selection():
    assert "v4l2h264enc" in media.h264_encoder(prefer_hw=True)
    assert "x264enc" in media.h264_encoder(prefer_hw=False)
    assert "rtpvp8pay" in media.webrtc_description("test", media.VideoFormat(), codec="vp8")
    assert media.mjpeg_description("test", media.VideoFormat()).endswith("sync=false")


def test_mjpeg_framing_roundtrip():
    data = pack_frame(b"abc", 1.5) + pack_frame(b"defg", 2.5)
    frames, rest = unpack_frames(data + b"\x00\x00")
    assert frames == [(1.5, b"abc"), (2.5, b"defg")]
    assert rest == b"\x00\x00"


def test_report_groups_by_output_and_transport():
    records = [
        {"run": "r1", "scenario": "s", "kind": "environment", "model": "Raspberry Pi 5"},
        {"run": "r1", "scenario": "s", "kind": "system", "cpu_total": 40.0, "temp_c": 60.0,
         "throttled_flags": ["throttling occurred"], "cpu_groups": {"chromium": 80.0}},
        {"run": "r1", "scenario": "s", "kind": "browser", "output": 1, "fps": 59.0,
         "config": {"transport": "mjpeg", "source": "test", "format": "1280x720@30"},
         "video": {"transport": "mjpeg", "fps": 30, "latencyAvgMs": 20, "latencyP95Ms": 30}},
        {"run": "r1", "scenario": "s", "kind": "browser", "output": 2, "fps": 60.0,
         "config": {"transport": "none"}},
    ]
    summary = summarise(records)
    outputs = summary["r1"]["outputs"]
    assert set(outputs) == {"1 · mjpeg · test 1280x720@30", "2 · none"}
    assert outputs["1 · mjpeg · test 1280x720@30"]["latency_avg_ms"] == 20
    md = render_markdown(summary)
    assert "Raspberry Pi 5" in md and "throttling occurred" in md
