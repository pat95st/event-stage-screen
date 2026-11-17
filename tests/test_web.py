"""Web application: auth flow, pages, outputs, system actions, network, backup."""

from __future__ import annotations

import io
import json
import zipfile

from stagescreen.infrastructure.backup import apply_pending_restore

PAGES = ["/", "/outputs", "/network", "/system", "/logs"]


def test_setup_login_logout_flow(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/setup"
    assert client.get("/api/dashboard").status_code == 401
    assert client.get("/api/health").json()["ok"] is True

    r = client.post("/api/auth/setup", json={"username": "ad", "password": "short"})
    assert r.status_code == 422
    assert client.post("/api/auth/setup", json={"username": "admin", "password": "secret123"}).status_code == 200
    assert client.post("/api/auth/setup", json={"username": "x", "password": "secret123"}).status_code == 409
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"

    client.post("/api/auth/logout")
    client.cookies.clear()
    assert client.get("/api/dashboard").status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "secret123"}).status_code == 200
    assert client.get("/api/dashboard").status_code == 200


def test_csrf_header_required(authed):
    r = authed.post("/api/ui/language", json={"language": "de"}, headers={"X-ESS": ""})
    assert r.status_code == 403


def test_login_rate_limit(authed):
    authed.cookies.clear()
    for _ in range(10):
        authed.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert authed.post("/api/auth/login", json={"username": "admin", "password": "secret123"}).status_code == 429


def test_pages_render_in_english_and_german(authed):
    for page in PAGES:
        r = authed.get(page)
        assert r.status_code == 200, page
        assert '<html lang="en"' in r.text and "Outputs" in r.text
    assert authed.post("/api/ui/language", json={"language": "de"}).status_code == 200
    for page in PAGES:
        r = authed.get(page)
        assert '<html lang="de"' in r.text and "Ausgänge" in r.text, page
    assert "window.ESS_I18N" in authed.get("/").text
    r = authed.post("/api/outputs/1", json={"mode": "bogus"})
    assert r.status_code == 422 and "Ungültiger Videomodus" in r.json()["error"]


def test_outputs_detect_and_save(authed, services):
    data = authed.get("/api/outputs").json()["result"]
    out1, out2 = data["outputs"]
    assert out1["connected"] is True and out1["modes"] == ["1920x1080", "1280x720", "3840x2160"]
    assert out2["detected"] is True and out2["connected"] is False
    assert [c["name"] for c in data["connectors"]] == ["HDMI-A-1", "HDMI-A-2"]

    r = authed.post("/api/outputs/2", json={
        "name": "Prompter", "connector": "HDMI-A-2", "enabled": True, "mode": "2560x720@59.94",
        "custom_mode": True, "rotation": 0, "mirror_of": 1,
        "mirror": {"scaling": "stretch", "rotation": 180, "flip_h": True, "flip_v": False},
    })
    assert r.status_code == 200, r.text
    saved = services.output_repo.get(2)
    assert saved.mode.label == "2560x720@59.94" and saved.custom_mode and saved.mirror_of == 1
    assert saved.mirror.flip_h and saved.mirror.rotation == 180 and saved.mirror.scaling.value == "stretch"

    assert authed.post("/api/outputs/1", json={"mirror_of": 1}).json()["result"]["mirror_of"] is None
    assert authed.post("/api/outputs/1", json={"rotation": 45}).status_code == 422
    assert authed.post("/api/outputs/3", json={}).status_code == 404


def test_system_actions_use_privileged_helper(authed, services):
    calls = services.privileged.calls
    assert authed.post("/api/system/hostname", json={"hostname": "Stage-Left"}).status_code == 200
    assert calls[-1][:2] == ("set-hostname", ("stage-left",))
    assert authed.post("/api/system/hostname", json={"hostname": "bad name!"}).status_code == 422

    tz = authed.get("/api/system/time").json()["result"]
    assert "Europe/Berlin" in tz["timezones"]
    assert authed.post("/api/system/timezone", json={"timezone": "Europe/Berlin"}).status_code == 200
    assert calls[-1][:2] == ("set-timezone", ("Europe/Berlin",))
    assert authed.post("/api/system/timezone", json={"timezone": "Mars/Base"}).status_code == 422

    assert authed.post("/api/system/ntp", json={"servers": ["ntp.example.org", "192.168.1.1"]}).status_code == 200
    assert calls[-1][:2] == ("set-ntp", ("ntp.example.org", "192.168.1.1"))
    assert authed.post("/api/system/ntp", json={"servers": ["bad server"]}).status_code == 422

    assert authed.post("/api/system/reboot").status_code == 200
    assert calls[-1][0] == "reboot"
    count = len(calls)
    assert authed.post("/api/system/shutdown", headers={"X-ESS": "0"}).status_code == 403
    assert len(calls) == count


def test_ndi_limit(authed, services):
    info = authed.get("/api/system").json()["result"]
    assert info["ndi"]["limit"] == info["ndi"]["default"]
    assert authed.post("/api/system/ndi-limit", json={"limit": 2}).json()["result"] == 2
    assert authed.post("/api/system/ndi-limit", json={"limit": 9}).status_code == 422
    assert authed.post("/api/system/ndi-limit", json={"limit": None}).json()["result"] == info["ndi"]["default"]


