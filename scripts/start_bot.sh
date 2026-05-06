#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

cd "$X_REPLY_ROOT"
mkdir -p state/logs

if tmux has-session -t "$X_REPLY_TMUX_SESSION" 2>/dev/null; then
  echo "bot already running: session=$X_REPLY_TMUX_SESSION"
  exit 0
fi

rm -f state/bot.pid
tmux new-session -d -s "$X_REPLY_TMUX_SESSION" \
  "cd '$X_REPLY_ROOT' && exec '$X_REPLY_PYTHON' '$X_REPLY_ROOT/bot_daemon.py' >> '$X_REPLY_ROOT/state/logs/bot.log' 2>&1"
sleep 1

if tmux has-session -t "$X_REPLY_TMUX_SESSION" 2>/dev/null; then
  pid="$(tmux list-panes -t "$X_REPLY_TMUX_SESSION" -F '#{pane_pid}' | head -n 1)"
  echo "$pid" > state/bot.pid
  echo "started bot: session=$X_REPLY_TMUX_SESSION pid=$pid"
else
  rm -f state/bot.pid
  echo "failed to start bot"
  exit 1
fi
