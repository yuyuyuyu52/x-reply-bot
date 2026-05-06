#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

cd "$X_REPLY_ROOT"

if tmux has-session -t "$X_REPLY_TMUX_SESSION" 2>/dev/null; then
  pid="$(tmux list-panes -t "$X_REPLY_TMUX_SESSION" -F '#{pane_pid}' | head -n 1)"
  echo "bot running: session=$X_REPLY_TMUX_SESSION pid=$pid"
  exit 0
fi

rm -f state/bot.pid
echo "bot stopped"
exit 1
