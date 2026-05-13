#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

cd "$X_REPLY_ROOT"

X_REPLY_CDP_URL="${X_REPLY_CDP_URL:-http://127.0.0.1:9222}"
BROWSER_HARNESS_ROOT="${BROWSER_HARNESS_ROOT:-$X_REPLY_ROOT/vendor/browser-harness}"
BROWSER_HARNESS_BIN="${BROWSER_HARNESS_BIN:-$X_REPLY_ROOT/.bin/browser-harness}"

if ! curl -fsS "$X_REPLY_CDP_URL/json/version" >/tmp/x-reply-cdp-version.json 2>/dev/null; then
  echo "Chrome CDP unavailable: $X_REPLY_CDP_URL"
  echo "Try: bash scripts/start_chrome.sh"
  exit 1
fi

echo "Chrome CDP ok: $X_REPLY_CDP_URL"
"$X_REPLY_PYTHON" - <<'PY'
import json
from pathlib import Path

payload = json.loads(Path("/tmp/x-reply-cdp-version.json").read_text(encoding="utf-8"))
print("browser:", payload.get("Browser", "unknown"))
print("webSocketDebuggerUrl:", payload.get("webSocketDebuggerUrl", "missing"))
PY

if [[ ! -x "$BROWSER_HARNESS_BIN" ]]; then
  echo "browser-harness missing or not executable: $BROWSER_HARNESS_BIN"
  echo "Try: bash scripts/bootstrap_browser.sh"
  exit 1
fi

if [[ ! -d "$BROWSER_HARNESS_ROOT" ]]; then
  echo "browser-harness root missing: $BROWSER_HARNESS_ROOT"
  echo "Try: bash scripts/bootstrap_browser.sh"
  exit 1
fi

echo "browser-harness bin ok: $BROWSER_HARNESS_BIN"
echo "browser-harness root ok: $BROWSER_HARNESS_ROOT"

if "$BROWSER_HARNESS_BIN" --doctor >/tmp/x-reply-browser-harness-doctor.txt 2>&1; then
  cat /tmp/x-reply-browser-harness-doctor.txt
else
  cat /tmp/x-reply-browser-harness-doctor.txt
  echo "browser-harness doctor failed"
  exit 1
fi
