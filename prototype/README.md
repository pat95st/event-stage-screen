# Milestone 1 — Spike: dual HDMI + NDI on the Raspberry Pi

This prototype answers the questions that decide the render architecture
(see [`docs/SPEC.md`](../docs/SPEC.md)) before the real application is built:

1. Can a Pi 4 / Pi 5 drive **two independent Chromium kiosk windows**, one per
   HDMI port, with independently set **resolution and refresh rate**?
2. How smoothly does a representative scene render (CSS transforms, rotation,
   mirroring, animations) on both outputs at once?
3. Which way of getting **NDI video into the browser** works best on each
   model, in terms of latency, CPU load, dropped frames and temperature?
   - **MJPEG**: GStreamer decodes NDI → JPEG → custom framed HTTP stream →
     `createImageBitmap` → canvas. One shared pipeline for all viewers.
   - **WebRTC**: GStreamer decodes NDI → H.264 (Pi 4: hardware encoder,
     Pi 5: x264) or VP8 → `webrtcbin` → `<video>`. One pipeline per viewer.

This is throw-away code. It does not use the final project structure.

## What it consists of

| Path | Purpose |
|---|---|
| `spike/` | Python spike server (Starlette): output pages, MJPEG/WebRTC bridge, metrics recorder, report generator |
| `spike/static/output.*` | The scene rendered on each output, with frame/latency measurement |
| `kiosk/session.sh` | Runs inside sway: sets HDMI modes with `wlr-randr`, starts one Chromium per output |
| `run.sh` | Runs one scenario for a given duration |
| `all.sh` | Runs all scenarios and writes the report |
| `scenarios/*.env` | Scenario definitions (modes, transport, source, load) |
| `setup.sh` | Installs everything on the Pi (`--with-ndi` adds NDI) |

## Preparing the Pi

Requirements: Raspberry Pi 4 or 5, Raspberry Pi OS **64-bit** (Bookworm or
Trixie; Lite or Desktop), two monitors connected, network access.

```bash
git clone -b claude/vibrant-hamilton-x7vvsg https://github.com/pat95st/event-stage-screen.git
```

```bash
cd event-stage-screen/prototype && ./setup.sh --with-ndi
```

`--with-ndi` downloads the NDI SDK from the official NDI website. The NDI
installer shows the NDI license, which you have to accept. The GStreamer NDI
plugin is then built from `gst-plugins-rs` (this takes about 10–30 minutes on a
Pi 5 and longer on a Pi 4). Everything ends up in `prototype/.ndi/` (git-ignored).
Leave out `--with-ndi` to test only the dual-HDMI part.

Check which NDI sources the Pi can see. Any NDI sender works, e.g. OBS with
the DistroAV plugin, NDI Tools Test Patterns, or a PTZ camera:

```bash
./ndi-sources.sh
```

## Running the measurements

The kiosk session needs the screens, so start it **from the Pi's local
console** (keyboard on the Pi, text console). On a Desktop image, first stop
the desktop:

```bash
sudo systemctl stop lightdm
```

Then log in on the console (Ctrl+Alt+F1 if needed) and run all scenarios.
Each scenario runs for 120 s by default:

```bash
cd ~/event-stage-screen/prototype && NDI_SOURCE="EXACT NAME FROM ndi-sources.sh" ./all.sh
```

A single scenario can be run with `./run.sh <scenario> [seconds]`, e.g.
`./run.sh s1-dual-1080p60-scene 60`.

| Scenario | Description |
|---|---|
| `s1-dual-1080p60-scene` | Both outputs 1080p60, animated scene, no video (baseline) |
| `s2-dual-1080p60-test-mjpeg` | Output 1: local test source via MJPEG |
| `s3-dual-1080p60-test-webrtc` | Output 1: local test source via WebRTC/H.264 |
| `s4-ndi-mjpeg` | Output 1: NDI 1080p30 via MJPEG |
| `s5-ndi-webrtc` | Output 1: NDI 1080p30 via WebRTC/H.264 |
| `s6-ndi-mirror-mjpeg` | Both outputs show the same NDI source (output 2 at 720p, video flipped) |
| `s7-dual-4k30-scene` | Both outputs 4K30 (Pi 5 only) |

The output names can be changed with `OUT1_NAME`/`OUT2_NAME` (see `data/outputs-before-*.txt`).

### Glass-to-glass latency (manual, optional)

The test source burns the wall clock (`HH:MM:SS.mmm`) into the video, and
each output page shows the same clock top right. Take a photo of the monitor
during `s2`/`s3`; the difference between the two clocks is the latency from
capture to the screen. For NDI, a sender showing a millisecond clock (e.g. OBS
with a clock overlay) on a machine with synchronised time works the same way.

## Sending back the results

`all.sh` finishes by writing `reports/<hostname>-<date>.md`. Please send
back:

- the report (fill in the "Manual observations" section at the end),
- `data/outputs-before-*.txt` (available modes per port),
- any errors from `data/*.log`.

The simplest way is to commit `reports/` and the relevant log files on the
branch, or to paste them into the chat.

## Running locally (without a Pi)

The server and pages also run on any Linux machine with GStreamer:

```bash
python3 -m venv --system-site-packages .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/python -m spike serve
```

Then open <http://127.0.0.1:8090/>. Run the tests with `.venv/bin/python -m pytest`.
