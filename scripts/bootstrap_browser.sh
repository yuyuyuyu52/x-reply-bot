#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/_common.sh"

cd "$X_REPLY_ROOT"

BROWSER_HARNESS_ROOT="${BROWSER_HARNESS_ROOT:-$X_REPLY_ROOT/vendor/browser-harness}"
BROWSER_HARNESS_BIN="${BROWSER_HARNESS_BIN:-$X_REPLY_ROOT/.bin/browser-harness}"
X_REPLY_CDP_URL="${X_REPLY_CDP_URL:-http://127.0.0.1:9222}"
X_REPLY_CHROME_PROFILE_DIR="${X_REPLY_CHROME_PROFILE_DIR:-$HOME/.config/x-reply-bot-chrome}"
X_REPLY_CHROME_PORT="${X_REPLY_CHROME_PORT:-9222}"

sudo_cmd() {
  if [[ "$(id -u)" == "0" ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

ensure_env_var() {
  local key="$1"
  local value="$2"
  local env_file="$X_REPLY_ROOT/.env"

  if [[ ! -f "$env_file" ]]; then
    cp "$X_REPLY_ROOT/.env.example" "$env_file"
  fi

  if grep -Eq "^[[:space:]]*(export[[:space:]]+)?${key}=" "$env_file"; then
    return
  fi

  printf '\n%s="%s"\n' "$key" "$value" >> "$env_file"
}

install_linux_packages() {
  if [[ "$(uname -s)" != "Linux" ]]; then
    echo "skip Linux package install on $(uname -s)"
    return
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "apt-get not found; install git, curl, python3, tmux, util-linux, and Chrome/Chromium manually"
    return
  fi

  sudo_cmd apt-get update
  sudo_cmd apt-get install -y ca-certificates curl git python3 tmux util-linux

  if command -v google-chrome >/dev/null 2>&1 || command -v google-chrome-stable >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1; then
    return
  fi

  local arch
  arch="$(dpkg --print-architecture 2>/dev/null || true)"
  if [[ "$arch" == "amd64" ]]; then
    local deb
    deb="$(mktemp -t google-chrome-stable.XXXXXX.deb)"
    curl -fsSL "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb" -o "$deb"
    sudo_cmd apt-get install -y "$deb"
    rm -f "$deb"
  else
    sudo_cmd apt-get install -y chromium || sudo_cmd apt-get install -y chromium-browser
  fi
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi

  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"

  if ! command -v uv >/dev/null 2>&1; then
    echo "uv install finished, but uv is not on PATH. Add $HOME/.local/bin to PATH and rerun this script."
    exit 1
  fi
}

install_browser_harness() {
  mkdir -p "$(dirname "$BROWSER_HARNESS_BIN")"

  if [[ ! -d "$BROWSER_HARNESS_ROOT" ]]; then
    echo "browser-harness source missing: $BROWSER_HARNESS_ROOT"
    echo "This repository should include vendor/browser-harness."
    exit 1
  fi

  if [[ ! -f "$BROWSER_HARNESS_ROOT/pyproject.toml" ]]; then
    echo "browser-harness pyproject.toml missing: $BROWSER_HARNESS_ROOT"
    exit 1
  fi

  uv tool install -e "$BROWSER_HARNESS_ROOT"

  cat > "$BROWSER_HARNESS_BIN" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${BROWSER_HARNESS_GLOBAL_BIN:-}" ]]; then
  exec "$BROWSER_HARNESS_GLOBAL_BIN" "$@"
fi

resolved="$(command -v browser-harness || true)"
if [[ -n "$resolved" && "$resolved" != "$0" ]]; then
  exec "$resolved" "$@"
fi

exec "$HOME/.local/bin/browser-harness" "$@"
SH
  chmod +x "$BROWSER_HARNESS_BIN"
}

install_linux_packages
install_uv
install_browser_harness

ensure_env_var "X_REPLY_CDP_URL" "$X_REPLY_CDP_URL"
ensure_env_var "BROWSER_HARNESS_BIN" "$BROWSER_HARNESS_BIN"
ensure_env_var "BROWSER_HARNESS_ROOT" "$BROWSER_HARNESS_ROOT"
ensure_env_var "X_REPLY_CHROME_PROFILE_DIR" "$X_REPLY_CHROME_PROFILE_DIR"

echo "browser dependencies installed"
echo "harness root: $BROWSER_HARNESS_ROOT"
echo "harness bin:  $BROWSER_HARNESS_BIN"
echo "Chrome CDP:   $X_REPLY_CDP_URL"
echo
echo "Next:"
echo "  bash scripts/start_chrome.sh"
echo "  Open/log in to X once in that Chrome profile"
echo "  bash scripts/status_browser.sh"
