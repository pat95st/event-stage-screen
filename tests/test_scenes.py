"""Scenes, media library, render state and kiosk access."""

from __future__ import annotations

import pytest

from stagescreen.application.bus import DomainError
from stagescreen.domain.scene import normalize_element, normalize_scene

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'


def upload(client, data, name, family=None):
    headers = {"X-Filename": name}
    if family:
        headers["X-Font-Family"] = family
    return client.post("/api/media", content=data, headers=headers)


def text_el(**kw):
    return {"type": "text", "x": 100, "y": 100, "w": 800, "h": 200, "props": {"text": "Hello", "font": "Inter"}, **kw}


# ---------------------------------------------------------------- domain

def test_normalize_fills_defaults():
    scene = normalize_scene({"elements": [{"type": "clock"}, {"type": "color", "props": {"color": "#FF0000"}}]})
    assert scene["width"] == 1920 and scene["transition"] == {"type": "fade", "duration_ms": 500}
    clock, color = scene["elements"]
    assert clock["props"]["format"] == "HH:mm:ss" and clock["props"]["style"] == "digital"
    assert color["props"]["color"] == "#ff0000" and color["id"].startswith("el_")
    assert clock["crop"] == {"top": 0, "right": 0, "bottom": 0, "left": 0}


@pytest.mark.parametrize("bad", [
    {"type": "video"},
    {"type": "text", "w": 0},
    {"type": "text", "rotation": 400},
    {"type": "text", "opacity": 2},
    {"type": "text", "crop": {"left": 0.5, "right": 0.5}},
    {"type": "text", "props": {"color": "red; background:url(x)"}},
    {"type": "text", "props": {"font": "Inter</style><script>"}},
    {"type": "clock", "props": {"format": "<b>HH</b>"}},
    {"type": "image", "props": {"media_id": "../../etc/passwd"}},
    {"type": "text", "id": "has space"},
])
def test_normalize_rejects_invalid(bad):
    with pytest.raises(DomainError):
        normalize_element(bad)


def test_duplicate_ids_rejected():
    with pytest.raises(DomainError):
        normalize_scene({"elements": [text_el(id="a"), text_el(id="a")]})


# ---------------------------------------------------------------- scenes API

def test_scene_lifecycle(authed, services):
    r = authed.post("/api/scenes", json={"output_id": 1, "name": "Main", "width": 1280, "height": 720})
    assert r.status_code == 200, r.text
    scene = r.json()["result"]
    sid = scene["id"]
    assert scene["live"]["width"] == 1280
    # the first scene of an output becomes active automatically
    assert services.settings.get("scene.active.1") == sid

    content = dict(scene["live"], elements=[text_el(id="t1")])
    assert authed.post(f"/api/scenes/{sid}/draft", json={"content": content}).status_code == 200
    got = authed.get(f"/api/scenes/{sid}").json()["result"]
    assert got["draft"]["elements"][0]["props"]["text"] == "Hello" and got["live"]["elements"] == []

    state = authed.get("/api/render/1/state").json()["result"]
    assert state["scene_id"] == sid and state["scene"]["elements"] == []  # draft is not live yet

    assert authed.post(f"/api/scenes/{sid}/publish", json={}).json()["result"]["published"] is True
    state = authed.get("/api/render/1/state").json()["result"]
    assert state["scene"]["elements"][0]["id"] == "t1"
    assert authed.get(f"/api/scenes/{sid}").json()["result"]["draft"] is None

    # live editing publishes the given content directly
    content["elements"][0]["props"]["text"] = "Live"
    authed.post(f"/api/scenes/{sid}/publish", json={"content": content})
    assert authed.get("/api/render/1/state").json()["result"]["scene"]["elements"][0]["props"]["text"] == "Live"

    # discard
    authed.post(f"/api/scenes/{sid}/draft", json={"content": dict(content, background="#ff0000")})
    authed.post(f"/api/scenes/{sid}/discard")
    assert authed.get(f"/api/scenes/{sid}").json()["result"]["draft"] is None

    dup = authed.post(f"/api/scenes/{sid}/duplicate", json={}).json()["result"]
    assert dup["name"] == "Main (copy)"
    assert authed.post(f"/api/scenes/{dup['id']}/rename", json={"name": "Break"}).status_code == 200
    listing = authed.get("/api/scenes").json()["result"]["outputs"][0]
    assert [s["name"] for s in listing["scenes"]] == ["Main", "Break"] and listing["active"] == sid

    assert authed.post("/api/outputs/1/activate", json={"scene_id": dup["id"]}).status_code == 200
    assert authed.get("/api/render/1/state").json()["result"]["scene_name"] == "Break"
    assert authed.post("/api/outputs/2/activate", json={"scene_id": sid}).status_code == 422  # wrong output
    assert authed.post("/api/outputs/1/activate", json={"scene_id": None}).status_code == 200
    assert authed.get("/api/render/1/state").json()["result"]["scene"] is None

    assert authed.post(f"/api/scenes/{sid}/delete").status_code == 200
    assert authed.get(f"/api/scenes/{sid}").status_code == 404


