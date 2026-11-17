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


class SceneRepository:
    """Scenes per output. ``live`` is what the output shows, ``draft`` the edited copy."""

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _row(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": r["id"], "output_id": r["output_id"], "name": r["name"], "sort": r["sort"],
            "live": json.loads(r["live"]), "draft": json.loads(r["draft"]) if r["draft"] else None,
            "updated_at": r["updated_at"],
        }

    def get(self, scene_id: str) -> dict[str, Any] | None:
        row = self.db.fetchone("SELECT * FROM scenes WHERE id = ?", (scene_id,))
        return self._row(row) if row else None

    def list(self, output_id: int | None = None) -> list[dict[str, Any]]:
        if output_id is None:
            rows = self.db.fetchall("SELECT * FROM scenes ORDER BY output_id, sort, name")
        else:
            rows = self.db.fetchall("SELECT * FROM scenes WHERE output_id = ? ORDER BY sort, name", (output_id,))
        return [self._row(r) for r in rows]

    def create(self, scene_id: str, output_id: int, name: str, content: dict[str, Any]) -> None:
        row = self.db.fetchone("SELECT COALESCE(MAX(sort), -1) + 1 AS n FROM scenes WHERE output_id = ?", (output_id,))
        self.db.execute(
            "INSERT INTO scenes (id, output_id, name, sort, live, draft, updated_at) VALUES (?, ?, ?, ?, ?, NULL, ?)",
            (scene_id, output_id, name, row["n"] if row else 0, json.dumps(content), now_iso()),
        )

    def save_draft(self, scene_id: str, draft: dict[str, Any] | None) -> None:
        self.db.execute("UPDATE scenes SET draft = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(draft) if draft is not None else None, now_iso(), scene_id))

    def publish(self, scene_id: str, content: dict[str, Any]) -> None:
        self.db.execute("UPDATE scenes SET live = ?, draft = NULL, updated_at = ? WHERE id = ?",
                        (json.dumps(content), now_iso(), scene_id))

    def rename(self, scene_id: str, name: str) -> None:
        self.db.execute("UPDATE scenes SET name = ?, updated_at = ? WHERE id = ?", (name, now_iso(), scene_id))

    def reorder(self, output_id: int, ids: list[str]) -> None:
        with self.db.transaction() as conn:
            for index, scene_id in enumerate(ids):
                conn.execute("UPDATE scenes SET sort = ? WHERE id = ? AND output_id = ?", (index, scene_id, output_id))

    def delete(self, scene_id: str) -> None:
        self.db.execute("DELETE FROM scenes WHERE id = ?", (scene_id,))


class MediaRepository:
    def __init__(self, db: Database):
        self.db = db

    def add(self, item: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO media (id, kind, name, family, filename, mime, size, sha256, width, height, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (item["id"], item["kind"], item["name"], item.get("family"), item["filename"], item["mime"],
             item["size"], item["sha256"], now_iso()),
        )

    def get(self, media_id: str) -> dict[str, Any] | None:
        return self.db.fetchone("SELECT * FROM media WHERE id = ?", (media_id,))

    def by_sha(self, sha256: str) -> dict[str, Any] | None:
        return self.db.fetchone("SELECT * FROM media WHERE sha256 = ?", (sha256,))

    def list(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            return self.db.fetchall("SELECT * FROM media WHERE kind = ? ORDER BY name", (kind,))
        return self.db.fetchall("SELECT * FROM media ORDER BY kind, name")

    def rename(self, media_id: str, name: str, family: str | None) -> None:
        self.db.execute("UPDATE media SET name = ?, family = COALESCE(?, family) WHERE id = ?", (name, family, media_id))

    def delete(self, media_id: str) -> None:
        self.db.execute("DELETE FROM media WHERE id = ?", (media_id,))
