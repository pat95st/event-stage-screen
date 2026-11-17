"""Output control through ``wlr-randr`` (wlroots compositors such as sway)."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable
from typing import Any

log = logging.getLogger("ess.kiosk.randr")

TRANSFORMS = {0: "normal", 90: "90", 180: "180", 270: "270"}
RunFn = Callable[[list[str]], tuple[int, str]]


def run_command(cmd: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)


def parse_text(text: str) -> list[dict[str, Any]]:
    """Parse the classic human-readable ``wlr-randr`` output."""
    outputs: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    in_modes = False
    for line in text.splitlines():
        if line and not line[0].isspace():
            name = line.split(" ", 1)[0]
            cur = {"name": name, "enabled": True, "modes": [], "transform": "normal", "position": [0, 0]}
            outputs.append(cur)
            in_modes = False
            continue
        if cur is None:
            continue
        stripped = line.strip()
        if stripped.startswith("Modes:"):
            in_modes = True
            continue
        # refresh is missing for virtual (headless) outputs
        m = re.match(r"(\d+)x(\d+) px(?:, ([\d.]+) Hz)?(?: \((.*)\))?$", stripped)
        if in_modes and m:
            flags = m.group(4) or ""
            cur["modes"].append({"width": int(m.group(1)), "height": int(m.group(2)), "refresh": round(float(m.group(3) or 0), 3),
                                 "preferred": "preferred" in flags, "current": "current" in flags})
            continue
        in_modes = False
        key, _, value = stripped.partition(":")
        value = value.strip()
        if key == "Enabled":
            cur["enabled"] = value == "yes"
        elif key == "Transform":
            cur["transform"] = value
        elif key == "Position":
            try:
                cur["position"] = [int(v) for v in value.split(",")]
            except ValueError:
                pass
    return outputs


def parse_json(text: str) -> list[dict[str, Any]]:
    outputs = []
    for o in json.loads(text):
        pos = o.get("position") or {}
        outputs.append({
            "name": o.get("name"),
            "enabled": bool(o.get("enabled", True)),
            "transform": o.get("transform") or "normal",
            "position": [int(pos.get("x", 0)), int(pos.get("y", 0))],
            "modes": [{"width": int(m["width"]), "height": int(m["height"]), "refresh": round(float(m["refresh"]), 3),
                       "preferred": bool(m.get("preferred")), "current": bool(m.get("current"))}
                      for m in o.get("modes") or []],
        })
    return outputs


def current_mode(output: dict[str, Any]) -> dict[str, Any] | None:
    return next((m for m in output.get("modes") or [] if m.get("current")), None)


def choose_mode(output: dict[str, Any], width: int, height: int, refresh: float) -> dict[str, Any] | None:
    """The listed mode closest to the requested one (same size, refresh within 0.6 Hz)."""
    candidates = [m for m in output.get("modes") or [] if m["width"] == width and m["height"] == height
                  and abs(m["refresh"] - refresh) < 0.6]
    return min(candidates, key=lambda m: abs(m["refresh"] - refresh)) if candidates else None


class Randr:
    def __init__(self, run: RunFn = run_command):
        self.run = run
        self.json_supported: bool | None = None

    def outputs(self) -> list[dict[str, Any]]:
        if self.json_supported is not False:
            code, out = self.run(["wlr-randr", "--json"])
            if code == 0 and out.lstrip().startswith("["):
                self.json_supported = True
                return parse_json(out)
            self.json_supported = False
        code, out = self.run(["wlr-randr"])
        return parse_text(out) if code == 0 else []

    def plan(self, output: dict[str, Any], desired: dict[str, Any], x: int) -> list[str] | None:
        """wlr-randr arguments to reach ``desired`` (None if nothing to do).

        desired: {"enabled", "mode": {width,height,refresh} | None, "custom_mode", "rotation"}
        """
        name = output["name"]
        if not desired.get("enabled"):
            return ["--output", name, "--off"] if output.get("enabled") else None
        args = ["--output", name, "--on"]
        cur = current_mode(output)
        mode = desired.get("mode")
        changed = not output.get("enabled")
        if mode is None:
            preferred = next((m for m in output.get("modes") or [] if m.get("preferred")), None)
            if preferred and cur and (cur["width"], cur["height"], cur["refresh"]) != (preferred["width"], preferred["height"], preferred["refresh"]):
                args.append("--preferred")
                changed = True
        else:
            w, h, r = int(mode["width"]), int(mode["height"]), float(mode["refresh"])
            matches_current = cur and cur["width"] == w and cur["height"] == h and abs(cur["refresh"] - r) < 0.05
            if not matches_current:
                listed = None if desired.get("custom_mode") else choose_mode(output, w, h, r)
                if listed:
                    args += ["--mode", f"{listed['width']}x{listed['height']}@{listed['refresh']:.6f}Hz"]
                else:
                    args += ["--custom-mode", f"{w}x{h}@{r:g}Hz"]
                changed = True
        transform = TRANSFORMS.get(int(desired.get("rotation") or 0), "normal")
        if output.get("transform") != transform:
            args += ["--transform", transform]
            changed = True
        if list(output.get("position") or [0, 0]) != [x, 0]:
            args += ["--pos", f"{x},0"]
            changed = True
        return args if changed else None

    def apply(self, args: list[str]) -> tuple[bool, str]:
        code, out = self.run(["wlr-randr", *args])
        if code != 0:
            log.warning("wlr-randr %s failed: %s", " ".join(args), out.strip())
        else:
            log.info("wlr-randr %s", " ".join(args))
        return code == 0, out.strip()


def logical_width(output: dict[str, Any]) -> int:
    """Width the output occupies in the layout (rotation swaps width and height)."""
    mode = current_mode(output)
    if not mode:
        return 0
    return mode["height"] if output.get("transform") in ("90", "270", "flipped-90", "flipped-270") else mode["width"]
