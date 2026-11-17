#!/usr/bin/env bash
# List NDI sources visible from this Pi (uses the plugin/runtime from setup.sh).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export GST_PLUGIN_PATH="$HERE/.ndi/plugins${GST_PLUGIN_PATH:+:$GST_PLUGIN_PATH}"
export NDI_RUNTIME_DIR_V6="$HERE/.ndi/lib"
export LD_LIBRARY_PATH="$HERE/.ndi/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
cd "$HERE" && exec .venv/bin/python -m spike sources
