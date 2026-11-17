"""Detection of HDMI connectors via the kernel DRM sysfs interface.

``/sys/class/drm/card*-HDMI-A-*`` contains ``status`` (connected /
disconnected) and ``modes`` (the EDID mode list, without refresh rates).
Reading it needs no display session, so it also works before the kiosk runs.
The exact refresh rates per mode are added by the kiosk backend (milestone 3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Connector:
    name: str  # e.g. HDMI-A-1
    connected: bool
    modes: list[str] = field(default_factory=list)  # e.g. ["1920x1080", "1280x720"]

    def to_dict(self) -> dict:
        return {"name": self.name, "connected": self.connected, "modes": self.modes}


def detect_connectors(drm_dir: Path) -> list[Connector]:
    """Return HDMI connectors sorted by name. Missing sysfs → empty list."""
    result: dict[str, Connector] = {}
    try:
        entries = sorted(drm_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        m = re.fullmatch(r"card\d+-(HDMI-A-\d+)", entry.name)
        if not m:
            continue
        name = m.group(1)
        status = _read(entry / "status")
        modes: list[str] = []
        for line in _read(entry / "modes").splitlines():
            line = line.strip()
            if re.fullmatch(r"\d+x\d+i?", line) and line not in modes:
                modes.append(line)
        # On some kernels a connector shows up on two cards; keep the connected one.
        if name in result and result[name].connected and status != "connected":
            continue
        result[name] = Connector(name, status == "connected", modes)
    return [result[k] for k in sorted(result)]


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""
