"""Kiosk agent: wlr-randr parsing/planning and browser supervision (no real display)."""

from __future__ import annotations

import json

from stagescreen.kiosk.agent import KioskAgent, sway_rules
from stagescreen.kiosk.randr import Randr, parse_json, parse_text

TEXT = """HDMI-A-1 "Samsung Electric Company U28E590 (HDMI-A-1)"
  Make: Samsung Electric Company
  Physical size: 610x350 mm
  Enabled: yes
  Modes:
    3840x2160 px, 30.000000 Hz (preferred)
    1920x1080 px, 60.000000 Hz (current)
    1920x1080 px, 59.940002 Hz
    1920x1080 px, 50.000000 Hz
  Position: 0,0
  Transform: normal
  Scale: 1.000000
HDMI-A-2 "Unknown (HDMI-A-2)"
  Enabled: no
  Modes:
    1280x720 px, 60.000000 Hz (preferred)
  Position: 0,0
  Transform: normal
"""


def test_parse_text_and_json():
    outs = parse_text(TEXT)
    assert [o["name"] for o in outs] == ["HDMI-A-1", "HDMI-A-2"]
    assert outs[0]["modes"][1] == {"width": 1920, "height": 1080, "refresh": 60.0, "preferred": False, "current": True}
    assert outs[1]["enabled"] is False
    js = json.dumps([{"name": "HDMI-A-1", "enabled": True, "transform": "90", "position": {"x": 0, "y": 0},
                      "modes": [{"width": 1920, "height": 1080, "refresh": 60.0, "preferred": True, "current": True}]}])
    assert parse_json(js)[0]["transform"] == "90"


def test_plan():
    out = parse_text(TEXT)[0]
    r = Randr(run=lambda cmd: (0, ""))
    same = {"enabled": True, "mode": {"width": 1920, "height": 1080, "refresh": 60}, "rotation": 0}
    assert r.plan(out, same, 0) is None
    assert r.plan(out, dict(same, mode={"width": 1920, "height": 1080, "refresh": 59.94}), 0) == \
        ["--output", "HDMI-A-1", "--on", "--mode", "1920x1080@59.940000Hz"]
    assert r.plan(out, dict(same, mode={"width": 2560, "height": 720, "refresh": 59.94}, custom_mode=True), 0) == \
        ["--output", "HDMI-A-1", "--on", "--custom-mode", "2560x720@59.94Hz"]
    assert r.plan(out, dict(same, mode=None), 0) == ["--output", "HDMI-A-1", "--on", "--preferred"]
    assert r.plan(out, dict(same, rotation=90), 1920) == ["--output", "HDMI-A-1", "--on", "--transform", "90", "--pos", "1920,0"]
    assert r.plan(out, {"enabled": False}, 0) == ["--output", "HDMI-A-1", "--off"]


class FakeProc:
    def __init__(self):
        self.alive = True

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.alive = False

    def wait(self, timeout=None):
        return 1

    def kill(self):
        self.alive = False


def make_agent(tmp_path, randr_text=TEXT):
    calls, spawned, reports = [], [], []
    now = [0.0]

    def run(cmd):
        calls.append(cmd)
        if cmd == ["wlr-randr", "--json"]:
            return 1, "unknown option"
        if cmd == ["wlr-randr"]:
            return 0, randr_text
        return 0, ""

    config = {"outputs": [
        {"id": 1, "connector": "HDMI-A-1", "enabled": True, "mode": {"width": 1920, "height": 1080, "refresh": 50},
         "custom_mode": False, "rotation": 0, "url": "http://127.0.0.1:8080/render/1"},
        {"id": 2, "connector": "HDMI-A-2", "enabled": True, "mode": None, "custom_mode": False, "rotation": 0,
         "url": "http://127.0.0.1:8080/render/2"},
    ], "token": "tok"}

    def http(path, data):
        if path == "/api/kiosk/config":
            return {"result": config}
        reports.append(data)
        return {"ok": True}

    def spawn(cmd):
        p = FakeProc()
        spawned.append((cmd, p))
        return p

    agent = KioskAgent(base_url="http://x", token="tok", randr=Randr(run=run), runtime_dir=tmp_path,
                       sway_dir=tmp_path, spawn=spawn, http=http, clock=lambda: now[0])
    return agent, calls, spawned, reports, now, config


def test_agent_applies_modes_and_starts_browsers(tmp_path):
    agent, calls, spawned, reports, now, config = make_agent(tmp_path)
    agent.step()
    applied = [c for c in calls if c[0] == "wlr-randr" and len(c) > 1 and c[1] == "--output"]
    assert ["wlr-randr", "--output", "HDMI-A-1", "--on", "--mode", "1920x1080@50.000000Hz"] in applied
    assert any(c[:4] == ["wlr-randr", "--output", "HDMI-A-2", "--on"] for c in applied)
    assert 'for_window [app_id="__render_2-"] move container to output "HDMI-A-2"' in (tmp_path / "rules").read_text()
    assert len(spawned) == 2
    cmd = spawned[0][0]
    assert "--app=http://127.0.0.1:8080/render/1?k=tok" in cmd
    assert reports[-1]["browsers"]["1"]["running"] is True

    # crashed browser is restarted after a back-off
    spawned[0][1].alive = False
    agent.step()
    assert len(spawned) == 2 and agent.browsers[1].restarts == 1
    now[0] += 3
    agent.step()
    assert len(spawned) == 3

    # disabling an output stops its browser and switches the port off
    config["outputs"][1]["enabled"] = False
    agent.step()
    assert 2 not in agent.browsers and spawned[1][1].alive is False
    assert ["wlr-randr", "--output", "HDMI-A-2", "--off"] not in calls  # it was already off in the fake output


def test_agent_without_web_service_keeps_browsers(tmp_path):
    agent, calls, spawned, reports, now, config = make_agent(tmp_path)
    agent.step()
    assert len(spawned) == 2

    def down(path, data):
        raise OSError("connection refused")

    agent.http = down
    agent.step()
    assert all(b.running() for b in agent.browsers.values())


def test_sway_rules():
    rules = sway_rules({1: "HDMI-A-1", 2: "HDMI-A-2"})
    assert rules.count("for_window") == 2 and "fullscreen enable" in rules and '"HDMI-A-2"' in rules
