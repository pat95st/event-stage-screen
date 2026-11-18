"""Software updates from GitHub releases.

The web service only *checks* for a newer release and creates a backup. The
installation itself is done by the root helper (``ess-helper update TAG``):
it downloads the tagged source archive of the official repository itself and
runs its ``deploy/install.sh`` detached via ``systemd-run``. The web service
can therefore only choose a version tag – it can never hand code to the
helper.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .. import __version__
from ..application.bus import DomainError

log = logging.getLogger("ess.updates")

DEFAULT_REPO = "pat95st/event-stage-screen"
TAG_RE = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
KEEP_BACKUPS = 5


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    m = TAG_RE.fullmatch((text or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def check_latest(repo: str = DEFAULT_REPO, current: str = __version__, timeout: float = 10) -> dict[str, Any]:
    """Ask GitHub for the latest release. Blocking."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"event-stage-screen/{current}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = json.loads(res.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"current": current, "latest": None, "update_available": False, "checked_at": time.time()}
        raise DomainError(f"GitHub: HTTP {exc.code}", 502) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DomainError(f"GitHub not reachable: {getattr(exc, 'reason', exc)}", 502) from None
    tag = str(data.get("tag_name") or "")
    latest, cur = parse_version(tag), parse_version(current)
    return {
        "current": current,
        "latest": {"tag": tag, "name": data.get("name") or tag, "notes": str(data.get("body") or "")[:5000],
                   "published_at": data.get("published_at"), "url": data.get("html_url")} if latest else None,
        "update_available": bool(latest and cur and latest > cur),
        "checked_at": time.time(),
    }


def prune_backups(backup_dir: Path) -> None:
    files = sorted(backup_dir.glob("pre-update-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP_BACKUPS:]:
        old.unlink(missing_ok=True)


def list_backups(backup_dir: Path) -> list[dict[str, Any]]:
    if not backup_dir.exists():
        return []
    return [{"name": p.name, "size": p.stat().st_size, "created": p.stat().st_mtime}
            for p in sorted(backup_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)]
