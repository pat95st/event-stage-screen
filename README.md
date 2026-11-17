# event-stage-screen

Turns a Raspberry Pi 4 or 5 into a dual-output stage display for
eventrentmanager. Each HDMI port shows its own freely composed scene with
run-sheet content, logos, a clock, text and NDI video. Scenes are configured
in a Bootstrap-based web application on the device.

- Specification: [`docs/SPEC.md`](docs/SPEC.md)
- Architecture: [`docs/architecture.md`](docs/architecture.md)
- Milestone 1 (dual HDMI + NDI measurements): [`prototype/README.md`](prototype/README.md)

## Status

| Milestone | State |
|---|---|
| 1. Spike: dual HDMI + NDI | done, waiting for measurements on real hardware |
| 2. Skeleton: web UI, login, settings, installer, CI | done |
| 3. Renderer and scene editor | next |
| 4. eventrentmanager integration | open |
| 5. NDI | open |
| 6. Automation, API, setup aids, updates | open |

The web interface currently offers: dashboard (outputs and system status),
output settings (video mode incl. custom modes, rotation, mirroring of output 1
to output 2), network (DHCP/static, Wi-Fi, automatic rollback), system (host
name, time zone, NTP, NDI source limit, reboot/shutdown, backup/restore,
password, language) and live logs. English and German.

## Installation on the Raspberry Pi

Raspberry Pi OS 64-bit (Bookworm or Trixie):

```bash
git clone https://github.com/pat95st/event-stage-screen.git
```

```bash
cd event-stage-screen && sudo ./deploy/install.sh
```

Then open `http://<ip-address>:8080/` (or `http://<hostname>.local:8080/`) and
create the administrator account. Re-running `install.sh` updates an existing
installation; the data in `/var/lib/event-stage-screen` is kept.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
ESS_SYSTEM_BACKEND=null ESS_DATA_DIR=./data .venv/bin/python -m stagescreen
```

`ESS_SYSTEM_BACKEND=null` only logs privileged actions instead of executing
them. Run the tests and the linter:

```bash
.venv/bin/pytest
```

```bash
.venv/bin/ruff check stagescreen tests deploy/ess-helper
```

`scripts/fetch-vendor.sh` downloads Bootstrap for offline use; without it the
web UI loads Bootstrap from a CDN.

## License

GPLv3, see [`LICENSE`](LICENSE). NDI® is a registered trademark of Vizrt NDI AB.
The NDI SDK is not part of this repository; it is downloaded during
installation only after its license has been accepted.
