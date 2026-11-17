"""Test doubles: fake nmcli output and a fake DRM sysfs tree."""

from __future__ import annotations

from pathlib import Path

NMCLI = {
    ("-t", "-f", "RUNNING", "general"): "running\n",
    ("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"):
        "eth0:ethernet:connected:Wired connection 1\nwlan0:wifi:disconnected:--\nlo:loopback:unmanaged:--\n",
    ("-t", "-f", "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS", "device", "show", "eth0"):
        "IP4.ADDRESS[1]:192.168.1.23/24\nIP4.GATEWAY:192.168.1.1\nIP4.DNS[1]:192.168.1.1\n",
    ("-t", "-f", "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS", "device", "show", "wlan0"): "IP4.GATEWAY:--\n",
    ("-t", "-f", "ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns", "connection", "show", "Wired connection 1"):
        "ipv4.method:auto\nipv4.addresses:\nipv4.gateway:--\nipv4.dns:\n",
    ("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes"):
        " :Venue WiFi:70:WPA2\n*:Crew\\:Net:55:WPA2\n :Venue WiFi:40:WPA2\n : :20:--\n",
}


async def fake_nmcli(args: list[str]) -> str:
    return NMCLI.get(tuple(args), "")


def make_drm(root: Path) -> Path:
    """Two HDMI connectors: HDMI-A-1 connected with modes, HDMI-A-2 disconnected."""
    drm = root / "drm"
    for name, status, modes in (
        ("card1-HDMI-A-1", "connected", "1920x1080\n1920x1080\n1280x720\n3840x2160\n"),
        ("card1-HDMI-A-2", "disconnected", ""),
        ("card1-Writeback-1", "unknown", ""),
    ):
        d = drm / name
        d.mkdir(parents=True)
        (d / "status").write_text(status + "\n")
        (d / "modes").write_text(modes)
    return drm
