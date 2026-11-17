#!/usr/bin/env bash
# Run one measurement scenario in a labwc kiosk session.
#
#   ./run.sh <scenario> [duration_seconds]
#
# Must be started from the Pi's local console (tty), not from inside an
# existing desktop session. Environment overrides:
#   NDI_SOURCE      NDI source name used by scenarios with *_SOURCE=ndi
#   OUT1_NAME/OUT2_NAME  Wayland output names (default HDMI-A-1 / HDMI-A-2)
#   SPIKE_MATCH     identifier (default) or title — how labwc matches windows
#   SPIKE_PORT      spike server port (default 8090)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCENARIO="${1:?usage: run.sh <scenario> [duration_seconds]}"
DURATION="${2:-120}"
SCENARIO_FILE="$HERE/scenarios/$SCENARIO.env"
[[ -f "$SCENARIO_FILE" ]] || { echo "unknown scenario: $SCENARIO" >&2; exit 1; }

# Defaults, then the scenario file.
OUT1_NAME="${OUT1_NAME:-HDMI-A-1}"
OUT2_NAME="${OUT2_NAME:-HDMI-A-2}"
OUT1_ENABLED=1 OUT2_ENABLED=1
OUT1_MODE="" OUT2_MODE=""
for n in 1 2; do
  for key in TRANSPORT SOURCE FORMAT CODEC LOAD VROTATE VFLIP; do
    printf -v "OUT${n}_${key}" '%s' ""
  done
done
REQUIRES_NDI=0 REQUIRES_PI5=0
# shellcheck disable=SC1090
source "$SCENARIO_FILE"

if [[ "$REQUIRES_NDI" == 1 && -z "${NDI_SOURCE:-}" ]]; then
  echo "scenario $SCENARIO needs NDI_SOURCE (see: .venv/bin/python -m spike sources)" >&2
  exit 2
fi

PORT="${SPIKE_PORT:-8090}"
DATA_DIR="$HERE/data"
mkdir -p "$DATA_DIR"
RUNTIME="${XDG_RUNTIME_DIR:-/tmp}/ess-spike"
rm -rf "$RUNTIME" && mkdir -p "$RUNTIME/labwc"

# Optional NDI plugin/runtime installed by setup.sh --with-ndi.
if [[ -d "$HERE/.ndi/plugins" ]]; then
  export GST_PLUGIN_PATH="$HERE/.ndi/plugins${GST_PLUGIN_PATH:+:$GST_PLUGIN_PATH}"
fi
if [[ -d "$HERE/.ndi/lib" ]]; then
  export NDI_RUNTIME_DIR_V6="$HERE/.ndi/lib"
  export LD_LIBRARY_PATH="$HERE/.ndi/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

# Build the page query string for each output.
query_for() {
  local n="$1" t s f c l r v
  t="OUT${n}_TRANSPORT"; s="OUT${n}_SOURCE"; f="OUT${n}_FORMAT"; c="OUT${n}_CODEC"
  l="OUT${n}_LOAD"; r="OUT${n}_VROTATE"; v="OUT${n}_VFLIP"
  local source="${!s}"
  [[ "$source" == "ndi" ]] && source="$NDI_SOURCE"
  "$HERE/.venv/bin/python" - "${!t}" "$source" "${!f}" "${!c}" "${!l}" "${!r}" "${!v}" <<'PY'
import sys
from urllib.parse import urlencode
keys = ["transport", "source", "format", "codec", "load", "vrotate", "vflip"]
print(urlencode({k: v for k, v in zip(keys, sys.argv[1:]) if v}))
PY
}
export OUT1_QUERY OUT2_QUERY
OUT1_QUERY="$(query_for 1)"
OUT2_QUERY="$(query_for 2)"

# labwc configuration: one window rule per output.
match_attr="identifier"
[[ "${SPIKE_MATCH:-identifier}" == "title" ]] && match_attr="title"
cat > "$RUNTIME/labwc/rc.xml" <<EOF
<?xml version="1.0"?>
<labwc_config>
  <core><gap>0</gap></core>
  <windowRules>
    <windowRule ${match_attr}="ess-output-1*" serverDecoration="no">
      <action name="MoveToOutput" output="${OUT1_NAME}"/>
      <action name="ToggleFullscreen"/>
    </windowRule>
    <windowRule ${match_attr}="ess-output-2*" serverDecoration="no">
      <action name="MoveToOutput" output="${OUT2_NAME}"/>
      <action name="ToggleFullscreen"/>
    </windowRule>
  </windowRules>
</labwc_config>
EOF
: > "$RUNTIME/labwc/menu.xml"

# Start the spike server.
export SPIKE_DATA_DIR="$DATA_DIR" SPIKE_SCENARIO="$SCENARIO"
(cd "$HERE" && exec .venv/bin/python -m spike serve --port "$PORT") >"$DATA_DIR/server-$SCENARIO.log" 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
  curl -fs "http://127.0.0.1:$PORT/api/environment" >/dev/null && break
  sleep 0.2
done

echo "scenario $SCENARIO for ${DURATION}s"
echo "  output 1 ($OUT1_NAME, ${OUT1_MODE:-auto}): ${OUT1_QUERY:-scene only}"
echo "  output 2 ($OUT2_NAME, ${OUT2_MODE:-auto}): ${OUT2_QUERY:-scene only}"

export HERE RUNTIME PORT DURATION SCENARIO DATA_DIR
export OUT1_NAME OUT2_NAME OUT1_MODE OUT2_MODE OUT1_ENABLED OUT2_ENABLED
labwc -C "$RUNTIME/labwc" -s "$HERE/kiosk/session.sh" >"$DATA_DIR/labwc-$SCENARIO.log" 2>&1 || true
echo "done. Create the report with: .venv/bin/python -m spike report"
