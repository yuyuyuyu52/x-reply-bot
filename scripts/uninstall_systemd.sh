#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found"
  exit 2
fi

sudo_cmd=()
if [[ "${EUID}" -ne 0 ]]; then
  sudo_cmd=(sudo)
fi

unit_path="/etc/systemd/system/${X_REPLY_SYSTEMD_SERVICE}"

"${sudo_cmd[@]}" systemctl disable --now "$X_REPLY_SYSTEMD_SERVICE" 2>/dev/null || true
"${sudo_cmd[@]}" rm -f "$unit_path"
"${sudo_cmd[@]}" systemctl daemon-reload
"${sudo_cmd[@]}" systemctl reset-failed "$X_REPLY_SYSTEMD_SERVICE" 2>/dev/null || true

echo "Removed systemd service: $X_REPLY_SYSTEMD_SERVICE"
