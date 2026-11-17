"""Runtime configuration from environment variables.

Everything the operator changes at runtime lives in the SQLite database and
is edited in the web UI. Only things needed *before* the database exists are
environment variables (see deploy/event-stage-screen.env).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HELPER = "/usr/local/lib/event-stage-screen/ess-helper"


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"
    # "sudo" runs privileged actions through the root helper; "null" only logs
    # them (development machines and tests).
    system_backend: str = "sudo"
    helper_path: str = DEFAULT_HELPER
    # Directory with DRM connectors (status/modes files), overridable for tests.
    drm_dir: Path = Path("/sys/class/drm")
    # Offer GStreamer test patterns as NDI sources (development and tests only).
    ndi_test_sources: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "stagescreen.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def restore_file(self) -> Path:
        """A backup uploaded for restore; applied on the next start."""
        return self.data_dir / "restore-pending.zip"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.log_dir, self.media_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> AppConfig:
        return cls(
            data_dir=Path(os.environ.get("ESS_DATA_DIR", "./data")).resolve(),
            host=os.environ.get("ESS_HOST", "0.0.0.0"),
            port=int(os.environ.get("ESS_PORT", "8080")),
            log_level=os.environ.get("ESS_LOG_LEVEL", "INFO"),
            system_backend=os.environ.get("ESS_SYSTEM_BACKEND", "sudo"),
            helper_path=os.environ.get("ESS_HELPER", DEFAULT_HELPER),
            drm_dir=Path(os.environ.get("ESS_DRM_DIR", "/sys/class/drm")),
            ndi_test_sources=os.environ.get("ESS_NDI_TEST_SOURCES") == "1",
        )
