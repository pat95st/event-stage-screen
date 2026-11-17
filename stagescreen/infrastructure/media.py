"""Media library: uploaded images and fonts stored under ``<data>/media``.

File types are detected from their content (magic bytes), never trusted from
the file name. SVG images are served with a restrictive Content-Security-Policy
so that scripts inside an SVG can never run in the application's origin.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from ..application.bus import DomainError
from ..domain.scene import FONT_RE
from ..i18n import Msg

MAX_IMAGE_BYTES = 25 * 2**20
MAX_FONT_BYTES = 10 * 2**20

# Bundled fonts (downloaded by scripts/fetch-vendor.sh into static/vendor/fonts).
BUNDLED_FONTS = {
    "Inter": [("inter-latin-400-normal.woff2", 400, "normal"), ("inter-latin-700-normal.woff2", 700, "normal")],
    "JetBrains Mono": [("jetbrains-mono-latin-400-normal.woff2", 400, "normal"),
                       ("jetbrains-mono-latin-700-normal.woff2", 700, "normal")],
}
SYSTEM_FONTS = ["DejaVu Sans", "DejaVu Serif", "DejaVu Sans Mono", "sans-serif", "serif", "monospace"]

SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:"


@dataclass(frozen=True)
class DetectedType:
    kind: str  # image | font
    mime: str
    ext: str


def detect_type(data: bytes) -> DetectedType:
    head = data[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return DetectedType("image", "image/png", ".png")
    if head.startswith(b"\xff\xd8\xff"):
        return DetectedType("image", "image/jpeg", ".jpg")
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return DetectedType("image", "image/webp", ".webp")
    if head.startswith((b"GIF87a", b"GIF89a")):
        return DetectedType("image", "image/gif", ".gif")
    if head.startswith(b"wOF2"):
        return DetectedType("font", "font/woff2", ".woff2")
    if head.startswith(b"wOFF"):
        return DetectedType("font", "font/woff", ".woff")
    if head.startswith(b"OTTO"):
        return DetectedType("font", "font/otf", ".otf")
    if head.startswith((b"\x00\x01\x00\x00", b"true")):
        return DetectedType("font", "font/ttf", ".ttf")
    text = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text.startswith(b"<svg") or (text.startswith(b"<?xml") and b"<svg" in text) or (text.startswith(b"<!--") and b"<svg" in text):
        return DetectedType("image", "image/svg+xml", ".svg")
    raise DomainError("Unsupported file type (allowed: PNG, JPEG, WebP, GIF, SVG, TTF, OTF, WOFF, WOFF2)")


def family_from_name(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"[-_](regular|bold|italic|light|medium|black|semibold|thin|\d{3})+$", "", stem, flags=re.I)
    family = re.sub(r"[^\w .'-]+", " ", re.sub(r"[-_]+", " ", stem)).strip() or "Custom font"
    return family[:80]


class MediaStore:
    def __init__(self, media_dir: Path):
        self.dir = media_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, filename: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.[a-z0-9]+", filename):
            raise DomainError("Invalid file name")
        return self.dir / filename

    def save(self, data: bytes, name: str, family: str | None = None) -> dict:
        if not data:
            raise DomainError("The file is empty")
        detected = detect_type(data)
        limit = MAX_IMAGE_BYTES if detected.kind == "image" else MAX_FONT_BYTES
        if len(data) > limit:
            raise DomainError(Msg("The file is too large (maximum {mb} MB)", mb=limit // 2**20))
        media_id = "m_" + secrets.token_hex(8)
        filename = media_id + detected.ext
        (self.dir / filename).write_bytes(data)
        clean_name = re.sub(r"[\x00-\x1f]", "", Path(name or "file").name)[:120] or "file"
        if detected.kind == "font":
            family = (family or "").strip() or family_from_name(clean_name)
            if not FONT_RE.fullmatch(family):
                raise DomainError(Msg('Invalid font name "{font}"', font=family[:40]))
        else:
            family = None
        return {
            "id": media_id,
            "kind": detected.kind,
            "name": clean_name,
            "family": family,
            "filename": filename,
            "mime": detected.mime,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def delete(self, filename: str) -> None:
        self.path(filename).unlink(missing_ok=True)


def font_face_css(fonts: list[dict], bundled_dir: Path, media_url) -> str:
    """CSS @font-face rules for bundled and uploaded fonts.

    ``media_url`` maps a media id to its URL (adds the kiosk token if needed).
    """
    rules = []
    for family, files in BUNDLED_FONTS.items():
        for filename, weight, style in files:
            if (bundled_dir / filename).exists():
                rules.append(
                    f'@font-face {{ font-family: "{family}"; src: url("/static/vendor/fonts/{filename}") format("woff2"); '
                    f"font-weight: {weight}; font-style: {style}; font-display: block; }}"
                )
    formats = {"font/woff2": "woff2", "font/woff": "woff", "font/otf": "opentype", "font/ttf": "truetype"}
    for f in fonts:
        family = f["family"].replace('"', "")
        rules.append(
            f'@font-face {{ font-family: "{family}"; src: url("{media_url(f["id"])}") format("{formats.get(f["mime"], "truetype")}"); '
            "font-display: block; }"
        )
    return "\n".join(rules) + "\n"
