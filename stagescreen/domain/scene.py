"""Scene model: validation and normalisation of scene documents.

A scene is stored as a JSON document (dict). ``normalize_scene`` is the single
gate every scene passes before it is stored: it fills defaults, coerces types
and rejects invalid values with translatable :class:`DomainError` messages.
The browser renderer (``web/static/js/scene-render.js``) relies on this shape.

Coordinates and sizes are in pixels of the scene's base resolution. ``x``/``y``
is the top-left corner of the unrotated element box; rotation happens around
the box centre. Crop values are fractions (0–0.95) of the source content.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from ..application.bus import DomainError
from ..i18n import Msg

ELEMENT_TYPES = ("text", "image", "clock", "color", "erm")
ERM_FIELDS = ("countdown", "progress", "current", "description", "notes", "next", "delay", "messages",
              "nostrobe", "itemlist", "mics", "program", "status")
NOTE_KEYS = ("general", "sound", "light", "video", "stage", "other")
# Extra properties per ERM field: name -> (kind, default, [min, max] | choices)
ERM_EXTRAS: dict[str, dict[str, tuple]] = {
    "countdown": {"warn_color": ("color", "#f59e0b"), "critical_color": ("color", "#ef4444"),
                  "show_pending": ("bool", True), "finished_text": ("text", "", 60)},
    "progress": {"bar_color": ("color", "#22c55e"), "track_color": ("color", "#2e333b"), "radius": ("num", 999, 0, 10000)},
    "current": {"show_position": ("bool", True), "label": ("text", "", 60)},
    "description": {},
    "notes": {"notes_filter": ("choice", "erm", ("erm", "all", "custom")), "notes_keys": ("keys", ["general", "stage"]),
              "show_labels": ("bool", True), "note_background": ("color", "transparent")},
    "next": {"label": ("text", "", 60), "show_time": ("bool", True), "show_in": ("bool", True)},
    "delay": {"on_time_color": ("color", "#d6dae0"), "late_color": ("color", "#ef4444"), "early_color": ("color", "#22c55e")},
    "messages": {"message_color": ("color", "#60a5fa"), "urgent_color": ("color", "#ef4444"), "max_messages": ("num", 3, 1, 10)},
    "nostrobe": {"label": ("text", "", 60)},
    "itemlist": {"max_rows": ("num", 8, 1, 50), "show_duration": ("bool", True), "show_done": ("bool", False),
                 "highlight_color": ("color", "#2fb4e8")},
    "mics": {},
    "program": {"template": ("text", "{program} · {room}", 200)},
    "status": {"show_online": ("bool", False), "label": ("text", "", 60)},
}
MAX_ELEMENTS = 200
COLOR_RE = re.compile(r"^(#([0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})|transparent)$")
FONT_RE = re.compile(r"^[\w .'-]{1,80}$", re.UNICODE)
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
CLOCK_FORMAT_RE = re.compile(r"^[^<>{}]{0,40}$")


def new_element_id() -> str:
    return "el_" + secrets.token_hex(5)


# ---------------------------------------------------------------- primitives

def _num(value: Any, default: float, lo: float, hi: float, label: str) -> float:
    if value is None or value == "":
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise DomainError(Msg("{field}: invalid number", field=label)) from None
    if v != v or not lo <= v <= hi:  # NaN or out of range
        raise DomainError(Msg("{field}: value out of range ({lo} to {hi})", field=label, lo=_fmt(lo), hi=_fmt(hi)))
    return round(v, 3)


def _int(value: Any, default: int, lo: int, hi: int, label: str) -> int:
    return int(round(_num(value, default, lo, hi, label)))


def _fmt(v: float) -> str:
    return f"{v:g}"


def _color(value: Any, default: str, label: str) -> str:
    if value in (None, ""):
        return default
    if not isinstance(value, str) or not COLOR_RE.fullmatch(value):
        raise DomainError(Msg('{field}: invalid colour "{value}"', field=label, value=str(value)[:20]))
    return value.lower()


def _choice(value: Any, choices: tuple[str, ...], default: str, label: str) -> str:
    if value in (None, ""):
        return default
    if value not in choices:
        raise DomainError(Msg("{field}: invalid value", field=label))
    return value


def _text(value: Any, default: str, max_len: int, label: str) -> str:
    if value is None:
        return default
    text = str(value)
    if len(text) > max_len:
        raise DomainError(Msg("{field}: at most {n} characters", field=label, n=max_len))
    return text


def _font(value: Any, default: str = "Inter") -> str:
    font = (value or default).strip() if isinstance(value, str) or value is None else default
    if not FONT_RE.fullmatch(font):
        raise DomainError(Msg('Invalid font name "{font}"', font=str(value)[:40]))
    return font


# ---------------------------------------------------------------- elements

def _common_text_props(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "font": _font(p.get("font")),
        "size": _num(p.get("size"), 72, 1, 2000, "Font size"),
        "weight": _int(p.get("weight"), 400, 100, 900, "Font weight"),
        "italic": bool(p.get("italic", False)),
        "color": _color(p.get("color"), "#ffffff", "Text colour"),
        "background": _color(p.get("background"), "transparent", "Background colour"),
        "align": _choice(p.get("align"), ("left", "center", "right"), "center", "Alignment"),
        "valign": _choice(p.get("valign"), ("top", "middle", "bottom"), "middle", "Vertical alignment"),
        "padding": _num(p.get("padding"), 0, 0, 1000, "Padding"),
        "shadow": bool(p.get("shadow", False)),
    }


def _props(kind: str, p: dict[str, Any]) -> dict[str, Any]:
    if kind == "text":
        return {
            **_common_text_props(p),
            "text": _text(p.get("text"), "Text", 2000, "Text"),
            "line_height": _num(p.get("line_height"), 1.2, 0.5, 3, "Line height"),
            "uppercase": bool(p.get("uppercase", False)),
        }
    if kind == "clock":
        fmt = _text(p.get("format"), "HH:mm:ss", 40, "Time format")
        if not CLOCK_FORMAT_RE.fullmatch(fmt):
            raise DomainError("Time format: invalid characters")
        return {
            **_common_text_props(p),
            "style": _choice(p.get("style"), ("digital", "analog"), "digital", "Clock style"),
            "format": fmt,
            "face_color": _color(p.get("face_color"), "#111111", "Face colour"),
            "hand_color": _color(p.get("hand_color"), "#ffffff", "Hand colour"),
            "second_color": _color(p.get("second_color"), "#e5484d", "Second hand colour"),
            "tick_color": _color(p.get("tick_color"), "#bbbbbb", "Tick colour"),
            "show_seconds": bool(p.get("show_seconds", True)),
            "time_source": _choice(p.get("time_source"), ("erm", "system"), "erm", "Time source"),
        }
    if kind == "erm":
        field = _choice(p.get("field"), ERM_FIELDS, "countdown", "ERM field")
        props = {**_common_text_props(p), "field": field,
                 "time_source": _choice(p.get("time_source"), ("erm", "system"), "erm", "Time source")}
        for key, spec in ERM_EXTRAS[field].items():
            kind_, default = spec[0], spec[1]
            value = p.get(key)
            if kind_ == "color":
                props[key] = _color(value, default, key)
            elif kind_ == "bool":
                props[key] = bool(default if value is None else value)
            elif kind_ == "num":
                props[key] = _num(value, default, spec[2], spec[3], key)
            elif kind_ == "text":
                props[key] = _text(value, default, spec[2], key)
            elif kind_ == "choice":
                props[key] = _choice(value, spec[2], default, key)
            elif kind_ == "keys":
                keys = value if isinstance(value, list) else default
                props[key] = [k for k in NOTE_KEYS if k in keys]
        return props
    if kind == "image":
        media_id = p.get("media_id") or None
        if media_id is not None and not (isinstance(media_id, str) and ID_RE.fullmatch(media_id)):
            raise DomainError("Invalid image reference")
        return {"media_id": media_id, "fit": _choice(p.get("fit"), ("contain", "cover", "fill"), "contain", "Fit")}
    # color area
    return {
        "color": _color(p.get("color"), "#2fb4e8", "Colour"),
        "radius": _num(p.get("radius"), 0, 0, 10000, "Corner radius"),
        "border_width": _num(p.get("border_width"), 0, 0, 1000, "Border width"),
        "border_color": _color(p.get("border_color"), "#ffffff", "Border colour"),
    }


def normalize_element(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise DomainError("Invalid element")
    kind = data.get("type")
    if kind not in ELEMENT_TYPES:
        raise DomainError("Unknown element type")
    el_id = data.get("id") or new_element_id()
    if not isinstance(el_id, str) or not ID_RE.fullmatch(el_id):
        raise DomainError("Invalid element id")
    crop = data.get("crop") or {}
    crop_n = {side: _num(crop.get(side), 0, 0, 0.95, "Crop") for side in ("top", "right", "bottom", "left")}
    if crop_n["left"] + crop_n["right"] > 0.95 or crop_n["top"] + crop_n["bottom"] > 0.95:
        raise DomainError("Crop: too much cropped")
    props = data.get("props") or {}
    if not isinstance(props, dict):
        raise DomainError("Invalid element")
    return {
        "id": el_id,
        "type": kind,
        "name": _text(data.get("name"), kind.capitalize(), 60, "Name").strip() or kind.capitalize(),
        "x": _num(data.get("x"), 0, -20000, 20000, "X"),
        "y": _num(data.get("y"), 0, -20000, 20000, "Y"),
        "w": _num(data.get("w"), 400, 1, 20000, "Width"),
        "h": _num(data.get("h"), 200, 1, 20000, "Height"),
        "rotation": _num(data.get("rotation"), 0, -360, 360, "Rotation"),
        "flip_h": bool(data.get("flip_h", False)),
        "flip_v": bool(data.get("flip_v", False)),
        "opacity": _num(data.get("opacity"), 1, 0, 1, "Opacity"),
        "visible": bool(data.get("visible", True)),
        "locked": bool(data.get("locked", False)),
        "lock_aspect": bool(data.get("lock_aspect", kind == "image")),
        "crop": crop_n,
        "props": _props(kind, props),
    }


# ---------------------------------------------------------------- scenes

def normalize_scene(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise DomainError("Invalid scene")
    elements = data.get("elements") or []
    if not isinstance(elements, list):
        raise DomainError("Invalid scene")
    if len(elements) > MAX_ELEMENTS:
        raise DomainError(Msg("A scene can contain at most {n} elements", n=MAX_ELEMENTS))
    normalized = [normalize_element(e) for e in elements]
    ids = [e["id"] for e in normalized]
    if len(ids) != len(set(ids)):
        raise DomainError("Duplicate element id")
    transition = data.get("transition") or {}
    return {
        "width": _int(data.get("width"), 1920, 320, 7680, "Width"),
        "height": _int(data.get("height"), 1080, 200, 4320, "Height"),
        "background": _color(data.get("background"), "#000000", "Background colour"),
        "scaling": _choice(data.get("scaling"), ("letterbox", "stretch"), "letterbox", "Scaling"),
        "transition": {
            "type": _choice(transition.get("type"), ("cut", "fade"), "fade", "Transition"),
            "duration_ms": _int(transition.get("duration_ms"), 500, 0, 5000, "Transition duration"),
        },
        "elements": normalized,
    }


def validate_scene_name(name: Any) -> str:
    name = str(name or "").strip()
    if not 1 <= len(name) <= 60:
        raise DomainError("Scene name: 1–60 characters")
    return name


def empty_scene(width: int = 1920, height: int = 1080) -> dict[str, Any]:
    return normalize_scene({"width": width, "height": height, "elements": []})


def media_ids(scene: dict[str, Any]) -> set[str]:
    """Media referenced by a scene (images)."""
    return {e["props"]["media_id"] for e in scene.get("elements", []) if e["type"] == "image" and e["props"].get("media_id")}


def fonts_used(scene: dict[str, Any]) -> set[str]:
    return {e["props"]["font"] for e in scene.get("elements", []) if e["type"] in ("text", "clock", "erm")}
