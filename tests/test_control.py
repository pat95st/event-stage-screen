"""Milestone 6: setup aids, thumbnails, automatic switching, remote-control API and updates."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import time
from pathlib import Path

import pytest

from stagescreen.infrastructure.updates import parse_version, prune_backups
from stagescreen.kiosk.agent import KioskAgent
from stagescreen.kiosk.randr import Randr
from tests.fake_erm import FakeErm, sample_state

TOKEN = "mvd_test_token_1234"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def scene(client, output, name):
    return client.post("/api/scenes", json={"output_id": output, "name": name}).json()["result"]["id"]


# ---------------------------------------------------------------- setup aids

def test_overlays_and_thumbnails(authed, services):
    assert authed.post("/api/outputs/1/blackout", json={}).json()["result"]["blackout"] is True
    assert authed.get("/api/render/1/state").json()["result"]["overlay"]["blackout"] is True
    assert authed.get("/api/render/2/state").json()["result"]["overlay"]["blackout"] is False
    assert authed.post("/api/outputs/blackout", json={}).json()["result"]["blackout"] is True  # not all on → all on
    assert authed.post("/api/outputs/blackout", json={"on": False}).json()["result"]["blackout"] is False
    assert authed.post("/api/outputs/2/testpattern", json={"pattern": "grid"}).status_code == 200
    assert authed.post("/api/outputs/2/testpattern", json={"pattern": "rainbow"}).status_code == 422
    authed.post("/api/outputs/identify", json={"seconds": 3})
    ov = authed.get("/api/render/2/state").json()["result"]["overlay"]
    assert ov["test_pattern"] == "grid" and 0 < ov["identify_ms"] <= 3000

    # thumbnails: requested by the UI, delivered by the kiosk agent
    token = services.kiosk_token
    assert authed.get("/api/outputs/1/thumbnail").status_code == 404
    assert authed.get("/api/kiosk/config", headers={"X-ESS-Kiosk": token}).json()["result"]["thumbnails"] is True
    r = authed.post("/api/kiosk/thumbnail/1", content=JPEG, headers={"X-ESS-Kiosk": token})
    assert r.status_code == 200
    assert authed.post("/api/kiosk/thumbnail/1", content=b"not a jpeg", headers={"X-ESS-Kiosk": token}).status_code == 422
    assert authed.post("/api/kiosk/thumbnail/1", content=JPEG).status_code == 401
    t = authed.get("/api/outputs/1/thumbnail")
    assert t.status_code == 200 and t.content == JPEG


def test_agent_sends_thumbnails_when_requested(tmp_path):
    posted, captured = [], []
    config = {"outputs": [{"id": 1, "connector": "HDMI-A-1", "enabled": True, "mode": None, "custom_mode": False,
                           "rotation": 0, "url": "http://x/render/1"}], "token": "tok", "thumbnails": True}
    text = 'HDMI-A-1 "x"\n  Enabled: yes\n  Modes:\n    1920x1080 px, 60.000000 Hz (preferred, current)\n  Position: 0,0\n  Transform: normal\n'

    class P:
        def poll(self):
            return None

    agent = KioskAgent(base_url="http://x", token="tok",
                       randr=Randr(run=lambda cmd: (0, text) if cmd == ["wlr-randr"] else (1, "")),
                       runtime_dir=tmp_path, sway_dir=tmp_path, spawn=lambda cmd: P(),
                       http=lambda path, data: {"result": config} if data is None else {"ok": True},
                       capture=lambda conn, scale: captured.append((conn, scale)) or JPEG,
                       post_bytes=lambda path, data: posted.append((path, data)), clock=lambda: 100.0)
    agent.step()
    assert captured == [("HDMI-A-1", 0.25)] and posted == [("/api/kiosk/thumbnail/1", JPEG)]
    agent.step()  # within 5 s: no new capture
    assert len(captured) == 1
    config["thumbnails"] = False
    agent.last_thumbnail = -1e9
    agent.step()
    assert len(captured) == 1


# ---------------------------------------------------------------- automation

def test_automatic_switching_with_manual_override(authed, services):
    with FakeErm(TOKEN) as fake:
        authed.post("/api/erm/settings", json={"base_url": fake.url})
        authed.post("/api/erm/outputs/1", json={"token": TOKEN})
        running = scene(authed, 1, "Running")
        finished = scene(authed, 1, "Thanks")
        urgent = scene(authed, 1, "Urgent")
        offline = scene(authed, 1, "Offline")
        # the offline rule first: during an outage the last known state still counts for the others
        rules = [{"trigger": "erm_offline", "scene_id": offline, "seconds": 5},
                 {"trigger": "urgent_message", "scene_id": urgent},
                 {"trigger": "finished", "scene_id": finished},
                 {"trigger": "running", "scene_id": running}]
        r = authed.post("/api/automation/1", json={"enabled": True, "rules": rules})
        assert r.status_code == 200, r.text
        poller = services.erm.pollers[TOKEN]

        poller.poll_once()
        services.automation.evaluate_all()
        assert services.settings.get("scene.active.1") == running

        fake.state = lambda: sample_state(finished=True)
        poller.poll_once()
        services.automation.evaluate_all()
        assert services.settings.get("scene.active.1") == finished

        fake.state = lambda: sample_state(urgent=True)
        poller.poll_once()
        services.automation.evaluate_all()
        assert services.settings.get("scene.active.1") == urgent

        # manual switch wins until "back to automatic"
        authed.post("/api/outputs/1/activate", json={"scene_id": running})
        services.automation.evaluate_all()
        view = authed.get("/api/automation").json()["result"]["outputs"][0]
        assert view["manual"] is True and services.settings.get("scene.active.1") == running
        assert authed.post("/api/outputs/1/automatic").status_code == 200
        assert services.settings.get("scene.active.1") == urgent

        # ERM offline for longer than the rule's seconds
        fake.status_override = 500
        poller.poll_once()
        poller.state.offline_since = time.time() - 10
        services.automation.evaluate_all()
        assert services.settings.get("scene.active.1") == offline

    bad = authed.post("/api/automation/1", json={"enabled": True, "rules": [{"trigger": "weather", "scene_id": running}]})
    assert bad.status_code == 422
    other = scene(authed, 2, "Other output")
    assert authed.post("/api/automation/1", json={"enabled": True, "rules": [{"trigger": "always", "scene_id": other}]}).status_code == 422


# ---------------------------------------------------------------- remote-control API

def test_api_keys_and_v1(authed, services):
    a = scene(authed, 1, "Walk-in")
    b = scene(authed, 1, "Break")
    content = {"elements": [{"type": "text", "id": "logo", "name": "Logo", "props": {"text": "x"}}]}
    authed.post(f"/api/scenes/{a}/publish", json={"content": content})

    key = authed.post("/api/apikeys", json={"name": "Companion"}).json()["result"]["key"]
    assert key.startswith("ess_")
    listed = authed.get("/api/apikeys").json()["result"]["keys"]
    assert listed[0]["name"] == "Companion" and "hash" not in listed[0] and "key" not in listed[0]

    authed.cookies.clear()
    hdr = {"Authorization": f"Bearer {key}"}
    assert authed.get("/api/v1/status").status_code == 401
    st = authed.get("/api/v1/status", headers=hdr).json()["result"]
    assert st["outputs"][0]["scene"] == "Walk-in" and [s["name"] for s in st["outputs"][0]["scenes"]] == ["Walk-in", "Break"]
    assert authed.post("/api/v1/outputs/1/scene", json={"scene": "break"}, headers=hdr).json()["result"]["scene_id"] == b
    assert authed.get(f"/api/v1/outputs/1/scene/next?key={key}").json()["result"]["scene"] == "Walk-in"
    assert authed.post("/api/v1/outputs/1/scene/previous", headers={"X-API-Key": key}).json()["result"]["scene"] == "Break"
    assert authed.post("/api/v1/outputs/1/scene", json={"scene": "nope"}, headers=hdr).status_code == 404
    assert authed.get("/api/v1/blackout?on=true", headers=hdr).json()["result"]["blackout"] is True
    assert services.overlays[2]["blackout"] is True
    assert authed.post("/api/v1/outputs/1/testpattern", json={"pattern": "bars"}, headers=hdr).status_code == 200
    assert authed.post("/api/v1/outputs/1/identify", headers=hdr).status_code == 200

    authed.post("/api/v1/outputs/1/scene", json={"scene": "Walk-in"}, headers=hdr)
    r = authed.post("/api/v1/outputs/1/elements/logo/visible", headers=hdr).json()["result"]
    assert r == {"element": "Logo", "visible": False}
    assert services.scene_repo.get(a)["live"]["elements"][0]["visible"] is False
    assert authed.get("/api/v1/outputs/1/elements/Logo/visible?visible=1", headers=hdr).json()["result"]["visible"] is True

    assert authed.get("/api/v1/status", headers={"Authorization": "Bearer ess_wrong"}).status_code == 401

    # with a session: reading works, GET actions are refused (CSRF), POST needs the CSRF header
    authed.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
    assert authed.get("/api/v1/status").status_code == 200
    assert authed.get("/api/v1/blackout?on=true").status_code == 403
    assert authed.post("/api/v1/blackout", json={"on": False}).status_code == 200
    assert authed.post("/api/v1/blackout", json={"on": False}, headers={"X-ESS": ""}).status_code == 403

    key_id = listed[0]["id"]
    assert authed.post(f"/api/apikeys/{key_id}/delete").status_code == 200
    authed.cookies.clear()
    assert authed.get("/api/v1/status", headers=hdr).status_code == 401


def test_api_key_rate_limit(client):
    for _ in range(20):
        client.get("/api/v1/status", headers={"X-API-Key": "ess_bad"})
    assert client.get("/api/v1/status", headers={"X-API-Key": "ess_bad"}).status_code == 429


# ---------------------------------------------------------------- updates

def test_update_install_creates_backup_and_calls_helper(authed, services):
    assert parse_version("v1.2.3") == (1, 2, 3) and parse_version("1.2") is None
    assert authed.post("/api/updates/install", json={"tag": "1.0.0; rm -rf /"}).status_code == 422
    r = authed.post("/api/updates/install", json={"tag": "v9.9.9"})
    assert r.status_code == 200, r.text
    assert services.privileged.calls[-1][:2] == ("update", ("v9.9.9",))
    backups = authed.get("/api/backups").json()["result"]["backups"]
    assert backups and backups[0]["name"].startswith("pre-update-")
    assert authed.get(f"/api/backups/{backups[0]['name']}").headers["content-type"] == "application/zip"
    assert authed.get("/api/backups/..%2Fstagescreen.db").status_code == 404


def test_prune_backups(tmp_path):
    for i in range(8):
        f = tmp_path / f"pre-update-0.{i}.zip"
        f.write_bytes(b"x")
        t = time.time() - 100 + i
        import os
        os.utime(f, (t, t))
    prune_backups(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"pre-update-0.{i}.zip" for i in range(3, 8)]


@pytest.fixture()
def helper(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parent.parent / "deploy" / "ess-helper"
    loader = importlib.machinery.SourceFileLoader("ess_helper_upd", str(path))
    spec = importlib.util.spec_from_loader("ess_helper_upd", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    calls = []

    def fake_run(cmd, stdin=None):
        calls.append(cmd)
        if cmd[0] == "tar":
            target = Path(cmd[cmd.index("-C") + 1])
            (target / "deploy").mkdir(parents=True)
            (target / "deploy" / "install.sh").write_text("#!/bin/bash\n")
        return ""

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(mod, "UPDATE_DIR", tmp_path / "update")
    monkeypatch.setattr(mod, "ENV_FILE", tmp_path / "env")
    monkeypatch.setenv("ESS_HELPER_TEST", "1")
    mod.calls = calls
    return mod


def test_helper_update(helper, capsys, tmp_path):
    assert helper.main(["ess-helper", "update", "v1.2.3"]) == 0
    curl = next(c for c in helper.calls if c[0] == "curl")
    assert curl[-1] == "https://github.com/pat95st/event-stage-screen/archive/refs/tags/v1.2.3.tar.gz"
    run = helper.calls[-1]
    assert run[0] == "systemd-run" and run[-1].endswith("deploy/install.sh")
    assert capsys.readouterr().out.strip().startswith("ess-update-")
    for bad in ("1.2", "v1.2.3;id", "../x", "latest"):
        assert helper.main(["ess-helper", "update", bad]) == 1
    (tmp_path / "env").write_text("ESS_UPDATE_REPO=evil/../repo\n")
    assert helper.update_repo() == "pat95st/event-stage-screen"
    (tmp_path / "env").write_text("ESS_UPDATE_REPO=myfork/event-stage-screen\n")
    assert helper.update_repo() == "myfork/event-stage-screen"