def test_scene_validation_errors(authed):
    sid = authed.post("/api/scenes", json={"output_id": 1, "name": "X"}).json()["result"]["id"]
    bad = {"elements": [{"type": "text", "w": -5}]}
    r = authed.post(f"/api/scenes/{sid}/draft", json={"content": bad})
    assert r.status_code == 422 and "Width" in r.json()["error"]
    assert authed.post("/api/scenes", json={"output_id": 3, "name": "X"}).status_code == 404
    assert authed.post("/api/scenes", json={"output_id": 1, "name": ""}).status_code == 422
    missing_image = {"elements": [{"type": "image", "props": {"media_id": "m_doesnotexist"}}]}
    assert authed.post(f"/api/scenes/{sid}/draft", json={"content": missing_image}).status_code == 422


def test_mirror_render_state(authed):
    sid = authed.post("/api/scenes", json={"output_id": 1, "name": "Main"}).json()["result"]["id"]
    authed.post("/api/outputs/2", json={"mirror_of": 1, "mirror": {"scaling": "stretch", "rotation": 90, "flip_h": True}})
    state = authed.get("/api/render/2/state").json()["result"]
    assert state["scene_id"] == sid and state["source_output"] == 1
    assert state["mirror"] == {"scaling": "stretch", "rotation": 90, "flip_h": True, "flip_v": False}


def test_export_import_with_media(authed, services):
    media = upload(authed, PNG, "logo.png").json()["result"]
    sid = authed.post("/api/scenes", json={"output_id": 1, "name": "Logo scene"}).json()["result"]["id"]
    content = {"elements": [{"type": "image", "id": "img", "props": {"media_id": media["id"]}}]}
    authed.post(f"/api/scenes/{sid}/publish", json={"content": content})
    r = authed.get(f"/api/scenes/{sid}/export?media=1")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert data["format"] == "event-stage-screen-scene" and len(data["media"]) == 1

    # importing into a fresh device recreates the image; here it is deduplicated by hash
    imported = authed.post("/api/scenes/import", json={"output_id": 2, "data": data}).json()["result"]
    assert imported["output_id"] == 2 and imported["live"]["elements"][0]["props"]["media_id"] == media["id"]
    assert len(services.media_repo.list("image")) == 1
    assert authed.post("/api/scenes/import", json={"output_id": 1, "data": {"format": "x"}}).status_code == 422

    # media in use cannot be deleted
    r = authed.post(f"/api/media/{media['id']}/delete")
    assert r.status_code == 409 and "Logo scene" in r.json()["error"]


# ---------------------------------------------------------------- media

