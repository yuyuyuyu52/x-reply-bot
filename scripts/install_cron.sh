#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

script="$X_REPLY_ROOT/scripts/scheduled_run.sh"
legacy="# x-reply-bot schedule"
begin="# BEGIN x-reply-bot schedule"
end="# END x-reply-bot schedule"
cron_tz="CRON_TZ=${X_REPLY_TZ:-Asia/Shanghai}"
job="0 1-23 * * * /usr/bin/env bash \"${script}\""

existing="$(crontab -l 2>/dev/null || true)"
filtered="$(
  printf '%s\n' "$existing" | awk -v legacy="$legacy" -v begin="$begin" -v end="$end" '
    legacy_skip > 0 { legacy_skip--; next }
    $0 == legacy { legacy_skip = 2; next }
    $0 == begin { skip = 1; next }
    $0 == end { skip = 0; next }
    !skip { print }
  '
)"

{
  if [[ -n "$filtered" ]]; then
    printf '%s\n' "$filtered"
  fi
  printf '%s\n' "$begin"
  printf '%s\n' "$cron_tz"
  printf '%s\n' "$job"
  printf '%s\n' "$end"
} | crontab -

echo "Installed cron job:"
echo "$cron_tz"
echo "$job"
