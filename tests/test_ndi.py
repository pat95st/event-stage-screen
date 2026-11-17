"""NDI: pipeline building, stream format, limits, settings and (with GStreamer) real MJPEG frames."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from stagescreen.application.bus import DomainError
from stagescreen.config import AppConfig
from stagescreen.container import Services
from stagescreen.domain.scene import ndi_sources, normalize_element
from stagescreen.infrastructure.ndi import (
    NdiService,
    StreamFormat,
    gst,
    pack_frame,
    pipeline_description,
    unpack_frames,
)
from stagescreen.infrastructure.network import NetworkService
from stagescreen.infrastructure.privileged import NullRunner
from stagescreen.web.app import create_app
from tests.fakes import fake_nmcli, make_drm

HAS_GST = gst() is not None and bool(gst().ElementFactory.find("jpegenc"))
needs_gst = pytest.mark.skipif(not HAS_GST, reason="GStreamer with jpegenc not available")


class MemSettings(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value):
        self[key] = value


def test_pipeline_description():
    ndi = pipeline_description('STUDIO "A" (OBS)', StreamFormat("720", 25, 70))
    assert 'ndisrc ndi-name="STUDIO \\"A\\" (OBS)"' in ndi and "timeout=5000" in ndi
    assert "demux.audio ! queue ! fakesink" in ndi
    assert "height=720,pixel-aspect-ratio=1/1" in ndi and "max-rate=25" in ndi and "quality=70" in ndi
    native = pipeline_description("X", StreamFormat("native", 30, 80))
    assert "videoscale" not in native
    test = pipeline_description("test:ball", StreamFormat())
    assert "videotestsrc is-live=true pattern=ball" in test and "ndisrc" not in test


def test_stream_format_and_framing():
    assert StreamFormat.from_params("bogus", "120", "5") == StreamFormat("1080", 60, 10)
    assert StreamFormat.from_params("540", None, None) == StreamFormat("540", 30, 80)
    with pytest.raises(DomainError):
        StreamFormat.from_params("720", "abc", "80")
    data = pack_frame(b"\xff\xd8jpeg", 12.5) + pack_frame(b"x", 13.0)
    frames, rest = unpack_frames(data)
    assert frames == [(12.5, b"\xff\xd8jpeg"), (13.0, b"x")] and rest == b""


def test_ndi_element_validation():
    e = normalize_element({"type": "ndi", "props": {"source": "CAM 1", "resolution": "720", "on_loss": "black"}})
    assert e["props"]["resolution"] == "720" and e["props"]["on_loss"] == "black" and e["props"]["loss_timeout"] == 5
    assert ndi_sources({"elements": [e]}) == {"CAM 1"}
    for bad in ({"source": 'a"b'}, {"resolution": "4k"}, {"on_loss": "explode"}, {"fps": 0}, {"loss_timeout": 500}):
        with pytest.raises(DomainError):
            normalize_element({"type": "ndi", "props": bad})


def test_discovery_servers_written_to_ndi_config(tmp_path):
    svc = NdiService(MemSettings(), lambda: 1, tmp_path)
    assert svc.set_discovery_servers(["192.168.1.10", " ", "ndi.example.org"]) == ["192.168.1.10", "ndi.example.org"]
    config = json.loads((tmp_path / ".ndi" / "ndi-config.v1.json").read_text())
    assert config["ndi"]["networks"]["discovery"] == "192.168.1.10,ndi.example.org"
    with pytest.raises(DomainError):
        svc.set_discovery_servers(["bad server name"])
    svc.set_discovery_servers([])
    assert "discovery" not in json.loads((tmp_path / ".ndi" / "ndi-config.v1.json").read_text())["ndi"]["networks"]


def test_test_sources_only_in_development_mode(tmp_path):
    svc = NdiService(MemSettings(), lambda: 1, tmp_path)
    with pytest.raises(DomainError):
        svc.validate_source("test:ball")
    with pytest.raises(DomainError):
        svc.validate_source("")


def test_api_without_ndi(authed):
    st = authed.get("/api/ndi").json()["result"]
    assert st["limit"] >= 1 and "streams" in st
    if not st["plugin"]:
        r = authed.get("/ndi/snapshot?source=CAM")
        assert r.status_code == 503
    assert authed.post("/api/ndi/settings", json={"discovery_servers": ["10.0.0.2"]}).status_code == 200
    assert authed.get("/api/ndi").json()["result"]["discovery_servers"] == ["10.0.0.2"]


@needs_gst
def test_hub_delivers_jpeg_frames_and_enforces_limit(tmp_path):
    async def run():
        svc = NdiService(MemSettings(), lambda: 1, tmp_path, allow_test_sources=True)
        hub = svc.hub("test:ball", StreamFormat("360", 15, 60))
        hub.lease(5)
        for _ in range(60):
            if hub.jpeg:
                break
            await asyncio.sleep(0.1)
        assert hub.jpeg and hub.jpeg[:3] == b"\xff\xd8\xff"
        # same source in another format is fine, a second source exceeds the limit of 1
        svc.hub("test:ball", StreamFormat("720", 15, 60))
        with pytest.raises(DomainError) as exc:
            svc.hub("test:smpte", StreamFormat("360", 15, 60))
        assert exc.value.status == 429
        frames = hub.frames()
        first = await asyncio.wait_for(frames.__anext__(), 5)
        assert unpack_frames(first)[0][0][1][:2] == b"\xff\xd8"
        await frames.aclose()
        svc.shutdown()
        assert not hub.running

    asyncio.run(run())


@needs_gst
def test_snapshot_endpoint_with_test_source(tmp_path: Path):
    runner = NullRunner()
    config = AppConfig(data_dir=tmp_path / "data", system_backend="null", drm_dir=make_drm(tmp_path), port=0,
                       ndi_test_sources=True)
    services = Services(config, privileged=runner, network=NetworkService(runner, read=fake_nmcli))
    with TestClient(create_app(services)) as client:
        client.headers["X-ESS"] = "1"
        client.post("/api/auth/setup", json={"username": "admin", "password": "secret123"})
        assert "test:smpte" in client.get("/api/ndi/sources").json()["result"]["sources"]
        r = client.get("/ndi/snapshot?source=test:smpte&res=360&fps=10")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
        client.cookies.clear()
        assert client.get("/ndi/snapshot?source=test:smpte&res=360&fps=10", follow_redirects=False).status_code == 303
        token = services.kiosk_token
        assert client.get(f"/ndi/snapshot?source=test:smpte&res=360&fps=10&k={token}").status_code == 200
        streams = services.ndi.status()["streams"]
        assert streams and streams[0]["source"] == "test:smpte"
