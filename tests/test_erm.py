"""eventrentmanager integration against the fake Display API server."""

from __future__ import annotations

import pytest

from stagescreen.application.bus import DomainError
from stagescreen.domain.scene import normalize_element
from stagescreen.infrastructure.erm import ErmClient, ErmPoller, normalize_base_url, parse_time, token_hint, validate_token
from tests.fake_erm import FakeErm, sample_state

TOKEN = "mvd_test_token_1234"


@pytest.fixture()
def fake():
    with FakeErm(TOKEN) as server:
        yield server


def test_url_and_token_validation():
    assert normalize_base_url("verleih.example.de/") == "https://verleih.example.de"
    assert normalize_base_url("http://10.0.0.5:8000") == "http://10.0.0.5:8000"
    assert normalize_base_url("") == ""
    with pytest.raises(DomainError):
        normalize_base_url("https://bad host")
    assert validate_token(f"  {TOKEN} ") == TOKEN
    for bad in ("mvp_abcdefghijkl", "mvd_short", "mvd_with space here"):
        with pytest.raises(DomainError):
            validate_token(bad)
    assert token_hint(TOKEN) == "mvd_test…1234"
    assert parse_time("2026-11-15T20:12:00+01:00") == 1794769920.0


def test_poller_online_offset_and_errors(fake):
    changes = []
    poller = ErmPoller(ErmClient(fake.url), TOKEN, on_change=lambda: changes.append(1))
    delay = poller.poll_once()
    assert poller.state.status == "online" and delay == 2.0
    assert abs(poller.state.offset_ms) < 2000
    assert poller.state.data["runSheet"]["program"]["name"] == "Abendprogramm"
    assert fake.requests[-1]["auth"] == f"Bearer {TOKEN}" and "version=ess-" in fake.requests[-1]["path"]
    assert changes

    fake.status_override = 500
    assert poller.poll_once() >= 5 and poller.state.status == "offline" and "500" in poller.state.message
    assert poller.state.data is not None  # last known state is kept
    fake.status_override = None

    bad = ErmPoller(ErmClient(fake.url), "mvd_wrong_token_9999")
    assert bad.poll_once() == 15.0 and bad.state.status == "unauthorized"
    assert "Display-Token" in bad.state.message

    gone = ErmPoller(ErmClient("http://127.0.0.1:9"), TOKEN)
    gone.poll_once()
    assert gone.state.status == "offline"


def test_settings_api_and_render_data(authed, services, fake):
    assert authed.post("/api/erm/settings", json={"base_url": fake.url, "verify_tls": True}).status_code == 200
    assert authed.post("/api/erm/outputs/1", json={"token": "nope"}).status_code == 422
    assert authed.post("/api/erm/outputs/1", json={"token": TOKEN}).status_code == 200
    assert authed.post("/api/erm/outputs/2", json={"token": None, "source": "output1"}).status_code == 200

    status = authed.get("/api/erm").json()["result"]
    assert status["base_url"] == fake.url
    out1, out2 = status["outputs"]
    assert out1["token_hint"] == "mvd_test…1234" and out2["source"] == "output1"

    # one poller serves both outputs (same token)
    assert list(services.erm.pollers) == [TOKEN]
    services.erm.pollers[TOKEN].poll_once()
    for n in (1, 2):
        erm = authed.get(f"/api/render/{n}/erm").json()["result"]
        assert erm["status"] == "online" and erm["data"]["runSheet"]["current"]["name"] == "Begrüßung"
        assert erm["now_ms"] > 0 and erm["received_at_ms"] > 0
    state = authed.get("/api/render/1/state").json()["result"]
    assert state["erm"]["data"]["display"]["thresholds"]["warnSeconds"] == 120
    dash = authed.get("/api/dashboard").json()["result"]
    assert dash["erm"][0]["status"] == "online" and dash["erm"][0]["display_name"] == "Bühnenmonitor links"

    test = authed.post("/api/erm/test", json={"base_url": fake.url, "output_id": 1}).json()["result"]
    assert test["display_name"] == "Bühnenmonitor links" and test["program"] == "Abendprogramm"
    r = authed.post("/api/erm/test", json={"base_url": fake.url, "token": "mvd_wrong_token_9999"})
    assert r.status_code == 401

    # removing the token stops the poller; output 2 follows output 1
    authed.post("/api/erm/outputs/1", json={"token": ""})
    assert services.erm.pollers == {}
    assert authed.get("/api/render/2/erm").json()["result"]["status"] == "unconfigured"


def test_kiosk_token_reads_erm_data(client, services):
    client.post("/api/auth/setup", json={"username": "admin", "password": "secret123"})
    client.cookies.clear()
    assert client.get("/api/render/1/erm").status_code == 401
    assert client.get(f"/api/render/1/erm?k={services.kiosk_token}").json()["result"]["configured"] is False
    assert client.get(f"/api/erm?k={services.kiosk_token}").status_code == 401  # admin API stays closed


def test_erm_elements_validate():
    e = normalize_element({"type": "erm", "props": {"field": "countdown", "warn_color": "#FFAA00"}})
    assert e["props"]["warn_color"] == "#ffaa00" and e["props"]["show_pending"] is True
    notes = normalize_element({"type": "erm", "props": {"field": "notes", "notes_filter": "custom", "notes_keys": ["sound", "x"]}})
    assert notes["props"]["notes_keys"] == ["sound"]
    lst = normalize_element({"type": "erm", "props": {"field": "itemlist", "max_rows": 5}})
    assert lst["props"]["max_rows"] == 5 and "warn_color" not in lst["props"]
    clock = normalize_element({"type": "clock", "props": {"time_source": "system"}})
    assert clock["props"]["time_source"] == "system"
    for bad in ({"field": "weather"}, {"field": "itemlist", "max_rows": 99}, {"field": "notes", "notes_filter": "some"},
                {"field": "messages", "urgent_color": "red"}):
        with pytest.raises(DomainError):
            normalize_element({"type": "erm", "props": bad})


def test_sample_state_shape():
    st = sample_state()
    rs = st["runSheet"]
    assert rs["current"]["remainingSeconds"] > 0 and rs["next"]["position"] == 3
    assert st["display"]["options"]["notes"] == ["general", "stage"]
