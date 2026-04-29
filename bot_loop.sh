#!/usr/bin/env bash
set -euo pipefail

export TZ=Asia/Shanghai
cd /home/will/x-reply-bot

mkdir -p state/logs
exec 9>"state/bot.lock"

if ! flock -n 9; then
  exit 0
fi

exec python3 /home/will/x-reply-bot/bot_daemon.py
