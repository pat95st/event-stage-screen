#!/usr/bin/env bash
# Downloads Bootstrap and the bundled fonts (Inter, JetBrains Mono; SIL Open
# Font License) into the static folder so the web UI and the outputs work
# offline (events often have no internet). Run once after checkout / on install.
set -euo pipefail
BOOTSTRAP_VERSION="${BOOTSTRAP_VERSION:-5.3.3}"
INTER_VERSION="${INTER_VERSION:-5.0.18}"
JETBRAINS_VERSION="${JETBRAINS_VERSION:-5.0.20}"
STATIC="$(cd "$(dirname "$0")/.." && pwd)/stagescreen/web/static/vendor"
mkdir -p "$STATIC/bootstrap" "$STATIC/fonts"

base="https://cdn.jsdelivr.net/npm/bootstrap@${BOOTSTRAP_VERSION}/dist"
curl -fsSL "$base/css/bootstrap.min.css" -o "$STATIC/bootstrap/bootstrap.min.css"
curl -fsSL "$base/js/bootstrap.bundle.min.js" -o "$STATIC/bootstrap/bootstrap.bundle.min.js"
echo "Bootstrap ${BOOTSTRAP_VERSION} -> $STATIC/bootstrap"

for weight in 400 700; do
  curl -fsSL "https://cdn.jsdelivr.net/npm/@fontsource/inter@${INTER_VERSION}/files/inter-latin-${weight}-normal.woff2" \
    -o "$STATIC/fonts/inter-latin-${weight}-normal.woff2"
  curl -fsSL "https://cdn.jsdelivr.net/npm/@fontsource/jetbrains-mono@${JETBRAINS_VERSION}/files/jetbrains-mono-latin-${weight}-normal.woff2" \
    -o "$STATIC/fonts/jetbrains-mono-latin-${weight}-normal.woff2"
done
echo "Fonts -> $STATIC/fonts"
