#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/_common.sh"

cd "$X_REPLY_ROOT"
mkdir -p state/logs

LOG_PATH="$X_REPLY_ROOT/state/logs/update.log"
exec >> "$LOG_PATH" 2>&1
exec 9>"state/update.lock"

timestamp() {
  date '+%Y-%m-%d %H:%M:%S %Z'
}

notify_tg() {
  local text="$1"
  UPDATE_NOTIFY_TEXT="$text" PYTHONPATH="$X_REPLY_ROOT" "$X_REPLY_PYTHON" - <<'PY' || true
import os

from src.common import load_env_file, telegram_enabled, telegram_notify

load_env_file()
if telegram_enabled():
    telegram_notify(os.environ["UPDATE_NOTIFY_TEXT"])
PY
}

bot_running() {
  tmux has-session -t "$X_REPLY_TMUX_SESSION" 2>/dev/null
}

ensure_bot_running() {
  if bot_running; then
    return 0
  fi
  echo "bot is not running; attempting restart"
  bash "$X_REPLY_ROOT/scripts/start_bot.sh" || true
}

on_error() {
  local code="$1"
  local line="$2"
  echo "===== $(timestamp) update failed line=${line} code=${code} ====="
  ensure_bot_running
  notify_tg "❌ 更新失败

已尝试保持/恢复 bot 运行。
日志：state/logs/update.log
exit_code: ${code}
line: ${line}"
  exit "$code"
}
trap 'on_error "$?" "$LINENO"' ERR

echo
echo "===== $(timestamp) update start ====="

if ! flock -n 9; then
  echo "another update is already running"
  notify_tg "⏳ 更新已在执行，请稍后再试。"
  exit 0
fi

old_rev="$(git rev-parse --short HEAD 2>/dev/null || printf unknown)"
echo "old_rev=${old_rev}"

git pull --ff-only

new_rev="$(git rev-parse --short HEAD 2>/dev/null || printf unknown)"
echo "new_rev=${new_rev}"

echo "running python compile check"
"$X_REPLY_PYTHON" -m compileall -q \
  bot_daemon.py \
  discover_hotspots.py \
  post_once.py \
  post_topics.py \
  run_once.py \
  sync_tg_commands.py \
  src

echo "syncing telegram command menu"
if ! "$X_REPLY_PYTHON" "$X_REPLY_ROOT/sync_tg_commands.py"; then
  echo "warning: sync_tg_commands.py failed; continuing restart"
fi

echo "restarting bot"
bash "$X_REPLY_ROOT/scripts/stop_bot.sh" || true
bash "$X_REPLY_ROOT/scripts/start_bot.sh"

echo "checking bot status"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if bash "$X_REPLY_ROOT/scripts/status_bot.sh"; then
    break
  fi
  sleep 1
done

if ! bot_running; then
  echo "bot health check failed after restart"
  false
fi

if [[ "$old_rev" == "$new_rev" ]]; then
  version_line="当前已经是最新版本：${new_rev}"
else
  version_line="已更新：${old_rev} → ${new_rev}"
fi

echo "===== $(timestamp) update ok ====="
notify_tg "✅ 更新完成

${version_line}
已通过编译检查，bot 已重启并通过状态检查。"
