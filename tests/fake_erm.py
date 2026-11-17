"""A fake eventrentmanager Display API server for tests and local development.

    python -m tests.fake_erm --port 8099 --token mvd_test_token_1234

serves GET /api/display/v1/state with a run sheet whose current item started
a minute ago, so countdowns and the delay badge are live.
"""

from __future__ import annotations

import argparse
import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def sample_state(now: datetime | None = None, *, finished: bool = False, urgent: bool = False) -> dict[str, Any]:
    now = now or datetime.now(UTC).astimezone()
    start = now - timedelta(minutes=21)

    def item(pos, name, offset_min, dur, status, **extra):
        s = start + timedelta(minutes=offset_min)
        return {"id": f"i{pos}", "position": pos, "name": name, "description": extra.get("description"),
                "notes": extra.get("notes", {"general": None, "sound": None, "light": None, "video": None, "stage": None}),
                "tradeNotes": extra.get("tradeNotes", []), "noStrobe": extra.get("noStrobe", False),
                "durationMinutes": dur, "status": status, "plannedStart": iso(s), "plannedEnd": iso(s + timedelta(minutes=dur)),
                "currentStart": iso(s), "projectedStart": iso(s), "projectedEnd": iso(s + timedelta(minutes=dur)),
                "actualStart": iso(s) if status in ("done", "current") else None}

    items = [
        item(1, "Einlass", 0, 15, "done"),
        item(2, "Begrüßung", 20, 5, "current", description="Moderation begrüßt die Gäste",
             notes={"general": "Pult links", "sound": "HH 1", "light": None, "video": None, "stage": "Banner"},
             tradeNotes=[{"tradeId": "t1", "trade": "Ton", "color": "#3b82f6", "text": "HH 1"},
                         {"tradeId": "t2", "trade": "Bühne", "color": "#a855f7", "text": "Banner"},
                         {"tradeId": "t3", "trade": "Rigging", "color": "#f59e0b", "text": "Truss 2 fährt"}],
             noStrobe=True),
        item(3, "Keynote", 25, 30, "next"),
        item(4, "Pause", 55, 20, "upcoming"),
    ]

    def block(i):
        s = datetime.fromisoformat(i["projectedStart"])
        e = s + timedelta(minutes=i["durationMinutes"])
        return {**i, "durationSeconds": i["durationMinutes"] * 60, "startsAt": iso(s), "endsAt": iso(e),
                "elapsedSeconds": int((now - s).total_seconds()), "remainingSeconds": int((e - now).total_seconds())}

    return {
        "apiVersion": 1,
        "serverTime": iso(now),
        "pollIntervalSeconds": 2,
        "display": {
            "id": "d1", "name": "Bühnenmonitor links", "role": "stage", "roleLabel": "Stagedisplay", "canControl": False,
            "assignment": {"mode": "rental", "rentalId": "r1", "minuteProgramId": None, "room": "Großer Saal"},
            "options": {"notes": ["general", "stage"], "showClock": True, "showDelay": True, "showNext": True,
                        "showItemList": False, "showDescription": True, "showMics": False, "allowControl": False,
                        "theme": "dark", "fontScale": 100},
            "thresholds": {"warnSeconds": 120, "criticalSeconds": 30},
            "configRevision": "4f0c2a9e1b7d3c55",
        },
        "runSheet": {
            "apiVersion": 1, "serverTime": iso(now), "revision": "rev1",
            "event": {"name": "Firmengala 2026", "noStrobe": False},
            "program": {"id": "p1", "name": "Abendprogramm", "room": "Großer Saal", "plannedStart": iso(start),
                        "currentStart": iso(start), "plannedEnd": iso(start + timedelta(minutes=75)),
                        "projectedEnd": iso(start + timedelta(minutes=76)), "jingleBefore": False, "jingleTitle": None,
                        "handheldMicCount": 4, "headsetMicCount": 2, "itemCount": len(items)},
            "trades": [{"id": "t1", "name": "Ton", "color": "#3b82f6"}],
            "live": {"state": "finished" if finished else "running", "tracking": True, "delaySeconds": 60,
                     "projectedDelaySeconds": 60, "overrunSeconds": 0, "finishedAt": None, "canControl": False},
            "current": block(items[1]), "next": block(items[2]), "items": items,
            "messages": [{"id": "m1", "text": "Redner kommt 5 Minuten später", "urgent": urgent, "stage": True,
                          "foh": True, "allPrograms": True, "sentAt": iso(now), "sentBy": "Veranstalter",
                          "fromFoh": False, "acknowledgedAt": None, "acknowledgedBy": None}],
        },
    }


class FakeErm:
    """Threaded fake server; ``state`` (callable or dict) is returned for valid tokens."""

    def __init__(self, token: str = "mvd_test_token_1234", port: int = 0):
        self.token = token
        self.state = sample_state
        self.status_override: int | None = None
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def _json(self, code, data):
                body = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.requests.append({"path": self.path, "auth": self.headers.get("Authorization")})
                if not self.path.startswith("/api/display/v1/state"):
                    return self._json(404, {"ok": False, "error": "Not found"})
                if fake.status_override:
                    return self._json(fake.status_override, {"ok": False, "error": "Server error"})
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    return self._json(401, {"ok": False, "error": "Ungültiger oder deaktivierter Display-Token."})
                state = fake.state() if callable(fake.state) else fake.state
                return self._json(200, state)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> FakeErm:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--token", default="mvd_test_token_1234")
    args = parser.parse_args()
    with FakeErm(args.token, args.port) as fake:
        print(f"fake eventrentmanager on {fake.url} (token {fake.token})")
        fake.thread.join()
