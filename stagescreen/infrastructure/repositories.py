"""Repositories: persistence of the write model in SQLite."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..domain.model import OUTPUT_IDS, MirrorScaling, MirrorSettings, OutputConfig, VideoMode
from .db import Database


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SettingsRepository:
    """Key/value settings stored as JSON."""

    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM settings WHERE key = ?", (key,))


class UserRepository:
    def __init__(self, db: Database):
        self.db = db

    def count(self) -> int:
        row = self.db.fetchone("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row else 0

    def get_hash(self, username: str) -> str | None:
        row = self.db.fetchone("SELECT password_hash FROM users WHERE username = ?", (username,))
        return row["password_hash"] if row else None

    def save(self, username: str, password_hash: str) -> None:
        self.db.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash",
            (username, password_hash, now_iso()),
        )

    def list(self) -> list[str]:
        return [r["username"] for r in self.db.fetchall("SELECT username FROM users ORDER BY username")]


class OutputRepository:
    """One row per HDMI output; the configuration is stored as JSON."""

    def __init__(self, db: Database):
        self.db = db

    def get(self, output_id: int) -> OutputConfig:
        row = self.db.fetchone("SELECT config FROM outputs WHERE id = ?", (output_id,))
        if not row:
            return OutputConfig.default(output_id)
        return _output_from_json(output_id, json.loads(row["config"]))

    def all(self) -> list[OutputConfig]:
        return [self.get(i) for i in OUTPUT_IDS]

    def save(self, cfg: OutputConfig) -> None:
        data = cfg.to_dict()
        data.pop("mode_label", None)
        self.db.execute(
            "INSERT INTO outputs (id, config) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET config = excluded.config",
            (cfg.id, json.dumps(data)),
        )


def _output_from_json(output_id: int, d: dict[str, Any]) -> OutputConfig:
    default = OutputConfig.default(output_id)
    mode = d.get("mode")
    mirror = d.get("mirror") or {}
    return OutputConfig(
        id=output_id,
        name=d.get("name") or default.name,
        connector=d.get("connector") or default.connector,
        enabled=bool(d.get("enabled", True)),
        mode=VideoMode(int(mode["width"]), int(mode["height"]), float(mode["refresh"])) if mode else None,
        custom_mode=bool(d.get("custom_mode", False)),
        rotation=int(d.get("rotation") or 0),
        mirror_of=d.get("mirror_of"),
        mirror=MirrorSettings(
            scaling=MirrorScaling(mirror.get("scaling", "letterbox")),
            rotation=int(mirror.get("rotation") or 0),
            flip_h=bool(mirror.get("flip_h", False)),
            flip_v=bool(mirror.get("flip_v", False)),
        ),
    )
