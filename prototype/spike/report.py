"""Turn ``metrics.jsonl`` into a Markdown measurement report."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path


def _load(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _avg(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), 1) if values else None


def _min(values):
    values = [v for v in values if v is not None]
    return round(min(values), 1) if values else None


def _max(values):
    values = [v for v in values if v is not None]
    return round(max(values), 1) if values else None


def _fmt(value, suffix: str = "") -> str:
    return "–" if value is None else f"{value}{suffix}"


def browser_key(rec: dict) -> str:
    """Group browser samples by output and video configuration."""
    cfg = rec.get("config") or {}
    transport = cfg.get("transport", "none")
    label = f"{rec.get('output')} · {transport}"
    if transport == "webrtc":
        label += f"/{cfg.get('codec', 'h264')}"
    if transport != "none":
        label += f" · {cfg.get('source', 'test')} {cfg.get('format', '')}".rstrip()
    return label


def summarise(records: list[dict]) -> dict:
    """Aggregate records per run → {run: {...}}."""
    runs: dict[str, dict] = defaultdict(lambda: {"system": [], "browser": defaultdict(list), "env": None, "scenario": None})
    for rec in records:
        run = runs[rec.get("run", "?")]
        run["scenario"] = rec.get("scenario")
        kind = rec.get("kind")
        if kind == "environment":
            run["env"] = rec
        elif kind == "system":
            run["system"].append(rec)
        elif kind == "browser":
            run["browser"][browser_key(rec)].append(rec)

    result = {}
    for run_id, run in runs.items():
        sys_recs = run["system"]
        throttled = sorted({flag for r in sys_recs for flag in r.get("throttled_flags") or []})
        outputs = {}
        for output, recs in sorted(run["browser"].items()):
            video = [r.get("video") or {} for r in recs]
            outputs[output] = {
                "screen": (recs[-1].get("screen") or {}),
                "fps_avg": _avg([r.get("fps") for r in recs]),
                "fps_min": _min([r.get("fps") for r in recs]),
                "long_frames": sum(r.get("longFrames") or 0 for r in recs),
                "transport": next((v.get("transport") for v in video if v.get("transport")), None),
                "video_fps_avg": _avg([v.get("fps") for v in video]),
                "video_dropped": _max([v.get("framesDropped") for v in video]),
                "latency_avg_ms": _avg([v.get("latencyAvgMs") for v in video]),
                "latency_p95_ms": _max([v.get("latencyP95Ms") for v in video]),
                "jitter_buffer_ms": _avg([v.get("jitterBufferMs") for v in video]),
                "decode_ms": _avg([v.get("decodeMs") for v in video]),
            }
        groups = defaultdict(list)
        for r in sys_recs:
            for g, v in (r.get("cpu_groups") or {}).items():
                groups[g].append(v)
        result[run_id] = {
            "scenario": run["scenario"],
            "env": run["env"] or {},
            "samples": len(sys_recs),
            "cpu_avg": _avg([r.get("cpu_total") for r in sys_recs]),
            "cpu_max": _max([r.get("cpu_total") for r in sys_recs]),
            "cpu_groups_avg": {g: _avg(v) for g, v in groups.items()},
            "temp_max": _max([r.get("temp_c") for r in sys_recs]),
            "mem_max_mb": _max([r.get("mem_used_mb") for r in sys_recs]),
            "throttled": throttled,
            "outputs": outputs,
        }
    return result


def render_markdown(summary: dict) -> str:
    lines = ["# Spike measurement report", ""]
    env = next((s["env"] for s in summary.values() if s["env"]), {})
    if env:
        lines += [
            "## Device",
            "",
            f"- Model: {env.get('model')}",
            f"- OS: {env.get('os')} (kernel {env.get('kernel')})",
            f"- CPUs / RAM: {env.get('cpus')} / {env.get('memory_mb')} MB",
            f"- GStreamer: {env.get('gstreamer')}",
            f"- Elements: ndisrc={env.get('ndisrc')}, v4l2h264enc={env.get('v4l2h264enc')} "
            f"(device {env.get('v4l2_encoder_device')}), x264enc={env.get('x264enc')}",
            "",
        ]
    lines += [
        "## Overview",
        "",
        "| Run | Scenario | CPU avg/max % | Temp max °C | Throttling |",
        "|---|---|---|---|---|",
    ]
    for run_id, s in sorted(summary.items()):
        lines.append(
            f"| {run_id} | {s['scenario']} | {_fmt(s['cpu_avg'])} / {_fmt(s['cpu_max'])} "
            f"| {_fmt(s['temp_max'])} | {', '.join(s['throttled']) or 'none'} |"
        )
    lines.append("")
    for run_id, s in sorted(summary.items()):
        lines += [f"## {s['scenario']} ({run_id})", ""]
        groups = ", ".join(f"{g} {_fmt(v)}%" for g, v in s["cpu_groups_avg"].items())
        lines += [f"- CPU per process group (100% = one core): {groups or '–'}",
                  f"- Memory used max: {_fmt(s['mem_max_mb'], ' MB')}", ""]
        if not s["outputs"]:
            lines += ["_No browser metrics received._", ""]
            continue
        lines += [
            "| Output | Screen | Page fps avg/min | Long frames | Transport | Video fps | Dropped | Latency avg/p95 ms | Jitter buf ms | Decode ms |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for out, o in s["outputs"].items():
            scr = o["screen"]
            screen = f"{scr.get('width')}×{scr.get('height')}" if scr else "–"
            lines.append(
                f"| {out} | {screen} | {_fmt(o['fps_avg'])} / {_fmt(o['fps_min'])} | {o['long_frames']} "
                f"| {o['transport'] or '–'} | {_fmt(o['video_fps_avg'])} | {_fmt(o['video_dropped'])} "
                f"| {_fmt(o['latency_avg_ms'])} / {_fmt(o['latency_p95_ms'])} "
                f"| {_fmt(o['jitter_buffer_ms'])} | {_fmt(o['decode_ms'])} |"
            )
        lines.append("")
    lines += [
        "## Manual observations",
        "",
        "- Glass-to-glass latency (camera photo, page clock vs. burnt-in clock): ",
        "- Did each window land on the correct HDMI port? ",
        "- Were mode switches (resolution/refresh) applied correctly? ",
        "- Visible tearing, stutter or artefacts: ",
        "",
    ]
    return "\n".join(lines)


def write_report(metrics_path: Path, out_path: Path) -> Path:
    summary = summarise(_load(metrics_path))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(summary), encoding="utf-8")
    return out_path
