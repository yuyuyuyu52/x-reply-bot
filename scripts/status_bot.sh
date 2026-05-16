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

if systemctl is-active --quiet "$X_REPLY_SYSTEMD_SERVICE"; then
  main_pid="$(systemctl show "$X_REPLY_SYSTEMD_SERVICE" --property=MainPID --value 2>/dev/null || true)"
  echo "bot running: service=$X_REPLY_SYSTEMD_SERVICE pid=${main_pid:-unknown}"
  exit 0
fi

echo "bot stopped: service=$X_REPLY_SYSTEMD_SERVICE"
exit 1
