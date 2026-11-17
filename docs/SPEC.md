# event-stage-screen — Specification

Status: agreed with the project owner on 2026-11-17. This document is the
reference for all milestones. Changes to the scope are recorded here first.

## 1. Goal

A Raspberry Pi turns into a stage display appliance for **eventrentmanager**
(ERM). Each of the Pi's two HDMI ports outputs an independent, freely
composed scene (similar to an OBS scene). Scenes combine ERM run-sheet
content, logos/images, a clock, text, colour areas and NDI video. Everything
is configured through a Bootstrap-based web application on the device.

## 2. Platform

| Topic | Decision |
|---|---|
| Hardware | Raspberry Pi 4 **and** Raspberry Pi 5 (model detected automatically) |
| OS | Raspberry Pi OS 64-bit (Trixie preferred, Bookworm supported) |
| Display stack | Minimal Wayland kiosk (sway as compositor), no desktop, no cursor, no screen blanking |
| Renderer | One Chromium kiosk window per HDMI output rendering a local scene page |
| Transformations | CSS transforms (GPU-composited) |
| NDI | GStreamer `ndisrc` (gst-plugins-rs) in a local bridge process, delivered to the scene page with low latency |
| NDI runtime | Downloaded by `install.sh` only after the user accepts the NDI license; never stored in the repo. Without it, NDI is disabled and everything else works |
| Backend | Python 3.11+, Starlette/uvicorn, Jinja2, SQLite, CQRS, port 8080 |
| Web UI | Bootstrap 5.3.3, dark theme, English source language + German catalog |
| Deployment | systemd units, idempotent `deploy/install.sh` |
| License | GPLv3 |

### Pi 4 limits

The Pi 4 has less headroom (software decoding of full-bandwidth NDI). By
default it allows **one** NDI source; the limit can be overridden in the
settings. The UI shows CPU load, temperature and throttling state.

> Decision change (milestone 3): the kiosk compositor is **sway** instead of
> labwc. labwc 0.7 (Bookworm) cannot move a window to a named output via
> window rules, and in `--app` mode Chromium ignores `--class` on Wayland (the
> app_id is derived from the URL). sway's `for_window [app_id=…] move to output`
> places each output's window deterministically. Both are wlroots compositors;
> `wlr-randr` is used for modes in either case.

## 3. Outputs

- Per HDMI port: real video mode (EDID modes + custom modes, e.g. for LED
  processors), refresh rate, output rotation 0/90/180/270°.
- Hotplug: a monitor connected later is detected and driven with the
  configured mode.
- **Mirror mode** (output 1 → output 2): output 2 renders the same scene with
  its own scaling (letterbox or stretch), its own mode, and optional extra
  rotation / horizontal / vertical flip (e.g. teleprompter). The scene is
  rendered a second time locally rather than copying frames, so latency stays
  identical.
- Boot splash until the first scene is live: configurable, default "logo +
  IP address + hostname". Never shown while a scene is active.
- Watchdog restarts crashed Chromium or bridge processes.
- Setup aids per output (web UI and API): test patterns (colour bars, grid,
  edge markers, resolution text), blackout, "identify output" (big number for
  a few seconds), live thumbnail of the real output in the web UI.

## 4. Scenes and editor

- Multiple scenes per output.
- Each scene has a fixed **base resolution** (like OBS) and is scaled to the
  output (letterbox or stretch). When creating a scene, the output's current
  resolution is suggested; the editor warns about aspect ratio mismatches.
- Switching: manual, or automatic by prioritised triggers:
  program `pending` / `running` / `finished`, "no program assigned",
  "ERM offline for more than X s", "urgent message active". A manual choice
  wins until the user switches back to "automatic".
- Transitions: cut, fade (configurable duration).

### Source types (v1)

- **ERM elements** (individually placeable): big countdown/remaining time,
  progress bar, current item (name, x/n), description, trade notes, next item
  ("in …"), delay badge, message bar, "no strobe" banner, item list,
  microphones, program/room/event info, online/offline state.
- **Full stage display** element as a quick start replicating the known
  ERM `/display` layout.
- **Logo / image** (PNG, SVG, JPG).
- **Clock**: digital (free format, e.g. `HH:mm`, `HH:mm:ss`, date) and
  analog. Time source selectable; default ERM server time with NTP fallback.
  Time zone configured system-wide.
- **NDI source**.
- **Text**.
- **Colour area / background**.

Later: web page (URL), looping video file, countdown to a fixed time.

Every element has its own font, colour and alignment settings.

### Transformations

Move, resize (with/without keeping aspect ratio), free rotation, horizontal /
vertical flip, crop, opacity, z-order, visibility, lock, snapping to grid and
edges, numeric input in a properties panel.

### Editor

- Visual canvas editor with handles plus properties panel (OBS-like).
- WYSIWYG: the editor uses the same render engine as the outputs. NDI shows
  as a still image (~1 fps).
- Studio mode: edits happen on a copy and go live with "Apply". A
  "live editing" switch allows quick fixes directly on air.
- Central media library per device (images, logos, custom fonts TTF/OTF/
  WOFF2). Bundled free fonts (e.g. Inter, JetBrains Mono for tabular digits).
- Scene export/import as JSON, optionally including the referenced media.

## 5. eventrentmanager integration

- Data source: ERM **Display API** `GET /api/display/v1/state` with a
  `mvd_…` bearer token. Polling interval taken from `pollIntervalSeconds`.
- One display client (token) **per output**; output 2 may alternatively
  mirror output 1.
- The local layout decides what is visible. ERM display options are ignored,
  except `notes[]`, which acts as the default trade filter of the trade-notes
  element (overridable locally).
- v1: tokens are entered manually. The UI is prepared for a later pairing-code
  flow, which will be added to eventrentmanager as a separate step
  (analogous to `POST /api/player/v1/pair`).

## 6. NDI

- Discovery via mDNS plus optional NDI discovery servers.
- No audio in v1.
- On signal loss (per element): freeze last frame, then transparent after
  5 s (default); alternatives: black, transparent, placeholder.

## 7. System

- External control: documented REST API with its own API key (separate from
  the admin login), e.g. for Bitfocus Companion / Stream Deck. OSC later.
- Settings in the web UI: hostname, network (DHCP/static, LAN/Wi-Fi via
  NetworkManager `nmcli`, confirmation + automatic rollback after 60 s),
  time zone and NTP servers, reboot/shutdown, restart outputs, system status
  (CPU, temperature, throttling, memory), live logs, full configuration
  backup/restore.
- Updates: re-running the idempotent `install.sh`, and an "update" button in
  the web UI based on GitHub releases (with a backup beforehand).
- Single local admin account (setup page on first start), session cookie,
  CSRF header, login rate limit — as in auto-event-play.

## 8. Milestones

1. **Prototype NDI + dual HDMI** on real hardware, producing a measurement
   report (see [`prototype/README.md`](../prototype/README.md)).
2. **Skeleton**: web UI, login, settings, installer, CI.
3. **Renderer and scene editor** with image, text, clock, colour area.
4. **ERM integration and ERM elements.**
5. **NDI.**
6. **Automation, API, setup aids, updates.**

Each milestone is committed and pushed separately; a pull request is only
opened on request.
