#!/usr/bin/env python3
"""Telegram long-poll loop and slash-command dispatcher.

Extracted from bot_daemon.py. ``handle_command`` is the per-message
dispatcher; ``poll_updates`` reads pending updates from getUpdates and
threads each through ``handle_command``. Both functions return the
updated daemon-loop state tuple unchanged when no command is recognized.

To avoid an import cycle, the ``start_job`` / ``_child_env`` / ``ROOT``
helpers from ``bot_daemon`` are imported lazily inside ``handle_command``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

from src.common import (
    TELEGRAM_STATE_PATH,
    load_json,
    telegram_chat_id,
    telegram_enabled,
    telegram_notify,
    telegram_token,
)
from src.logger import get_logger
from src.persona_store import add_event as persona_add_event
from src.reporters import (
    hotspot_summary,
    learning_summary,
    post_summary,
    revisit_summary,
    status_text,
)

logger = get_logger(__name__)


def _safe_notify(text: str) -> None:
    if not telegram_enabled():
        return
    try:
        telegram_notify(text)
    except Exception as exc:
        logger.warning(f"telegram_notify failed: {exc}")


def _start_update_process(root, env: dict) -> subprocess.Popen[str]:
    return subprocess.Popen(
        ["/usr/bin/env", "bash", str(root / "scripts/update_bot.sh")],
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        start_new_session=True,
    )


def tg_api(method: str, params: dict | None = None, timeout: int = 30) -> dict:
    token = telegram_token()
    if not token:
        raise RuntimeError("telegram not configured")
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def read_tg_state() -> dict:
    return load_json(TELEGRAM_STATE_PATH, {"update_offset": 0})


def write_tg_state(state: dict) -> None:
    """Atomically persist Telegram update offset.

    Uses a pid-scoped tmp file + os.replace so a daemon crash mid-write can't
    leave TELEGRAM_STATE_PATH truncated (which would re-replay every old
    update on next start).
    """
    tmp = TELEGRAM_STATE_PATH.with_suffix(TELEGRAM_STATE_PATH.suffix + f".tmp.{os.getpid()}")
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, TELEGRAM_STATE_PATH)
    finally:
        # Best-effort cleanup if os.replace failed.
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def handle_command(
    text: str,
    run_proc: subprocess.Popen[str] | None,
    next_run_at: datetime,
    next_post_run_at: datetime,
    next_learn_at: datetime,
    next_revisit_at: datetime,
    next_hotspot_at: datetime,
    run_trigger: str,
    active_label: str,
) -> tuple[subprocess.Popen[str] | None, datetime, datetime, datetime, datetime, datetime, str, str]:
    # Lazy import to break the bot_daemon -> telegram_commands -> bot_daemon cycle.
    from bot_daemon import ROOT, _child_env, start_job

    stripped = (text or "").strip()
    if not stripped:
        return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
    command = stripped.split()[0].lower()
    if command.startswith("/run"):
        if run_proc and run_proc.poll() is None:
            _safe_notify("⏳ 当前已有任务在执行。")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
        _safe_notify("💬 回复\n\n✅ 已收到 /run，开始执行。")
        return start_job("run_once.py", "telegram"), next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, "telegram", "run_once.py"

    if command.startswith("/status"):
        _safe_notify(status_text(run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, active_label))
        return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label

    if command.startswith("/update"):
        if run_proc and run_proc.poll() is None:
            _safe_notify("⏳ 当前已有任务在执行，暂不更新。请等当前任务结束后再发送 /update。")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
        _safe_notify("🔄 更新\n\n✅ 已收到 /update，开始更新：拉取最新代码、检查并重启。更新完成后会再通知你。")
        update_proc = _start_update_process(ROOT, _child_env())
        return update_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, "telegram", "scripts/update_bot.sh"

    if command.startswith("/config"):
        from src.config_manager import handle_config_command

        body = stripped[len(command):].strip()
        _safe_notify(handle_config_command(body))
        return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label

    if command.startswith("/post_once"):
        if run_proc and run_proc.poll() is None:
            _safe_notify("⏳ 当前已有任务在执行。")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
        _safe_notify("📝 主动发帖\n\n✅ 已收到 /post_once，开始执行。")
        return start_job("post_once.py", "telegram"), next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, "telegram", "post_once.py"

    if command.startswith("/post_dry_run"):
        if run_proc and run_proc.poll() is None:
            _safe_notify("⏳ 当前已有任务在执行。")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
        _safe_notify("📝 主动发帖\n\n🧪 已收到 /post_dry_run，开始生成候选但不会发送。")
        return (
            subprocess.Popen(
                [sys.executable, str(ROOT / "post_once.py"), "--trigger", "telegram", "--dry-run"],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=_child_env(),
            ),
            next_run_at,
            next_post_run_at,
            next_learn_at,
            next_revisit_at,
            next_hotspot_at,
            "telegram",
            "post_once.py --dry-run",
        )

    if command.startswith("/post_status"):
        _safe_notify(post_summary(next_post_run_at))
        return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label

    if command.startswith("/learn_status"):
        _safe_notify(learning_summary(next_learn_at))
        return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label

    if command.startswith("/learn_once"):
        if run_proc and run_proc.poll() is None:
            _safe_notify("⏳ 当前已有任务在执行。")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
        _safe_notify("👀 观察学习\n\n✅ 已收到 /learn_once，开始执行。")
        return start_job("src/learning/observe.py", "telegram"), next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, "telegram", "src/learning/observe.py"

    if command.startswith("/revisit_status"):
        _safe_notify(revisit_summary(next_revisit_at))
        return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label

    if command.startswith("/revisit_once"):
        if run_proc and run_proc.poll() is None:
            _safe_notify("⏳ 当前已有任务在执行。")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
        _safe_notify("📈 反馈回访\n\n✅ 已收到 /revisit_once，开始执行。")
        return start_job("src/learning/revisit.py", "telegram"), next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, "telegram", "src/learning/revisit.py"

    if command.startswith("/hotspot_discover"):
        if run_proc and run_proc.poll() is None:
            _safe_notify("⏳ 当前已有任务在执行。")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
        _safe_notify("🔥 热点发现\n\n✅ 已收到 /hotspot_discover，开始执行。")
        return start_job("discover_hotspots.py", "telegram"), next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, "telegram", "discover_hotspots.py"

    if command.startswith("/hotspot_status"):
        _safe_notify(hotspot_summary(next_hotspot_at))
        return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label

    if command.startswith("/event"):
        body = stripped[len("/event"):].strip()
        if not body:
            _safe_notify("⚠️ 用法：/event <事件描述>，例如：/event 今天和朋友聊了关于XX的事")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
        try:
            evt = persona_add_event(body)
            _safe_notify(f"✅ 已记录事件\n\n📅 {evt['time_beijing']}\n📝 {evt['raw']}")
        except Exception as exc:
            logger.warning(f"persona_add_event failed: {exc}")
            _safe_notify(f"❌ 记录失败：{exc}")
        return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label

    if command.startswith("/review"):
        try:
            parts = stripped.split()
            days = int(parts[1]) if len(parts) >= 2 else 3
            days = max(1, min(days, 30))
        except (ValueError, IndexError):
            days = 3
        from src.context_builder import scan_reviewable_entries
        entries = scan_reviewable_entries(days=days)
        if not entries:
            _safe_notify(f"📋 最近 {days} 天内没有待评价的条目。")
        else:
            lines = [f"📋 最近 {days} 天内待评价条目（共 {len(entries)} 条）："]
            for e in entries:
                icon = "💬" if e["kind"] == "reply" else "📝"
                kind_label = "回复" if e["kind"] == "reply" else "帖子"
                text_snippet = e["text_preview"].replace("\n", " ")
                lines.append(f"{icon} `{e['stamp']}` {kind_label}: {text_snippet}")
            _safe_notify("\n".join(lines))
        return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label

    if command.startswith("/rate"):
        parts = stripped.split()
        try:
            stamp = parts[1]
            score = int(parts[2])
            if score < 1 or score > 5:
                _safe_notify("⚠️ 评分需在 1-5 之间。用法：/rate <id> <1-5> [点评]")
                return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
            comment = " ".join(parts[3:]) if len(parts) >= 4 else ""
        except (IndexError, ValueError):
            _safe_notify("⚠️ 用法：/rate <id> <1-5> [点评]。例如：/rate 20260506_205028 4 不错，很自然")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
        from src.context_builder import write_feedback
        updated = write_feedback(stamp, score, comment)
        if not updated:
            _safe_notify(f"❌ 未找到条目 `{stamp}`。先用 /review 查看可评价的条目。")
        else:
            icon = "💬" if "reply_text" in updated else "📝"
            text = str(updated.get("reply_text") or updated.get("post_text") or updated.get("best_effort_post_text") or "")[:60]
            stars = "⭐" * score
            confirm = f"{icon} 已评分\n\n{stars} {score}/5\n{text}"
            if comment:
                confirm += f"\n\n💭 {comment}"
            _safe_notify(confirm)
        return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label

    return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label


def poll_updates(
    run_proc: subprocess.Popen[str] | None,
    next_run_at: datetime,
    next_post_run_at: datetime,
    next_learn_at: datetime,
    next_revisit_at: datetime,
    next_hotspot_at: datetime,
    run_trigger: str,
    active_label: str,
) -> tuple[subprocess.Popen[str] | None, datetime, datetime, datetime, datetime, datetime, str, str]:
    if not telegram_enabled():
        return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label

    state = read_tg_state()
    initial_offset = state.get("update_offset", 0)
    params = {
        "timeout": 1,
        "offset": initial_offset,
    }
    data = tg_api("getUpdates", params=params, timeout=5)
    if not data.get("ok", True):
        # Telegram returned an error (rate limit, bad token, etc.). Don't
        # advance the offset and back off so we don't hammer the API into a
        # harder 429.
        description = data.get("description") or "(no description)"
        logger.warning(f"telegram getUpdates not ok: {description}")
        time.sleep(5)
        return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
    results = data.get("result") or []
    allowed_chat = telegram_chat_id()
    new_offset = initial_offset
    for item in results:
        update_id = int(item.get("update_id", 0))
        new_offset = max(new_offset, update_id + 1)
        try:
            message = item.get("message") or {}
            chat = message.get("chat") or {}
            text = str(message.get("text") or "")
            if str(chat.get("id") or "") != allowed_chat:
                continue
            run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label = handle_command(
                text,
                run_proc,
                next_run_at,
                next_post_run_at,
                next_learn_at,
                next_revisit_at,
                next_hotspot_at,
                run_trigger,
                active_label,
            )
        except Exception as exc:
            logger.warning(f"telegram update {update_id} handler error: {exc}")
    # Persist offset once after the whole batch — avoids the previous bug
    # where every update triggered a fresh non-atomic file write, and any
    # crash mid-loop could lose offset progress or replay updates.
    if new_offset != initial_offset:
        try:
            write_tg_state({"update_offset": new_offset})
        except Exception as exc:
            logger.warning(f"telegram offset persist failed: {exc}")
    return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
