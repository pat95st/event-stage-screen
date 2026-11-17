"""The root helper validates every argument itself (security boundary)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parent.parent / "deploy" / "ess-helper"


@pytest.fixture()
def helper(monkeypatch):
    loader = importlib.machinery.SourceFileLoader("ess_helper", str(HELPER))
    spec = importlib.util.spec_from_loader("ess_helper", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    calls = []

    def fake_run(cmd, stdin=None):
        calls.append((cmd, stdin))
        if cmd[:3] == ["nmcli", "-t", "-f"]:
            return "eth0:ethernet\nwlan0:wifi\nlo:loopback\n"
        if cmd[:2] == ["nmcli", "-g"]:
            return "Wired connection 1\n"
        if cmd[:2] == ["timedatectl", "list-timezones"]:
            return "Europe/Berlin\nUTC\n"
        if cmd[:2] == ["busctl", "call"]:
            return 'o "/org/freedesktop/NetworkManager/Checkpoint/7"\n'
        return ""

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setenv("ESS_HELPER_TEST", "1")
    mod.calls = calls
    return mod


def test_unknown_action(helper, capsys):
    assert helper.main(["ess-helper", "rm", "-rf", "/"]) == 2


@pytest.mark.parametrize("argv", [
    ["set-hostname", "Bad_Name"], ["set-hostname"], ["set-timezone", "Mars/Base"],
    ["set-ntp", "a b"], ["reboot", "now"], ["net-checkpoint", "5"], ["net-confirm", "/etc/passwd"],
    ["net-ipv4", "eth9", "auto"], ["net-ipv4", "eth0", "manual", "10.0.0.5"], ["net-ipv4", "eth0", "manual", "10.0.0.5/32", "-"],
    ["net-ipv4", "eth0", "auto", "not-ip"], ["net-ipv4", "lo", "auto"], ["net-wifi", "eth0", "ssid"],
    ["net-ipv4", "eth0;reboot", "auto"],
])
def test_rejects_invalid_arguments(helper, argv):
    assert helper.main(["ess-helper", *argv]) == 1
    assert not [c for c in helper.calls if c[0][0] in ("hostnamectl", "systemctl") or c[0][:3] == ["nmcli", "connection", "modify"]]


def test_ipv4_manual(helper):
    assert helper.main(["ess-helper", "net-ipv4", "eth0", "manual", "10.0.0.5/24", "10.0.0.1", "1.1.1.1"]) == 0
    modify = next(c[0] for c in helper.calls if c[0][:3] == ["nmcli", "connection", "modify"])
    assert modify[3] == "Wired connection 1"
    assert modify[4:] == ["ipv4.method", "manual", "ipv4.addresses", "10.0.0.5/24", "ipv4.gateway", "10.0.0.1",
                          "ipv4.dns", "1.1.1.1", "ipv4.ignore-auto-dns", "yes"]
    assert helper.calls[-1][0] == ["nmcli", "connection", "up", "Wired connection 1"]


def test_checkpoint_prints_path(helper, capsys):
    assert helper.main(["ess-helper", "net-checkpoint", "60"]) == 0
    assert capsys.readouterr().out.strip() == "/org/freedesktop/NetworkManager/Checkpoint/7"
    assert helper.calls[-1][0][-4:] == ["aouu", "0", "60", "3"]


def test_wifi_password_from_stdin(helper, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("supersecret\n"))
    assert helper.main(["ess-helper", "net-wifi", "wlan0", "Venue WiFi"]) == 0
    assert helper.calls[-1][0] == ["nmcli", "device", "wifi", "connect", "Venue WiFi", "ifname", "wlan0", "password", "supersecret"]
