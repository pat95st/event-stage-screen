#!/usr/bin/env bash
# Run every scenario in sequence and write the Markdown report.
#
#   NDI_SOURCE="MY-PC (OBS)" ./all.sh [duration_seconds]
#
# Scenarios needing NDI are skipped when NDI_SOURCE is empty; 4K scenarios
# are skipped on a Raspberry Pi 4.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DURATION="${1:-120}"
MODEL="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)"

for file in "$HERE"/scenarios/*.env; do
  name="$(basename "$file" .env)"
  if grep -q '^REQUIRES_NDI=1' "$file" && [[ -z "${NDI_SOURCE:-}" ]]; then
    echo "skip $name (NDI_SOURCE not set)"; continue
  fi
  if grep -q '^REQUIRES_PI5=1' "$file" && [[ "$MODEL" == *"Pi 4"* ]]; then
    echo "skip $name (Pi 5 only)"; continue
  fi
  "$HERE/run.sh" "$name" "$DURATION"
  sleep 3
done

cd "$HERE" && .venv/bin/python -m spike report
