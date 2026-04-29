#!/usr/bin/env bash
set -euo pipefail

cd /home/will/x-reply-bot
mkdir -p state/logs
session="x-reply-bot"

if tmux has-session -t "$session" 2>/dev/null; then
  echo "bot already running: session=$session"
  exit 0
fi

rm -f state/bot.pid
tmux new-session -d -s "$session" "cd /home/will/x-reply-bot && exec python3 /home/will/x-reply-bot/bot_daemon.py >> /home/will/x-reply-bot/state/logs/bot.log 2>&1"
sleep 1

if tmux has-session -t "$session" 2>/dev/null; then
  pid="$(tmux list-panes -t "$session" -F '#{pane_pid}' | head -n 1)"
  echo "$pid" > state/bot.pid
  echo "started bot: session=$session pid=$pid"
else
  rm -f state/bot.pid
  echo "failed to start bot"
  exit 1
fi
