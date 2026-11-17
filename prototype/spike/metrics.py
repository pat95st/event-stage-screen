"""System and browser metrics recording (JSON lines)."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path

import psutil

log = logging.getLogger("spike.metrics")

PROCESS_GROUPS = {
    "chromium": ("chromium", "chrome", "chromium-browser"),
    "sway": ("sway", "Xwayland"),
    "spike": ("python", "python3"),
}


def pi_model() -> str:
    try:
        return Path("/proc/device-tree/model").read_text().rstrip("\x00\n")
    except OSError:
        return platform.machine()


def os_release() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return platform.platform()


def read_temperature() -> float | None:
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0
    except (OSError, ValueError):
        return None


def read_throttled() -> int | None:
    """Return the ``vcgencmd get_throttled`` bit mask, if available."""
    if not shutil.which("vcgencmd"):
        return None
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2).stdout
        return int(out.strip().split("=")[1], 16)
    except Exception:
        return None


def decode_throttled(mask: int | None) -> list[str]:
    if not mask:
        return []
    bits = {
        0: "under-voltage now", 1: "frequency capped now", 2: "throttled now",
        3: "soft temperature limit now", 16: "under-voltage occurred",
        17: "frequency capping occurred", 18: "throttling occurred",
        19: "soft temperature limit occurred",
    }
    return [label for bit, label in bits.items() if mask & (1 << bit)]


def group_cpu(procs: dict[int, psutil.Process]) -> dict[str, float]:
    """Sum CPU percent (of one core) per process group."""
    totals = {name: 0.0 for name in PROCESS_GROUPS}
    for proc in list(procs.values()):
        try:
            name = proc.name()
            pct = proc.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        for group, names in PROCESS_GROUPS.items():
            if any(name.startswith(n) for n in names):
                totals[group] += pct
                break
    return {k: round(v, 1) for k, v in totals.items()}


class Recorder:
    """Appends samples to ``<data_dir>/metrics.jsonl`` tagged with the scenario."""

    def __init__(self, data_dir: Path, scenario: str) -> None:
        self.path = data_dir / "metrics.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.scenario = scenario
        self.run_id = time.strftime("%Y%m%d-%H%M%S")

    def write(self, kind: str, data: dict) -> None:
        record = {"t": time.time(), "run": self.run_id, "scenario": self.scenario, "kind": kind, **data}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def write_environment(self, extra: dict) -> None:
        self.write("environment", {
            "host": socket.gethostname(),
            "model": pi_model(),
            "os": os_release(),
            "kernel": platform.release(),
            "cpus": psutil.cpu_count(),
            "memory_mb": round(psutil.virtual_memory().total / 2**20),
            **extra,
        })


async def sample_system(recorder: Recorder, interval: float = 2.0) -> None:
    """Background task sampling CPU, memory, temperature and throttling."""
    procs: dict[int, psutil.Process] = {}
    psutil.cpu_percent(None, percpu=True)
    while True:
        for proc in psutil.process_iter():
            if proc.pid not in procs:
                procs[proc.pid] = proc
                try:
                    proc.cpu_percent(None)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        procs = {pid: p for pid, p in procs.items() if p.is_running()}
        await asyncio.sleep(interval)
        throttled = read_throttled()
        per_core = psutil.cpu_percent(None, percpu=True)
        recorder.write("system", {
            "cpu_total": round(sum(per_core) / max(len(per_core), 1), 1),
            "cpu_per_core": per_core,
            "cpu_groups": group_cpu(procs),
            "mem_used_mb": round(psutil.virtual_memory().used / 2**20),
            "temp_c": read_temperature(),
            "throttled": throttled,
            "throttled_flags": decode_throttled(throttled),
        })
