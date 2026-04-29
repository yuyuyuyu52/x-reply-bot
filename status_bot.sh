#!/usr/bin/env bash
set -euo pipefail

cd /home/will/x-reply-bot
session="x-reply-bot"

if tmux has-session -t "$session" 2>/dev/null; then
  pid="$(tmux list-panes -t "$session" -F '#{pane_pid}' | head -n 1)"
  echo "bot running: session=$session pid=$pid"
  exit 0
fi

rm -f state/bot.pid
echo "bot stopped"
exit 1
