"""Read-only system information: Pi model, CPU, temperature, throttling, network."""

from __future__ import annotations

import platform
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

import psutil

THROTTLE_BITS = {
    0: "Under-voltage detected",
    1: "ARM frequency capped",
    2: "Currently throttled",
    3: "Soft temperature limit active",
    16: "Under-voltage has occurred",
    17: "ARM frequency capping has occurred",
    18: "Throttling has occurred",
    19: "Soft temperature limit has occurred",
}


def pi_model() -> str:
    try:
        return Path("/proc/device-tree/model").read_text().rstrip("\x00\n")
    except OSError:
        return platform.machine()


def model_family(model: str) -> str:
    """'pi5', 'pi4' or 'other'."""
    if "Raspberry Pi 5" in model or "Compute Module 5" in model:
        return "pi5"
    if "Raspberry Pi 4" in model or "Compute Module 4" in model or "Raspberry Pi 400" in model:
        return "pi4"
    return "other"


def default_ndi_limit(family: str) -> int:
    """Default maximum of simultaneous NDI sources (see docs/SPEC.md, 'Pi 4 limits')."""
    return 1 if family == "pi4" else 4


def os_release() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return platform.platform()


def temperature_c() -> float | None:
    try:
        return round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def throttled_mask() -> int | None:
    if not shutil.which("vcgencmd"):
        return None
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2).stdout
        return int(out.strip().split("=")[1], 16)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def throttled_flags(mask: int | None) -> list[str]:
    if not mask:
        return []
    return [label for bit, label in THROTTLE_BITS.items() if mask & (1 << bit)]


def ip_addresses() -> list[str]:
    ips = []
    for name, addrs in psutil.net_if_addrs().items():
        if name == "lo":
            continue
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith("127."):
                ips.append(a.address)
    return sorted(set(ips))


def live_status() -> dict[str, Any]:
    """Cheap snapshot for the status bar / dashboard (called every few seconds)."""
    mask = throttled_mask()
    mem = psutil.virtual_memory()
    return {
        "cpu_percent": psutil.cpu_percent(None),
        "load": [round(x, 2) for x in psutil.getloadavg()],
        "memory_percent": mem.percent,
        "memory_used_mb": round(mem.used / 2**20),
        "memory_total_mb": round(mem.total / 2**20),
        "temperature_c": temperature_c(),
        "throttled": mask,
        "throttled_flags": throttled_flags(mask),
        "throttled_now": bool(mask and mask & 0xF),
    }


# ---------------------------------------------------------------------- time

NTP_DROPIN = Path("/etc/systemd/timesyncd.conf.d/event-stage-screen.conf")


def _run(cmd: list[str]) -> str:
    if not shutil.which(cmd[0]):
        return ""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def time_settings() -> dict[str, Any]:
    """Current time zone, NTP state and configured NTP servers."""
    props = {}
    for line in _run(["timedatectl", "show"]).splitlines():
        key, _, value = line.partition("=")
        props[key] = value
    servers: list[str] = []
    try:
        for line in NTP_DROPIN.read_text().splitlines():
            if line.startswith("NTP="):
                servers = line[4:].split()
    except OSError:
        pass
    return {
        "timezone": props.get("Timezone") or _local_timezone(),
        "ntp_enabled": props.get("NTP") == "yes",
        "ntp_synchronized": props.get("NTPSynchronized") == "yes",
        "ntp_servers": servers,
    }


def list_timezones() -> list[str]:
    zones = [z for z in _run(["timedatectl", "list-timezones"]).splitlines() if z]
    if zones:
        return zones
    try:
        import zoneinfo

        return sorted(z for z in zoneinfo.available_timezones() if "/" in z or z == "UTC")
    except Exception:  # pragma: no cover
        return ["UTC"]


def _local_timezone() -> str:
    try:
        return Path("/etc/timezone").read_text().strip() or "UTC"
    except OSError:
        return "UTC"
