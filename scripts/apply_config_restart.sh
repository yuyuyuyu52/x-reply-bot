#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/_common.sh"

cd "$X_REPLY_ROOT"
mkdir -p state/logs

LOG_PATH="$X_REPLY_ROOT/state/logs/config_apply.log"
BACKUP_PATH="${1:-}"
CONFIG_KEY="${2:-unknown}"

exec >> "$LOG_PATH" 2>&1
exec 9>"state/config_apply.lock"

timestamp() {
  date '+%Y-%m-%d %H:%M:%S %Z'
}

notify_tg() {
  local text="$1"
  CONFIG_NOTIFY_TEXT="$text" PYTHONPATH="$X_REPLY_ROOT" "$X_REPLY_PYTHON" - <<'PY' || true
import os

from src.common import load_env_file, telegram_enabled, telegram_notify

load_env_file()
if telegram_enabled():
    telegram_notify(os.environ["CONFIG_NOTIFY_TEXT"])
PY
}

restore_env() {
  if [[ -n "$BACKUP_PATH" && -f "$BACKUP_PATH" ]]; then
    cp "$BACKUP_PATH" "$X_REPLY_ROOT/.env"
    echo "restored .env from $BACKUP_PATH"
  fi
}

bot_running() {
  tmux has-session -t "$X_REPLY_TMUX_SESSION" 2>/dev/null
}

on_error() {
  local code="$1"
  local line="$2"
  echo "===== $(timestamp) config apply failed key=${CONFIG_KEY} line=${line} code=${code} ====="
  restore_env
  bash "$X_REPLY_ROOT/scripts/start_bot.sh" || true
  notify_tg "❌ 配置生效失败

配置项：${CONFIG_KEY}
已尝试回滚 .env 并恢复 bot。
日志：state/logs/config_apply.log
exit_code: ${code}
line: ${line}"
  exit "$code"
}
trap 'on_error "$?" "$LINENO"' ERR

echo
echo "===== $(timestamp) config apply start key=${CONFIG_KEY} ====="

if ! flock -n 9; then
  echo "another config apply is already running"
  notify_tg "⏳ 另一个配置生效流程正在执行，请稍后再试。"
  exit 0
fi

echo "running python compile check"
"$X_REPLY_PYTHON" -m compileall -q \
  bot_daemon.py \
  discover_hotspots.py \
  post_once.py \
  post_topics.py \
  run_once.py \
  sync_tg_commands.py \
  src

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
  echo "bot health check failed after config restart"
  false
fi

echo "===== $(timestamp) config apply ok key=${CONFIG_KEY} ====="
notify_tg "✅ 配置已生效

配置项：${CONFIG_KEY}
已通过编译检查，bot 已重启并通过状态检查。"
