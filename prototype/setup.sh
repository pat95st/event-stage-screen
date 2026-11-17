#!/usr/bin/env bash
# Prepare a Raspberry Pi (Raspberry Pi OS 64-bit, Bookworm or Trixie) for the
# milestone 1 spike. Run as the normal user (sudo is used for apt).
#
#   ./setup.sh               # kiosk stack, GStreamer, Python venv
#   ./setup.sh --with-ndi    # additionally NDI runtime + GStreamer NDI plugin
#
# NDI: the proprietary NDI SDK is downloaded from the official source and its
# installer shows the license, which you must accept. Nothing NDI-related is
# stored in the repository; files land in prototype/.ndi (git-ignored).
#   NDI_SDK_URL   override the SDK download URL
#   GST_RS_REF    override the gst-plugins-rs branch/tag used for the plugin
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WITH_NDI=0
[[ "${1:-}" == "--with-ndi" ]] && WITH_NDI=1

say() { printf '\n==> %s\n' "$*"; }

pkg_available() { apt-cache show "$1" >/dev/null 2>&1; }

say "Installing system packages"
sudo apt-get update
PKGS=(
  labwc wlr-randr curl
  python3 python3-venv python3-gi python3-psutil
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gir1.2-gst-plugins-bad-1.0
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav
  gstreamer1.0-nice fonts-dejavu-core
)
if pkg_available chromium; then PKGS+=(chromium); else PKGS+=(chromium-browser); fi
for p in raspi-utils libraspberrypi-bin; do
  if pkg_available "$p"; then PKGS+=("$p"); break; fi
done
sudo apt-get install -y "${PKGS[@]}"

say "Creating Python virtual environment"
python3 -m venv --system-site-packages "$HERE/.venv"
"$HERE/.venv/bin/pip" install --upgrade pip >/dev/null
"$HERE/.venv/bin/pip" install -r "$HERE/requirements.txt"

chmod +x "$HERE/run.sh" "$HERE/all.sh" "$HERE/kiosk/session.sh"

if [[ "$WITH_NDI" != 1 ]]; then
  say "Done (without NDI). Run ./setup.sh --with-ndi to add NDI support."
  exit 0
fi

NDI_DIR="$HERE/.ndi"
mkdir -p "$NDI_DIR/lib" "$NDI_DIR/plugins" "$NDI_DIR/work"

# --- NDI runtime --------------------------------------------------------
if ! ls "$NDI_DIR/lib"/libndi.so.6* >/dev/null 2>&1; then
  say "Downloading the NDI SDK (license acceptance required)"
  NDI_SDK_URL="${NDI_SDK_URL:-https://downloads.ndi.tv/SDK/NDI_SDK_Linux/Install_NDI_SDK_v6_Linux.tar.gz}"
  cd "$NDI_DIR/work"
  curl -fL "$NDI_SDK_URL" -o ndi-sdk.tar.gz
  tar -xzf ndi-sdk.tar.gz
  installer="$(ls Install_NDI_SDK_v6_Linux.sh 2>/dev/null || ls ./*.sh | head -n1)"
  # The vendor installer displays the license and asks for acceptance.
  bash "$installer"
  libdir="$(find . -type d -path '*lib/aarch64-rpi4-linux-gnueabi' | head -n1)"
  [[ -z "$libdir" ]] && libdir="$(find . -type d -path '*lib/aarch64*' | head -n1)"
  [[ -z "$libdir" ]] && { echo "No aarch64 NDI library found in the SDK" >&2; exit 1; }
  cp -a "$libdir"/libndi.so* "$NDI_DIR/lib/"
  cd "$HERE"
fi

# --- GStreamer NDI plugin (gst-plugins-rs) -----------------------------------
export GST_PLUGIN_PATH="$NDI_DIR/plugins"
if gst-inspect-1.0 ndisrc >/dev/null 2>&1; then
  say "GStreamer NDI plugin already available"
else
  say "Building the GStreamer NDI plugin from gst-plugins-rs (takes a while)"
  sudo apt-get install -y git build-essential pkg-config \
    libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
  cargo_ver="$(cargo --version 2>/dev/null | awk '{print $2}')"
  if [[ -z "$cargo_ver" ]] || [[ "$(printf '%s\n1.83.0\n' "$cargo_ver" | sort -V | head -n1)" != "1.83.0" ]]; then
    curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal
    # shellcheck disable=SC1091
    source "$HOME/.cargo/env"
  fi
  gst_minor="$(pkg-config --modversion gstreamer-1.0 | cut -d. -f2)"
  case "$gst_minor" in
    22) ref=0.11 ;;
    24) ref=0.12 ;;
    26) ref=0.13 ;;
    *)  ref=main ;;
  esac
  ref="${GST_RS_REF:-$ref}"
  src="$NDI_DIR/work/gst-plugins-rs"
  rm -rf "$src"
  git clone --depth 1 --branch "$ref" https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git "$src"
  (cd "$src" && cargo build --release -j 2 -p gst-plugin-ndi)
  cp "$src/target/release/libgstndi.so" "$NDI_DIR/plugins/"
fi

export NDI_RUNTIME_DIR_V6="$NDI_DIR/lib"
export LD_LIBRARY_PATH="$NDI_DIR/lib"
if gst-inspect-1.0 ndisrc >/dev/null 2>&1; then
  say "NDI ready. List sources with: ./ndi-sources.sh"
else
  echo "ndisrc still not available — check the output above" >&2
  exit 1
fi
