#!/usr/bin/env bash
# Adds NDI support to an existing event-stage-screen installation.
#
#   sudo ./deploy/install-ndi.sh
#
# 1. Downloads the NDI SDK from the official NDI website. The vendor
#    installer shows the NDI license, which you have to accept yourself.
#    Only the runtime library is kept (in /usr/local/lib/event-stage-screen/ndi).
# 2. Installs the GStreamer NDI plugin (gst-plugins-rs): from a Debian package
#    if available, otherwise it is built from source (takes 10–40 minutes).
# 3. Registers both for the services and restarts them.
#
# Environment overrides:
#   NDI_SDK_URL   download URL of the NDI SDK for Linux
#   GST_RS_REF    gst-plugins-rs branch/tag to build
#
# Nothing NDI-related is part of the repository (the NDI SDK is proprietary).
set -euo pipefail

NDI_DIR=/usr/local/lib/event-stage-screen/ndi
ENV_FILE=/etc/event-stage-screen.env
BUILD_DIR=/var/cache/event-stage-screen/ndi-build
NDI_SDK_URL="${NDI_SDK_URL:-https://downloads.ndi.tv/SDK/NDI_SDK_Linux/Install_NDI_SDK_v6_Linux.tar.gz}"

say() { printf '==> %s\n' "$*"; }
pkg_available() { apt-cache show "$1" >/dev/null 2>&1; }

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo." >&2
  exit 1
fi
if [[ ! -t 0 ]]; then
  echo "This script must run interactively: the NDI license has to be accepted by you." >&2
  exit 1
fi

mkdir -p "$NDI_DIR/lib" "$NDI_DIR/plugins" "$BUILD_DIR"
export GST_PLUGIN_PATH="$NDI_DIR/plugins"
export NDI_RUNTIME_DIR_V6="$NDI_DIR/lib"
export LD_LIBRARY_PATH="$NDI_DIR/lib"

# ---------------------------------------------------------------- NDI runtime
if ls "$NDI_DIR/lib"/libndi.so.6* >/dev/null 2>&1; then
  say "NDI runtime already installed"
else
  say "Downloading the NDI SDK"
  cd "$BUILD_DIR"
  rm -rf sdk && mkdir sdk && cd sdk
  curl -fL "$NDI_SDK_URL" -o ndi-sdk.tar.gz
  tar -xzf ndi-sdk.tar.gz
  installer="$(ls ./*.sh | head -n1)"
  say "Starting the NDI installer – please read and accept the NDI license"
  bash "$installer"
  libdir="$(find . -type d -path '*lib/aarch64-rpi4-linux-gnueabi' | head -n1)"
  [[ -z "$libdir" ]] && libdir="$(find . -type d -path '*lib/aarch64*' | head -n1)"
  if [[ -z "$libdir" ]]; then
    echo "No 64-bit ARM NDI library found in the SDK (did you accept the license?)." >&2
    exit 1
  fi
  cp -a "$libdir"/libndi.so* "$NDI_DIR/lib/"
  cd / && rm -rf "$BUILD_DIR/sdk"
fi

# ---------------------------------------------------------------- GStreamer NDI plugin
if gst-inspect-1.0 ndisrc >/dev/null 2>&1; then
  say "GStreamer NDI plugin already available"
else
  installed=0
  for p in gstreamer1.0-plugins-rs-ndi gst-plugin-ndi; do
    if pkg_available "$p"; then
      say "Installing $p"
      apt-get install -y -qq "$p" && installed=1 && break
    fi
  done
  if [[ $installed -eq 0 ]]; then
    say "Building the GStreamer NDI plugin from gst-plugins-rs"
    apt-get install -y -qq git build-essential pkg-config libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
    export RUSTUP_HOME=/var/cache/event-stage-screen/rustup CARGO_HOME=/var/cache/event-stage-screen/cargo
    if [[ ! -x "$CARGO_HOME/bin/cargo" ]]; then
      curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --no-modify-path
    fi
    export PATH="$CARGO_HOME/bin:$PATH"
    gst_minor="$(pkg-config --modversion gstreamer-1.0 | cut -d. -f2)"
    case "$gst_minor" in
      22) ref=0.11 ;;
      24) ref=0.12 ;;
      26) ref=0.13 ;;
      *)  ref=main ;;
    esac
    ref="${GST_RS_REF:-$ref}"
    src="$BUILD_DIR/gst-plugins-rs"
    rm -rf "$src"
    git clone --depth 1 --branch "$ref" https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs.git "$src"
    (cd "$src" && cargo build --release -j 2 -p gst-plugin-ndi)
    install -m 0644 "$src/target/release/libgstndi.so" "$NDI_DIR/plugins/"
    rm -rf "$src"
  fi
fi

if ! gst-inspect-1.0 ndisrc >/dev/null 2>&1; then
  echo "The GStreamer element ndisrc is still not available – see the output above." >&2
  exit 1
fi

# ---------------------------------------------------------------- register for the services
say "Registering NDI for the services"
touch "$ENV_FILE"
sed -i '/^GST_PLUGIN_PATH=/d;/^NDI_RUNTIME_DIR_V6=/d;/^LD_LIBRARY_PATH=/d' "$ENV_FILE"
cat >> "$ENV_FILE" <<EOF
GST_PLUGIN_PATH=$NDI_DIR/plugins
NDI_RUNTIME_DIR_V6=$NDI_DIR/lib
LD_LIBRARY_PATH=$NDI_DIR/lib
EOF
systemctl restart event-stage-screen.service 2>/dev/null || true

echo
echo "NDI is ready. Sources appear on the NDI page of the web interface."
