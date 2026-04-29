#!/usr/bin/env bash
set -euo pipefail

cd /home/will/x-reply-bot
session="x-reply-bot"

if tmux has-session -t "$session" 2>/dev/null; then
  tmux kill-session -t "$session"
  echo "stopped bot: session=$session"
else
  echo "bot not running"
fi

rm -f state/bot.pid
