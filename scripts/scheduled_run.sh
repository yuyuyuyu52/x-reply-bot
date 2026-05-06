#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

export TZ="${X_REPLY_TZ:-Asia/Shanghai}"
cd "$X_REPLY_ROOT"

mkdir -p state/logs
exec 9>"state/run.lock"

timestamp() {
  date '+%Y-%m-%d %H:%M:%S %Z'
}

{
  if ! flock -n 9; then
    echo "===== $(timestamp) ====="
    echo "skip: previous run still active"
    echo
    exit 0
  fi

  hour="$(date '+%H')"
  if ((10#$hour < 7 || 10#$hour > 23)); then
    echo "===== $(timestamp) ====="
    echo "skip: outside Beijing window"
    echo
    exit 0
  fi

  if [[ "${1:-}" != "--no-jitter" ]]; then
    max_jitter="${X_REPLY_JITTER_SECONDS:-1800}"
    sleep_for=$(( RANDOM % (max_jitter + 1) ))
    echo "===== $(timestamp) ====="
    echo "jitter_sleep=${sleep_for}s"
    sleep "${sleep_for}"
  else
    echo "===== $(timestamp) ====="
    echo "jitter_sleep=0s"
  fi

  "$X_REPLY_PYTHON" "$X_REPLY_ROOT/run_once.py"
  echo
} >> "state/logs/cron.log" 2>&1