def test_media_upload_types(authed, services):
    png = upload(authed, PNG, "a.png").json()["result"]
    assert png["kind"] == "image" and png["mime"] == "image/png"
    assert upload(authed, PNG, "again.png").json()["result"]["id"] == png["id"]  # same content → same item
    svg = upload(authed, SVG, "logo.svg").json()["result"]
    assert svg["mime"] == "image/svg+xml"
    font = upload(authed, b"wOF2" + b"\x00" * 100, "Brand-Bold.woff2").json()["result"]
    assert font["kind"] == "font" and font["family"] == "Brand"
    named = upload(authed, b"OTTO" + b"\x00" * 100, "x.otf", family="Stage Sans").json()["result"]
    assert named["family"] == "Stage Sans"
    assert upload(authed, b"MZ\x90\x00 not allowed", "evil.exe").status_code == 422
    assert upload(authed, b"<html><script>alert(1)</script>", "x.svg").status_code == 422

    r = authed.get(f"/media/{svg['id']}")
    assert r.status_code == 200 and "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert authed.get("/media/m_unknown").status_code == 404

    fonts = authed.get("/api/fonts").json()["result"]
    assert set(fonts["uploaded"]) == {"Brand", "Stage Sans"}
    css = authed.get("/api/fonts.css").text
    assert 'font-family: "Stage Sans"' in css and f"/media/{named['id']}" in css

    assert authed.post(f"/api/media/{png['id']}/rename", json={"name": "Logo"}).status_code == 200
    assert authed.post(f"/api/media/{font['id']}/rename", json={"name": "f", "family": "bad<font>"}).status_code == 422
    assert authed.post(f"/api/media/{png['id']}/delete").status_code == 200
    assert not (services.config.media_dir / png["filename"]).exists()


def test_splash_settings(authed):
    svg = upload(authed, SVG, "logo.svg").json()["result"]
    assert authed.post("/api/splash", json={"mode": "logo", "logo": svg["id"]}).status_code == 200
    state = authed.get("/api/render/1/state").json()["result"]
    assert state["splash"] == {"mode": "logo", "logo": svg["id"]}
    assert authed.post("/api/splash", json={"mode": "rainbow"}).status_code == 422
    assert authed.post(f"/api/media/{svg['id']}/delete").status_code == 409


# ---------------------------------------------------------------- kiosk access

def test_render_requires_session_or_kiosk_token(client, services):
    client.post("/api/auth/setup", json={"username": "admin", "password": "secret123"})
    client.cookies.clear()
    token = services.kiosk_token
    assert (services.config.data_dir / "kiosk.token").read_text() == token
    assert client.get("/api/render/1/state").status_code == 401
    assert client.get("/render/1", follow_redirects=False).status_code == 303
    assert client.get(f"/render/1?k={token}").status_code == 200
    assert client.get(f"/api/render/1/state?k={token}").json()["result"]["output_id"] == 1
    assert client.get("/api/render/1/state", headers={"X-ESS-Kiosk": token}).status_code == 200
    assert client.get("/api/render/1/state?k=wrong").status_code == 401
    # the kiosk token gives no access to the admin API or to writes
    assert client.get(f"/api/system?k={token}").status_code == 401
    assert client.post(f"/api/scenes?k={token}", json={"output_id": 1, "name": "x"}).status_code == 401
    # kiosk agent endpoints need the token, a session is not enough
    assert client.get("/api/kiosk/config").status_code == 401
    cfg = client.get("/api/kiosk/config", headers={"X-ESS-Kiosk": token}).json()["result"]
    assert [o["connector"] for o in cfg["outputs"]] == ["HDMI-A-1", "HDMI-A-2"]
    report = {"outputs": [{"name": "HDMI-A-1", "enabled": True, "modes": [
        {"width": 1920, "height": 1080, "refresh": 60.0, "preferred": True, "current": True},
        {"width": 1920, "height": 1080, "refresh": 50.0, "preferred": False, "current": False}]}]}
    assert client.post("/api/kiosk/report", json=report, headers={"X-ESS-Kiosk": token}).status_code == 200
    client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
    out = client.get("/api/outputs").json()["result"]
    assert out["kiosk_online"] is True
    assert out["outputs"][0]["current_mode"]["refresh"] == 60.0 and len(out["outputs"][0]["kiosk_modes"]) == 2
