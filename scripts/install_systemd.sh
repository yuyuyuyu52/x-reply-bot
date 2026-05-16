#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

cd "$X_REPLY_ROOT"
mkdir -p state/logs

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "systemd install is only supported on Linux"
  exit 2
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found; install systemd or run on a systemd-based Linux host"
  exit 2
fi

sudo_cmd=()
if [[ "${EUID}" -ne 0 ]]; then
  sudo_cmd=(sudo)
fi

service_user="${X_REPLY_SYSTEMD_USER:-${SUDO_USER:-$(id -un)}}"
python_bin="$(command -v "$X_REPLY_PYTHON" 2>/dev/null || true)"
if [[ -z "$python_bin" ]]; then
  python_bin="$X_REPLY_PYTHON"
fi

unit_path="/etc/systemd/system/${X_REPLY_SYSTEMD_SERVICE}"
tmp_unit="$(mktemp)"
trap 'rm -f "$tmp_unit"' EXIT

cat > "$tmp_unit" <<EOF
[Unit]
Description=x-reply-bot daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${service_user}
WorkingDirectory=${X_REPLY_ROOT}
Environment=PYTHONUNBUFFERED=1
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=X_REPLY_ROOT=${X_REPLY_ROOT}
Environment=X_REPLY_PYTHON=${python_bin}
ExecStart=${python_bin} ${X_REPLY_ROOT}/bot_daemon.py
Restart=always
RestartSec=10
KillSignal=SIGTERM
KillMode=process
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

"${sudo_cmd[@]}" install -m 0644 "$tmp_unit" "$unit_path"
"${sudo_cmd[@]}" systemctl daemon-reload
"${sudo_cmd[@]}" systemctl enable --now "$X_REPLY_SYSTEMD_SERVICE"

echo "Installed and started systemd service: $X_REPLY_SYSTEMD_SERVICE"
echo "Status: systemctl status $X_REPLY_SYSTEMD_SERVICE"
echo "Logs: journalctl -u $X_REPLY_SYSTEMD_SERVICE -f"
echo "Service user: $service_user"
