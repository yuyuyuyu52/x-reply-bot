#!/usr/bin/env python3
"""Shared infrastructure: state paths, env loading, JSON I/O, file locking.

This module was the original monolith; domain-specific code has been extracted
into dedicated modules.  Everything is re-exported here so existing callers
that do ``from common import chat_json_result`` keep working.

Canonical homes for extracted code:
  llm.py          – LLM client, cost estimation, JSON parsing
  harness.py      – browser-harness interaction, CDP resolution
  telegram.py     – Telegram Bot API helpers
  topics.py       – post-topic queue management
  metrics.py      – X post metric parsing & engagement scoring
  context_builder.py – learning/persona context assembly for prompts
"""
from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
SELECTED_PATH = STATE_DIR / "selected_post.json"
REPLIED_PATH = STATE_DIR / "replied_posts.json"
RUN_LOG_PATH = STATE_DIR / "run_log.json"
SCREENSHOT_DIR = STATE_DIR / "screenshots"
HISTORY_DIR = STATE_DIR / "history"
LATEST_RUN_PATH = STATE_DIR / "latest_run.json"
POST_HISTORY_DIR = STATE_DIR / "post_history"
LATEST_POST_RUN_PATH = STATE_DIR / "latest_post_run.json"
POST_TOPICS_PATH = STATE_DIR / "post_topics.json"
PERSONA_PATH = STATE_DIR / "persona.json"
TELEGRAM_STATE_PATH = STATE_DIR / "telegram_state.json"
DAILY_REPORT_STATE_PATH = STATE_DIR / "daily_report_state.json"
LATEST_REVISIT_RUN_PATH = STATE_DIR / "latest_revisit_run.json"
REVISIT_REPORT_STATE_PATH = STATE_DIR / "revisit_report_state.json"
HOTSPOT_STORE_PATH = STATE_DIR / "hotspot.db"
HOTSPOT_HISTORY_DIR = STATE_DIR / "hotspot_history"
LATEST_HOTSPOT_RUN_PATH = STATE_DIR / "latest_hotspot_run.json"
REVISIT_HISTORY_DIR = STATE_DIR / "revisit_history"
BOT_LOCK_PATH = STATE_DIR / "bot.lock"
RUN_LOCK_PATH = STATE_DIR / "run_once.lock"
POST_LOCK_PATH = STATE_DIR / "post_once.lock"
OBSERVE_LOCK_PATH = STATE_DIR / "observe_feed.lock"
REVISIT_LOCK_PATH = STATE_DIR / "revisit.lock"
HOTSPOT_LOCK_PATH = STATE_DIR / "hotspot_discover.lock"
PERSONA_LOCK_PATH = STATE_DIR / "persona.lock"
FOLLOW_TODAY_PATH = STATE_DIR / "follow_today.json"
LOG_DIR = STATE_DIR / "logs"
ENV_PATH = ROOT / ".env"
VALID_POST_TOPIC_TYPES = {"news_react", "story", "argument", "casual", "thread", "article"}

THREAD_MIN_SEGMENTS = 3
THREAD_MAX_SEGMENTS = 5
THREAD_MAX_SEGMENT_CHARS = 280

BLOCK_PATTERNS = [
    "promoted",
    "广告",
    "赞助",
    "抽奖",
    "giveaway",
    "airdrop",
    "邀请码",
    "返利",
    "affiliate",
    "discount code",
    "use my code",
    "dm me",
    "赚钱",
    "稳赚",
    "投资建议",
    "signal",
    "onlyfans",
    "成人视频",
]


def ensure_state_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    POST_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    REVISIT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def load_env_file() -> None:
    if not ENV_PATH.exists():
        return
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return default


def model_name() -> str:
    return env_first("X_REPLY_MODEL", "OPENAI_MODEL", "ANTHROPIC_MODEL", default="MiniMax-M2.7")


def base_url() -> str:
    return env_first(
        "X_REPLY_BASE_URL",
        "OPENAI_BASE_URL",
        "ANTHROPIC_BASE_URL",
        default="https://api.minimaxi.com/v1",
    )


