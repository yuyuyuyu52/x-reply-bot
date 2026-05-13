#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

cd "$X_REPLY_ROOT"

X_REPLY_CDP_URL="${X_REPLY_CDP_URL:-http://127.0.0.1:9222}"
X_REPLY_CHROME_PORT="${X_REPLY_CHROME_PORT:-${X_REPLY_CDP_URL##*:}}"
X_REPLY_CHROME_PORT="${X_REPLY_CHROME_PORT%%/*}"
X_REPLY_CHROME_PROFILE_DIR="${X_REPLY_CHROME_PROFILE_DIR:-$HOME/.config/x-reply-bot-chrome}"
X_REPLY_CHROME_BIN="${X_REPLY_CHROME_BIN:-}"

find_chrome() {
  if [[ -n "$X_REPLY_CHROME_BIN" ]]; then
    echo "$X_REPLY_CHROME_BIN"
    return
  fi
  if [[ "$(uname -s)" == "Darwin" ]]; then
    for candidate in "/Applications/Google Chrome.app" "$HOME/Applications/Google Chrome.app" "/Applications/Chromium.app" "$HOME/Applications/Chromium.app"; do
      if [[ -d "$candidate" ]]; then
        echo "$candidate"
        return
      fi
    done
  fi
  for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return
    fi
  done
  return 1
}

if curl -fsS "$X_REPLY_CDP_URL/json/version" >/dev/null 2>&1; then
  echo "Chrome CDP already available: $X_REPLY_CDP_URL"
  exit 0
fi

chrome_bin="$(find_chrome || true)"
if [[ -z "$chrome_bin" ]]; then
  echo "Chrome/Chromium not found. Run: bash scripts/bootstrap_browser.sh"
  exit 1
fi

if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" && "$(uname -s)" == "Linux" ]]; then
  echo "No DISPLAY/WAYLAND_DISPLAY detected. Start a desktop/VNC/Xvfb session first, then rerun this script."
  exit 1
fi

mkdir -p "$X_REPLY_CHROME_PROFILE_DIR" "$X_REPLY_ROOT/state/logs"

args=(
  "--remote-debugging-address=127.0.0.1"
  "--remote-debugging-port=$X_REPLY_CHROME_PORT"
  "--user-data-dir=$X_REPLY_CHROME_PROFILE_DIR"
  "--no-first-run"
  "--no-default-browser-check"
  "--disable-dev-shm-usage"
)

if [[ "$(id -u)" == "0" ]]; then
  args+=("--no-sandbox")
fi

if [[ "$(uname -s)" == "Darwin" ]]; then
  chrome_app_name="Google Chrome"
  if [[ "$chrome_bin" == *"Chromium"* ]]; then
    chrome_app_name="Chromium"
  fi
  open -na "$chrome_app_name" --args "${args[@]}" "https://x.com/home" >> "$X_REPLY_ROOT/state/logs/chrome.log" 2>&1
  : > "$X_REPLY_ROOT/state/chrome.pid"
else
  nohup "$chrome_bin" "${args[@]}" "https://x.com/home" >> "$X_REPLY_ROOT/state/logs/chrome.log" 2>&1 &
  echo $! > "$X_REPLY_ROOT/state/chrome.pid"
fi

for _ in $(seq 1 30); do
  if curl -fsS "$X_REPLY_CDP_URL/json/version" >/dev/null 2>&1; then
    if [[ "$(uname -s)" == "Darwin" ]]; then
      pgrep -fn "remote-debugging-port=$X_REPLY_CHROME_PORT" > "$X_REPLY_ROOT/state/chrome.pid" 2>/dev/null || true
    fi
    echo "started Chrome CDP: $X_REPLY_CDP_URL"
    echo "profile: $X_REPLY_CHROME_PROFILE_DIR"
    exit 0
  fi
  sleep 1
done

echo "Chrome was launched but CDP did not become ready within 30s."
echo "Check: $X_REPLY_ROOT/state/logs/chrome.log"
exit 1
