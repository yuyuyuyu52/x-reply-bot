#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

legacy="# x-reply-bot schedule"
begin="# BEGIN x-reply-bot schedule"
end="# END x-reply-bot schedule"

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

printf '%s\n' "$filtered" | crontab -
echo "Removed x-reply-bot cron schedule."
