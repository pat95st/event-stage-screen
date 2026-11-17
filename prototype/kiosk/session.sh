#!/usr/bin/env bash
# Started by labwc (-s) from run.sh: applies output modes, launches one
# Chromium window per output, waits for the scenario duration, then exits.
set -uo pipefail

log() { echo "[session] $*"; }

chromium_bin() {
  for bin in chromium chromium-browser; do
    command -v "$bin" >/dev/null && { echo "$bin"; return; }
  done
  echo "chromium not found" >&2
  exit 1
}

# Accepts WIDTHxHEIGHT@RATE; tries an EDID mode first, then a custom mode.
apply_mode() {
  local output="$1" mode="$2"
  [[ -z "$mode" ]] && return 0
  [[ "$mode" == *Hz ]] || mode="${mode}Hz"
  if wlr-randr --output "$output" --on --mode "$mode" 2>/dev/null; then
    log "$output: mode $mode"
  elif wlr-randr --output "$output" --on --custom-mode "$mode"; then
    log "$output: custom mode $mode"
  else
    log "$output: failed to set $mode"
  fi
}

wlr-randr > "$DATA_DIR/outputs-before-$SCENARIO.txt" 2>&1
apply_mode "$OUT1_NAME" "$OUT1_MODE"
apply_mode "$OUT2_NAME" "$OUT2_MODE"
sleep 1
wlr-randr > "$DATA_DIR/outputs-after-$SCENARIO.txt" 2>&1

CHROMIUM="$(chromium_bin)"
launch() {
  local n="$1" query="$2"
  "$CHROMIUM" \
    --ozone-platform=wayland \
    --class="ess-output-$n" \
    --user-data-dir="$RUNTIME/chromium-$n" \
    --app="http://127.0.0.1:$PORT/output/$n?$query" \
    --no-first-run --noerrdialogs --disable-infobars \
    --disable-session-crashed-bubble --password-store=basic \
    --autoplay-policy=no-user-gesture-required \
    --disable-features=WebRtcHideLocalIpsWithMdns,Translate \
    --enable-gpu-rasterization --ignore-gpu-blocklist \
    --check-for-update-interval=31536000 \
    >"$DATA_DIR/chromium-$n-$SCENARIO.log" 2>&1 &
  log "launched output $n: $query"
}

[[ "$OUT1_ENABLED" == 1 ]] && launch 1 "${OUT1_QUERY:-}"
[[ "$OUT2_ENABLED" == 1 ]] && launch 2 "${OUT2_QUERY:-}"

sleep "$DURATION"
log "duration reached, exiting"
pkill -f "user-data-dir=$RUNTIME/chromium-" || true
sleep 1
labwc --exit 2>/dev/null || kill "${LABWC_PID:-0}" 2>/dev/null || true
