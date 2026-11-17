"""Backup and restore of the complete configuration.

A backup is a ZIP file containing ``manifest.json``, a consistent copy of the
SQLite database (``stagescreen.db``) and the media library (``media/``).

Restoring happens in two steps so the running service never swaps its open
database: the uploaded file is validated and stored as
``restore-pending.zip``; the service then restarts (systemd ``Restart=always``)
and :func:`apply_pending_restore` replaces the data before anything is opened.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path, PurePosixPath

from .. import __version__
from ..application.bus import DomainError
from ..config import AppConfig
from .db import MIGRATIONS, Database

log = logging.getLogger("ess.backup")

FORMAT = "event-stage-screen-backup"
FORMAT_VERSION = 1
MAX_BACKUP_BYTES = 512 * 2**20


def create_backup(config: AppConfig, db: Database) -> bytes:
    buf = io.BytesIO()
    tmp = config.tmp_dir / f"backup-{time.time_ns()}.db"
    try:
        db.backup_to(tmp)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps({
                "format": FORMAT,
                "format_version": FORMAT_VERSION,
                "app_version": __version__,
                "schema_version": db.version,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }, indent=2))
            zf.write(tmp, "stagescreen.db")
            for path in sorted(config.media_dir.rglob("*")):
                if path.is_file():
                    zf.write(path, "media/" + path.relative_to(config.media_dir).as_posix())
    finally:
        tmp.unlink(missing_ok=True)
    return buf.getvalue()


def validate_backup(data: bytes) -> dict:
    """Check an uploaded backup; returns its manifest or raises DomainError."""
    if len(data) > MAX_BACKUP_BYTES:
        raise DomainError("The backup file is too large", 413)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise DomainError("This is not a backup file") from None
    with zf:
        names = zf.namelist()
        if "manifest.json" not in names or "stagescreen.db" not in names:
            raise DomainError("This is not a backup file")
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except ValueError:
            raise DomainError("This is not a backup file") from None
        if manifest.get("format") != FORMAT:
            raise DomainError("This is not a backup file")
        if int(manifest.get("format_version", 0)) > FORMAT_VERSION or int(manifest.get("schema_version", 0)) > len(MIGRATIONS):
            raise DomainError("The backup was created by a newer version – please update first")
        for name in names:
            p = PurePosixPath(name)
            if p.is_absolute() or ".." in p.parts or not (name in ("manifest.json", "stagescreen.db") or name.startswith("media/")):
                raise DomainError("The backup file contains invalid paths")
    return manifest


def store_pending_restore(config: AppConfig, data: bytes) -> dict:
    manifest = validate_backup(data)
    config.restore_file.write_bytes(data)
    log.warning("Backup from %s stored – it will be restored on restart", manifest.get("created_at"))
    return manifest


def apply_pending_restore(config: AppConfig) -> bool:
    """Replace database and media with a pending backup (called before startup)."""
    path = config.restore_file
    if not path.exists():
        return False
    try:
        data = path.read_bytes()
        validate_backup(data)
        staging = config.tmp_dir / "restore"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(staging)
        db_file = staging / "stagescreen.db"
        conn = sqlite3.connect(str(db_file))
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise DomainError("The database in the backup is damaged")
        finally:
            conn.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(config.db_path) + suffix).unlink(missing_ok=True)
        shutil.move(str(db_file), config.db_path)
        shutil.rmtree(config.media_dir, ignore_errors=True)
        if (staging / "media").exists():
            shutil.move(str(staging / "media"), config.media_dir)
        config.media_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        log.warning("Configuration restored from backup")
        return True
    except Exception:
        log.exception("Restoring the backup failed – keeping the current configuration")
        return False
    finally:
        path.unlink(missing_ok=True)