def test_network_change_needs_confirmation(authed, services):
    net = authed.get("/api/network").json()["result"]
    assert [d["device"] for d in net["devices"]] == ["eth0", "wlan0"]
    eth = net["devices"][0]
    assert eth["addresses"] == ["192.168.1.23/24"] and eth["config"]["method"] == "auto"

    r = authed.post("/api/network/ipv4", json={"device": "eth0", "ipv4": {
        "method": "manual", "address": "10.0.0.5/24", "gateway": "10.0.0.1", "dns": "1.1.1.1, 9.9.9.9"}})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["seconds_left"] > 50
    assert services.privileged.calls[-1][:2] == ("net-checkpoint", ("60",))
    # a second change is refused while the first one waits for confirmation
    assert authed.post("/api/network/ipv4", json={"device": "eth0", "ipv4": {"method": "auto"}}).status_code == 409
    assert authed.get("/api/dashboard").json()["result"]["network_pending"]["device"] == "eth0"

    # the change itself is applied in the background (after the response)
    import time
    for _ in range(40):
        if any(c[0] == "net-ipv4" for c in services.privileged.calls):
            break
        time.sleep(0.05)
    applied = next(c for c in services.privileged.calls if c[0] == "net-ipv4")
    assert applied[1] == ("eth0", "manual", "10.0.0.5/24", "10.0.0.1", "1.1.1.1", "9.9.9.9")

    assert authed.post("/api/network/confirm").status_code == 200
    assert services.privileged.calls[-1][:2] == ("net-confirm", ("/org/freedesktop/NetworkManager/Checkpoint/1",))
    assert authed.post("/api/network/confirm").status_code == 409


def test_network_validation(authed):
    bad = [
        {"device": "eth9", "ipv4": {"method": "auto"}},
        {"device": "eth0", "ipv4": {"method": "manual", "address": "10.0.0.5"}},
        {"device": "eth0", "ipv4": {"method": "manual", "address": "10.0.0.5/24", "gateway": "10.1.0.1"}},
        {"device": "eth0", "ipv4": {"method": "auto", "dns": "not-an-ip"}},
    ]
    for payload in bad:
        assert authed.post("/api/network/ipv4", json=payload).status_code in (404, 422), payload
    assert authed.post("/api/network/wifi", json={"device": "wlan0", "ssid": "x", "password": "short"}).status_code == 422


def test_wifi_scan_and_connect(authed, services):
    nets = authed.get("/api/network/wifi").json()["result"]["networks"]
    assert [n["ssid"] for n in nets] == ["Crew:Net", "Venue WiFi"]
    assert nets[1]["signal"] == 70
    r = authed.post("/api/network/wifi", json={"device": "wlan0", "ssid": "Venue WiFi", "password": "longpassword"})
    assert r.status_code == 200
    authed.post("/api/network/rollback")
    assert services.privileged.calls[-1][0] in ("net-rollback", "net-wifi")


def test_backup_and_restore(authed, services):
    services.settings.set("ui.language", "de")
    (services.config.media_dir / "logo.svg").write_text("<svg/>")
    r = authed.get("/api/backup")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    backup = r.content
    with zipfile.ZipFile(io.BytesIO(backup)) as zf:
        assert set(zf.namelist()) == {"manifest.json", "stagescreen.db", "media/logo.svg"}
        assert json.loads(zf.read("manifest.json"))["format"] == "event-stage-screen-backup"

    assert authed.post("/api/backup/restore", content=b"not a zip").status_code == 422
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"format": "event-stage-screen-backup", "format_version": 1}))
        zf.writestr("stagescreen.db", b"")
        zf.writestr("../etc/passwd", b"x")
    assert authed.post("/api/backup/restore", content=evil.getvalue()).status_code == 422

    # change the configuration, then restore the backup
    services.settings.set("ui.language", "en")
    (services.config.media_dir / "logo.svg").unlink()
    services.request_restart = lambda delay=1.0: None  # do not kill the test process
    assert authed.post("/api/backup/restore", content=backup).status_code == 200
    assert services.config.restore_file.exists()

    services.db.close()
    assert apply_pending_restore(services.config) is True
    assert not services.config.restore_file.exists()
    assert (services.config.media_dir / "logo.svg").read_text() == "<svg/>"
    from stagescreen.infrastructure.db import Database
    from stagescreen.infrastructure.repositories import SettingsRepository
    db = Database(services.config.db_path)
    assert SettingsRepository(db).get("ui.language") == "de"
    db.close()


def test_logs_and_system_info(authed):
    info = authed.get("/api/system").json()["result"]
    assert info["system_backend"] == "null"
    assert "SaveOutput" in info["commands"]["commands"]
    logs = authed.get("/api/logs?level=INFO").json()["result"]
    assert "entries" in logs
    assert authed.get("/api/logs/files").status_code == 200
