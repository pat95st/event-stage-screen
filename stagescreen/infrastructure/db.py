"""SQLite access with simple, ordered schema migrations."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger("ess.db")

MIGRATIONS: list[str] = [
    # 1: base schema
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE users (
        username      TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        created_at    TEXT NOT NULL
    );
    CREATE TABLE outputs (
        id     INTEGER PRIMARY KEY,
        config TEXT NOT NULL
    );
    """,
    # 2: scenes and media library
    """
    CREATE TABLE scenes (
        id         TEXT PRIMARY KEY,
        output_id  INTEGER NOT NULL,
        name       TEXT NOT NULL,
        sort       INTEGER NOT NULL DEFAULT 0,
        live       TEXT NOT NULL,
        draft      TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX scenes_output ON scenes(output_id, sort);
    CREATE TABLE media (
        id         TEXT PRIMARY KEY,
        kind       TEXT NOT NULL,
        name       TEXT NOT NULL,
        family     TEXT,
        filename   TEXT NOT NULL,
        mime       TEXT NOT NULL,
        size       INTEGER NOT NULL,
        sha256     TEXT NOT NULL,
        width      INTEGER,
        height     INTEGER,
        created_at TEXT NOT NULL
    );
    """,
]


class Database:
    """Thread-safe wrapper around one SQLite connection (WAL mode)."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")

    @property
    def version(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> None:
        with self._lock:
            version = self.version
            for index, sql in enumerate(MIGRATIONS, start=1):
                if index <= version:
                    continue
                log.info("Applying database migration %d", index)
                self._conn.execute("BEGIN")
                try:
                    for statement in _split_sql(sql):
                        self._conn.execute(statement)
                    self._conn.execute(f"PRAGMA user_version = {index}")
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def fetchall(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def fetchone(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def backup_to(self, target: Path) -> None:
        """Write a consistent copy of the database to ``target``."""
        with self._lock:
            dest = sqlite3.connect(str(target))
            try:
                self._conn.backup(dest)
            finally:
                dest.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _split_sql(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]
