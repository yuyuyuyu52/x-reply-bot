#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/scripts/_common.sh"

export TZ="${X_REPLY_TZ:-Asia/Shanghai}"
cd "$X_REPLY_ROOT"

mkdir -p state/logs
exec 9>"state/bot.lock"

if ! flock -n 9; then
  exit 0
fi

exec "$X_REPLY_PYTHON" "$X_REPLY_ROOT/bot_daemon.py"
