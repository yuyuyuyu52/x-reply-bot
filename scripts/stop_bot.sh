#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

cd "$X_REPLY_ROOT"

if tmux has-session -t "$X_REPLY_TMUX_SESSION" 2>/dev/null; then
  tmux kill-session -t "$X_REPLY_TMUX_SESSION"
  echo "stopped bot: session=$X_REPLY_TMUX_SESSION"
else
  echo "bot not running"
fi

rm -f state/bot.pid
