#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

cd "$X_REPLY_ROOT"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found; production daemon management requires systemd"
  exit 2
fi

if ! systemctl cat "$X_REPLY_SYSTEMD_SERVICE" >/dev/null 2>&1; then
  echo "systemd service not installed: $X_REPLY_SYSTEMD_SERVICE"
  echo "install with: sudo bash \"$X_REPLY_ROOT/scripts/install_systemd.sh\""
  exit 2
fi

if [[ "${EUID}" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
  sudo systemctl start "$X_REPLY_SYSTEMD_SERVICE"
else
  systemctl start "$X_REPLY_SYSTEMD_SERVICE"
fi
systemctl is-active --quiet "$X_REPLY_SYSTEMD_SERVICE"
echo "started bot: service=$X_REPLY_SYSTEMD_SERVICE"
