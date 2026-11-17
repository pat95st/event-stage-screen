#!/usr/bin/env bash
# event-stage-screen installer for Raspberry Pi OS (Bookworm or newer, 64-bit).
#
#   git clone https://github.com/pat95st/event-stage-screen.git && cd event-stage-screen
#   sudo ./deploy/install.sh              # without NDI
#   sudo ./deploy/install.sh --with-ndi   # also runs deploy/install-ndi.sh
#
# Re-running the script updates an existing installation; data is kept.
set -euo pipefail

APP_DIR=/opt/event-stage-screen
DATA_DIR=/var/lib/event-stage-screen
ENV_FILE=/etc/event-stage-screen.env
HELPER_DIR=/usr/local/lib/event-stage-screen
SERVICE=event-stage-screen.service
KIOSK_SERVICE=event-stage-screen-kiosk.service
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WITH_NDI=0
[[ "${1:-}" == "--with-ndi" ]] && WITH_NDI=1

say() { printf '==> %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo." >&2
  exit 1
fi

say "System packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl rsync sudo network-manager \
  sway wlr-randr fonts-dejavu-core \
  python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good
if apt-cache show chromium >/dev/null 2>&1; then
  apt-get install -y -qq chromium
else
  apt-get install -y -qq chromium-browser
fi

say "Service user 'ess'"
if ! id ess >/dev/null 2>&1; then
  useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin ess
fi
for group in video render input; do
  getent group "$group" >/dev/null && usermod -aG "$group" ess
done

say "Application -> $APP_DIR"
mkdir -p "$APP_DIR" "$DATA_DIR"
rsync -a --delete \
  --exclude ".git" --exclude ".venv" --exclude "data" --exclude "__pycache__" \
  --exclude "prototype" --exclude "stagescreen/web/static/vendor" \
  "$SRC_DIR/" "$APP_DIR/"

say "Python environment"
# system site packages: the GStreamer bindings (python3-gi) come from Debian
if [[ -f "$APP_DIR/.venv/pyvenv.cfg" ]] && grep -q "include-system-site-packages = false" "$APP_DIR/.venv/pyvenv.cfg"; then
  rm -rf "$APP_DIR/.venv"
fi
python3 -m venv --system-site-packages "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

say "Bootstrap (offline copy)"
bash "$APP_DIR/scripts/fetch-vendor.sh" \
  || echo "    Note: Bootstrap could not be downloaded; the web UI will load it from the internet."

say "Privileged helper"
install -d -m 0755 -o root -g root "$HELPER_DIR"
install -m 0755 -o root -g root "$APP_DIR/deploy/ess-helper" "$HELPER_DIR/ess-helper"
sudoers_tmp="$(mktemp)"
cp "$APP_DIR/deploy/event-stage-screen.sudoers" "$sudoers_tmp"
if visudo -cf "$sudoers_tmp" >/dev/null; then
  install -m 0440 -o root -g root "$sudoers_tmp" /etc/sudoers.d/event-stage-screen
else
  echo "    Error: sudoers rule is invalid, privileged actions will not work." >&2
fi
rm -f "$sudoers_tmp"

say "Configuration"
[[ -f "$ENV_FILE" ]] || cp "$APP_DIR/deploy/event-stage-screen.env" "$ENV_FILE"
chown -R ess:ess "$DATA_DIR"
chmod 0750 "$DATA_DIR"
chown -R root:root "$APP_DIR"

say "Services"
cp "$APP_DIR/deploy/$SERVICE" "/etc/systemd/system/$SERVICE"
cp "$APP_DIR/deploy/$KIOSK_SERVICE" "/etc/systemd/system/$KIOSK_SERVICE"
chmod 0755 "$APP_DIR/deploy/kiosk-session.sh"
# The outputs are driven by our own kiosk session on tty1; a desktop display
# manager (Raspberry Pi OS Desktop) would compete for the screens.
for dm in lightdm gdm3 sddm; do
  if systemctl is-enabled "$dm" >/dev/null 2>&1; then
    echo "    Disabling the desktop display manager ($dm) – the Pi becomes a dedicated display."
    systemctl disable "$dm" >/dev/null 2>&1 || true
    systemctl stop "$dm" >/dev/null 2>&1 || true
  fi
done
systemctl set-default graphical.target >/dev/null
systemctl daemon-reload
systemctl enable "$SERVICE" "$KIOSK_SERVICE" >/dev/null
systemctl restart "$SERVICE"
systemctl restart "$KIOSK_SERVICE"

if [[ $WITH_NDI -eq 1 ]]; then
  bash "$APP_DIR/deploy/install-ndi.sh"
fi

PORT="$(grep -E '^ESS_PORT=' "$ENV_FILE" | cut -d= -f2)"
echo
echo "Done. Web interface: http://$(hostname -I | awk '{print $1}'):${PORT:-8080}/"
echo "              or     http://$(hostname).local:${PORT:-8080}/"
echo "The administrator account is created on the first visit."
if ! grep -q '^NDI_RUNTIME_DIR_V6=' "$ENV_FILE"; then
  echo "NDI support is not installed yet: sudo ./deploy/install-ndi.sh"
fi