def api_key() -> str:
    return env_first("X_REPLY_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def image_api_key() -> str:
    return os.environ.get("X_REPLY_IMAGE_API_KEY", "").strip()


def image_api_url() -> str:
    return os.environ.get("X_REPLY_IMAGE_API_URL", "").strip()


def image_model() -> str:
    return os.environ.get("X_REPLY_IMAGE_MODEL", "gpt-image-2").strip()


def image_cost_cny() -> float:
    try:
        return float(os.environ.get("X_REPLY_IMAGE_COST_CNY", "0.070"))
    except ValueError:
        return 0.070


def append_log(entry: dict) -> None:
    logs = load_json(RUN_LOG_PATH, [])
    logs.append(entry)
    write_json(RUN_LOG_PATH, logs[-200:])


def append_jsonl(path: Path, entry: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def history_path_for(timestamp_label: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", timestamp_label)
    return HISTORY_DIR / f"{safe}.json"


def post_history_path_for(timestamp_label: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", timestamp_label)
    return POST_HISTORY_DIR / f"{safe}.json"


def looks_supported_language(text: str) -> bool:
    """Check whether text is primarily Chinese/English (shared filter)."""
    if not text:
        return False
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    other_letters = len(re.findall(r"[\u0400-\u04ff\u0600-\u06ff\u0900-\u0d7f\u3040-\u30ff\uac00-\ud7af]", text))
    meaningful = han + latin + other_letters
    if meaningful == 0:
        return False
    return (han + latin) / meaningful >= 0.8


def normalize_status_url(url: str) -> str:
    """Extract canonical https://x.com/<user>/status/<id> from a URL."""
    match = re.search(r"https://x\.com/[^/]+/status/\d+", url or "")
    return match.group(0) if match else ""


def repost_enabled() -> bool:
    return os.environ.get("X_REPLY_ENABLE_REPOST", "1") not in ("0", "false", "no", "False")


def quote_enabled() -> bool:
    return os.environ.get("X_REPLY_ENABLE_QUOTE", "1") not in ("0", "false", "no", "False")


def repost_daily_limit() -> int:
    try:
        return max(0, int(os.environ.get("X_REPOST_DAILY_LIMIT", "1")))
    except ValueError:
        return 1


def count_daily_reposts(date_str: str) -> int:
    total = 0
    for path in sorted(HISTORY_DIR.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if item.get("date_beijing") == date_str and item.get("action") == "repost":
            total += 1
    return total


@contextmanager
def exclusive_lock(path: Path):
    """Context manager for flock-based exclusive locking."""
    fh = path.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fh
    except BlockingIOError:
        fh.close()
        raise
    except BaseException:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()
        raise
    else:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


def persist_run_record(record: dict, latest_path: Path, history_dir: Path, stamp: str) -> None:
    """Write a run record to both the latest-run file and the timestamped archive."""
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", stamp)
    write_json(latest_path, record)
    write_json(history_dir / f"{safe}.json", record)


# ---------------------------------------------------------------------------
# Backwards-compatibility re-exports
# ---------------------------------------------------------------------------
# Everything below was moved to dedicated modules but is re-exported here so
# that ``from common import X`` in existing code keeps working.

from src.llm import (  # noqa: E402, F401
    anthropic_completion,
    chat_completion,
    chat_json_result,
    chat_text,
    chat_text_result,
    estimate_cost,
    extract_first_json_object,
    extract_usage,
    parse_json_object,
    post_json_with_retries,
    provider_mode,
    qwen35_flash_rates,
    should_retry_http_error,
)

from src.harness import (  # noqa: E402, F401
    browser_harness_bin,
    browser_harness_root,
    cdp_urls,
    harness_compose_and_send_snippet,
    harness_navigate_snippet,
    resolve_ws,
    restart_harness_daemon,
    run_harness,
)

from src.telegram import (  # noqa: E402, F401
    telegram_chat_id,
    telegram_enabled,
    telegram_get_commands,
    telegram_notify,
    telegram_set_commands,
    telegram_token,
    tg_api,
)

from src.topics import (  # noqa: E402, F401
    load_post_topics,
    mark_post_topic_status,
    next_pending_post_topic,
    normalize_post_topic,
    post_topic_summary,
    save_post_topics,
    topic_summary_text,
)

# Legacy module-level constants (evaluated lazily via functions now).
BROWSER_HARNESS = browser_harness_bin()
BROWSER_HARNESS_ROOT = browser_harness_root()
CDP_URLS = cdp_urls()
