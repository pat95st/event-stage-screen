# Architecture

event-stage-screen follows the structure of `pat95st/auto-event-play`.

```
stagescreen/
  domain/          pure model and validation (outputs, video modes, network, host name)
  application/     CQRS: commands + command handlers (write), queries + query handlers (read), buses
  infrastructure/  SQLite, repositories, auth, logging, SSE hub, system info, DRM detection,
                   NetworkManager, backup, privileged runner
  i18n/            English source strings, German catalog (de.py)
  web/             Starlette app, Jinja2 templates, static JS/CSS (Bootstrap 5.3.3, dark)
  container.py     composition root (wires everything)
deploy/            install.sh, systemd unit, env file, root helper, sudoers rule
```

## Request flow

- `GET /api/...` → query → query handler → plain dict → JSON `{"ok": true, "result": ...}`.
- `POST /api/...` → command → command handler → JSON. Validation errors raise
  `DomainError` (translatable `Msg`), returned as `{"error": "..."}` with HTTP 4xx.
- Writing requests need the header `X-ESS: 1` (CSRF protection) and a valid session.
- `GET /api/stream` is a Server-Sent-Events stream (`status`, `changed`, `log`).

## Privileged actions

The web service runs as the unprivileged system user `ess`. Changing the host
name, time zone, NTP servers or network and rebooting need root. These go
through `deploy/ess-helper`, installed root-owned to
`/usr/local/lib/event-stage-screen/ess-helper` and allowed for `ess` via a
single sudoers rule. The helper accepts a fixed set of actions and validates
every argument again; it is the security boundary, independent of the web app.

With `ESS_SYSTEM_BACKEND=null` (development, tests) the actions are only logged.

## Network changes with automatic rollback

Before each change the helper creates a NetworkManager checkpoint
(`CheckpointCreate`, rollback timeout 60 s). The change is applied in the
background after the HTTP response has been sent. The user confirms in the web
UI (`CheckpointDestroy`); if the device became unreachable and nobody confirms,
NetworkManager restores the previous configuration by itself.

## Backup and restore

A backup is a ZIP with `manifest.json`, a consistent SQLite copy and the media
library. A restore is validated, stored as `restore-pending.zip`, and the
service restarts (systemd `Restart=always`); the data is swapped before the
database is opened again.

## Configuration

Environment variables (`/etc/event-stage-screen.env`):

| Variable | Default | Meaning |
|---|---|---|
| `ESS_DATA_DIR` | `./data` | Database, logs, media (`/var/lib/event-stage-screen` on the Pi) |
| `ESS_HOST` / `ESS_PORT` | `0.0.0.0` / `8080` | Web server |
| `ESS_SYSTEM_BACKEND` | `sudo` | `sudo` (root helper) or `null` (simulate) |
| `ESS_HELPER` | `/usr/local/lib/event-stage-screen/ess-helper` | Path of the root helper |
| `ESS_DRM_DIR` | `/sys/class/drm` | DRM sysfs (HDMI connector detection) |
| `ESS_LOG_LEVEL` | `INFO` | Log level |

Everything else is stored in SQLite and edited in the web UI.
