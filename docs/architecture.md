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
  kiosk/           kiosk agent running inside the sway session on the Pi (wlr-randr, Chromium)
  container.py     composition root (wires everything)
deploy/            install.sh, systemd units, env file, root helper, sudoers rule, kiosk session script
```

## Request flow

- `GET /api/...` → query → query handler → plain dict → JSON `{"ok": true, "result": ...}`.
- `POST /api/...` → command → command handler → JSON. Validation errors raise
  `DomainError` (translatable `Msg`), returned as `{"error": "..."}` with HTTP 4xx.
- Writing requests need the header `X-ESS: 1` (CSRF protection) and a valid session.
- `GET /api/stream` is a Server-Sent-Events stream (`status`, `changed`, `log`).

## Scenes and rendering

A scene is a JSON document (see `stagescreen/domain/scene.py`): base
resolution, background, scaling (letterbox/stretch), transition (cut/fade) and
an ordered list of elements (`text`, `image`, `clock`, `color`). Each element
has a box (`x`, `y`, `w`, `h` in base pixels), `rotation`, `flip_h`/`flip_v`,
`opacity`, `crop` (fractions per side), `visible`, `locked`, `lock_aspect`
and type-specific `props`. `normalize_scene` validates every document before
it is stored.

Every scene has a **live** version (what the output shows) and an optional
**draft** (studio mode). The editor autosaves the draft; *Apply* publishes it.
With *live editing* every change is published directly. Each output has one
active scene (`scene.active.<n>`); output 2 in mirror mode shows output 1's
active scene with its own scaling, rotation and flipping.

`web/static/js/scene-render.js` renders scenes in the browser. The same code is
used by the kiosk render page (`/render/<n>`) and the editor canvas, so the
editor is WYSIWYG. Render pages fetch `/api/render/<n>/state` and follow
changes through `/api/render/stream` (SSE).

Media (images, fonts) live in `<data>/media`; types are detected from the file
content. SVGs are served with a restrictive Content-Security-Policy.

## Kiosk

`event-stage-screen-kiosk.service` opens a logind session on tty1 for the
user `ess` and runs `deploy/kiosk-session.sh`, which starts **sway** with a
generated configuration. Inside the session `python -m stagescreen.kiosk`
(the kiosk agent) loops every 2 seconds:

1. `GET /api/kiosk/config` – desired outputs (connector, enabled, mode, rotation).
2. Apply modes, rotation and a side-by-side layout with `wlr-randr`
   (listed modes first, `--custom-mode` otherwise; rejected modes are not retried
   for 60 s).
3. Write sway rules that move each output's Chromium window full screen to its
   connector (`for_window [app_id="__render_<n>-"] …`) and keep one Chromium
   `--app` window per enabled, connected output; crashed browsers are restarted
   with back-off, unplugged outputs stop their browser, re-plugged ones start it again.
4. `POST /api/kiosk/report` – detected outputs and modes (shown in the web UI).

The agent and the render pages authenticate with the **kiosk token**
(`<data>/kiosk.token`, header `X-ESS-Kiosk` or `?k=`). It grants read access
to render state, media and fonts and to the kiosk endpoints only – never to
the admin API or to writes.

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
