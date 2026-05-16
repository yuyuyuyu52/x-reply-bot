# Sourced helper for x-reply-bot shell scripts.
#
# Resolves the repo root from the calling script's location and loads .env
# (without overriding values already exported by the parent shell). This
# lets the same scripts run on any host (production Linux, dev macOS) as
# long as the repo layout is intact.
#
# Usage:
#   source "$(dirname "$0")/_common.sh"
#   # Now $X_REPLY_ROOT, $X_REPLY_PYTHON are guaranteed.

set -euo pipefail

# scripts/_common.sh lives one level below the repo root.
# Resolve symlinks so callers invoked through a symlinked entry point
# (e.g. /usr/local/bin/start_bot.sh -> .../scripts/start_bot.sh) still
# get the real repo root, not the symlink's parent directory.
__bot_common_src="${BASH_SOURCE[0]}"
while [ -L "$__bot_common_src" ]; do
  __bot_common_link="$(readlink "$__bot_common_src")"
  # Relative symlinks resolve against the link's own directory.
  case "$__bot_common_link" in
    /*) __bot_common_src="$__bot_common_link" ;;
    *) __bot_common_src="$(cd "$(dirname "$__bot_common_src")" && pwd)/$__bot_common_link" ;;
  esac
done
__bot_common_dir="$(cd "$(dirname "$__bot_common_src")" && pwd)"
X_REPLY_ROOT="$(cd "${__bot_common_dir}/.." && pwd)"
export X_REPLY_ROOT

# Default Python interpreter; override with X_REPLY_PYTHON in env or .env.
: "${X_REPLY_PYTHON:=python3}"
export X_REPLY_PYTHON

# systemd unit name for daemon lifecycle scripts.
: "${X_REPLY_SYSTEMD_SERVICE:=x-reply-bot.service}"
case "$X_REPLY_SYSTEMD_SERVICE" in
  *.service) ;;
  *) X_REPLY_SYSTEMD_SERVICE="${X_REPLY_SYSTEMD_SERVICE}.service" ;;
esac
export X_REPLY_SYSTEMD_SERVICE

# Load .env without overriding already-set vars (mirrors common.load_env_file()).
if [[ -f "${X_REPLY_ROOT}/.env" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    # Skip blanks and comments.
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    # Strip optional leading "export ".
    line="${line#export }"
    # Must contain "=".
    [[ "$line" != *"="* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    # Trim surrounding whitespace from key.
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    # Strip matching surrounding quotes from val.
    if [[ "$val" =~ ^\".*\"$ ]] || [[ "$val" =~ ^\'.*\'$ ]]; then
      val="${val:1:${#val}-2}"
    fi
    # Don't override values already set in the environment.
    if [[ -z "${!key:-}" ]]; then
      export "$key=$val"
    fi
  done < "${X_REPLY_ROOT}/.env"
fi
