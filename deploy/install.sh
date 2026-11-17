#!/usr/bin/env bash
# event-stage-screen installer for Raspberry Pi OS (Bookworm or newer, 64-bit).
#
#   git clone https://github.com/pat95st/event-stage-screen.git && cd event-stage-screen
#   sudo ./deploy/install.sh
#
# Re-running the script updates an existing installation; data is kept.
set -euo pipefail

APP_DIR=/opt/event-stage-screen
DATA_DIR=/var/lib/event-stage-screen
ENV_FILE=/etc/event-stage-screen.env
HELPER_DIR=/usr/local/lib/event-stage-screen
SERVICE=event-stage-screen.service
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

say() { printf '==> %s\n' "$*"; }

if [[ $EUID -ne 0 ]]; then
  echo "Please run with sudo." >&2
  exit 1
fi

say "System packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl rsync sudo network-manager

say "Service user 'ess'"
if ! id ess >/dev/null 2>&1; then
  useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin ess
fi
for group in video render; do
  getent group "$group" >/dev/null && usermod -aG "$group" ess
done

say "Application -> $APP_DIR"
mkdir -p "$APP_DIR" "$DATA_DIR"
rsync -a --delete \
  --exclude ".git" --exclude ".venv" --exclude "data" --exclude "__pycache__" \
  --exclude "prototype" --exclude "stagescreen/web/static/vendor" \
  "$SRC_DIR/" "$APP_DIR/"

say "Python environment"
python3 -m venv "$APP_DIR/.venv"
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

say "Service"
cp "$APP_DIR/deploy/$SERVICE" "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

PORT="$(grep -E '^ESS_PORT=' "$ENV_FILE" | cut -d= -f2)"
echo
echo "Done. Web interface: http://$(hostname -I | awk '{print $1}'):${PORT:-8080}/"
echo "              or     http://$(hostname).local:${PORT:-8080}/"
echo "The administrator account is created on the first visit."
