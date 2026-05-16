#!/usr/bin/env python3
"""Telegram-summary builders + daily/nightly report dispatchers.

Extracted from bot_daemon.py. Each ``*_summary`` function returns a
ready-to-send Chinese text block; ``maybe_send_*`` dispatchers gate on
state files so we send each report at most once per window.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta

from src import job_store, postable_pool
from src.common import (
    DAILY_REPORT_STATE_PATH,
    HISTORY_DIR,
    LATEST_HOTSPOT_RUN_PATH,
    LATEST_POST_RUN_PATH,
    LATEST_REVISIT_RUN_PATH,
    LATEST_RUN_PATH,
    POST_HISTORY_DIR,
    REVISIT_REPORT_STATE_PATH,
    load_json,
    telegram_enabled,
    telegram_notify,
    write_json,
)
from src.learning.store import learning_counts, top_learning_posts
from src.logger import get_logger
from src.scheduling import (
    BEIJING_TZ,
    REVISIT_WINDOW_START_HOUR,
    _beijing_now,
    hotspot_enabled,
    in_revisit_window,
    learning_enabled,
)

logger = get_logger(__name__)


def format_header(title: str) -> list[str]:
    return [title, ""]


def format_kv(icon: str, label: str, value) -> str:
    return f"{icon} {label}: {value}"


def _format_schedule_time(ts: datetime, now: datetime, active: bool) -> str:
    formatted = ts.strftime('%Y-%m-%d %H:%M:%S %Z')
    if ts > now:
        return formatted
    if active:
        return f"当前任务完成后重算（原定 {formatted}）"
    return f"已到点，等待调度循环（原定 {formatted}）"


def _job_queue_lines(active: dict | None, queued: list[dict], recent_bad: list[dict]) -> list[str]:
    lines: list[str] = []
    if active:
        lines.append(format_kv("⏳", "当前", f"正在执行 #{active['id']} {active['label']} ({active.get('trigger', '')})"))
    else:
        lines.append(format_kv("✅", "当前", "空闲"))
    lines.append(format_kv("📚", "队列", f"{len(queued)} 个"))
    for job in queued:
        lines.append(f"  #{job['id']} {job['label']} ({job.get('trigger', '')})")
    if recent_bad:
        lines.append(format_kv("⚠️", "最近异常", ""))
        for job in recent_bad:
            lines.append(f"  #{job['id']} {job['label']} {job.get('status', '')}")
    return lines


def latest_summary() -> str:
    latest = load_json(LATEST_RUN_PATH, {})
    if not latest:
        return "ℹ️ 最近还没有成功记录。"
    action = latest.get("action", "reply")
    action_label = {
        "reply": "💬 回复",
        "quote": "🔁 引用",
        "repost": "🔄 转发",
    }.get(action, "💬 回复")
    lines = [
        format_kv("🕒", "最近时间", latest.get("time_beijing", "")),
        format_kv("⚙️", "最近触发", latest.get("trigger", "")),
        format_kv("🎬", "操作类型", action_label),
        format_kv("🔗", "帖子", latest.get("post_url", "")),
        format_kv("🎯", "选中理由", latest.get("selection_reason", "")),
        format_kv("💭", "内容", latest.get("reply_text", "")),
        format_kv("🧠", "理由", latest.get("reply_reason", "")),
        format_kv("💰", "本次 Cost", f"{float(latest.get('total_cost_cny') or 0.0):.6f} 元"),
    ]
    return "\n".join(lines)


def status_text(
    run_proc: subprocess.Popen[str] | None,
    next_run_at: datetime,
    next_post_run_at: datetime,
    next_learn_at: datetime,
    next_revisit_at: datetime,
    next_hotspot_at: datetime,
    active_label: str,
) -> str:
    now = _beijing_now()
    active_job = job_store.active_job()
    queued = job_store.queued_jobs(limit=5)
    recent_bad = job_store.recent_jobs(["failed", "timed_out", "interrupted"], limit=3)
    lines = format_header("📊 Bot 状态")
    lines.extend(_job_queue_lines(active_job, queued, recent_bad))
    lines.append(format_kv("🕒", "现在", now.strftime('%Y-%m-%d %H:%M:%S %Z')))
    active = bool(active_job)
    lines.append(format_kv("💬", "下次回复", _format_schedule_time(next_run_at, now, active)))
    lines.append(format_kv("📝", "下次主动发帖", _format_schedule_time(next_post_run_at, now, active)))
    if learning_enabled():
        lines.append(format_kv("👀", "下次观察学习", _format_schedule_time(next_learn_at, now, active)))
    lines.append(format_kv("📈", "下次反馈回访", _format_schedule_time(next_revisit_at, now, active)))
    if hotspot_enabled():
        lines.append(format_kv("🔥", "下次热点发现", _format_schedule_time(next_hotspot_at, now, active)))
    lines.append("")
    lines.append(latest_summary())
    return "\n".join(lines)


def count_scheduled_posts(date_str: str) -> int:
    history_dir = POST_HISTORY_DIR
    total = 0
    for path in sorted(history_dir.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        # NOTE: telegram-triggered posts intentionally not counted toward
        # daily limit (manual override). This is asymmetric with
        # count_hotspot_posts_today which counts all triggers — do not
        # "fix" by removing the trigger filter without owner sign-off.
        if item.get("date_beijing") == date_str and item.get("trigger") == "schedule":
            total += 1
    return total


def post_daily_limit() -> int:
    try:
        return max(1, int(os.environ.get("X_POST_DAILY_LIMIT", "4")))
    except ValueError:
        return 4


def post_summary(next_post_run_at: datetime) -> str:
    latest = load_json(LATEST_POST_RUN_PATH, {})
    pool = postable_pool.pool_status()
    manual = pool["manual"]
    hotspot = pool["hotspot"]
    today = _beijing_now().strftime("%Y-%m-%d")
    lines = format_header("📝 主动发帖状态")
    lines.extend(
        [
            format_kv("📥", "人工待发", manual["pending"]),
            format_kv("✅", "人工已用", manual["used"]),
            format_kv("⏭️", "人工跳过", manual["skipped"]),
            format_kv("🔥", "热点池(24h)", hotspot["pool_size_24h"]),
            format_kv("🌱", "今日新发现热点", hotspot["discovered_today"]),
            format_kv("📤", "今日已发热点", hotspot["posted_today"]),
            format_kv("📅", "今日定时已发", f"{count_scheduled_posts(today)}/{post_daily_limit()}"),
            format_kv("🕒", "下次主动发帖", next_post_run_at.strftime('%Y-%m-%d %H:%M:%S %Z')),
        ]
    )
    if latest:
        lines.extend(
            [
                "",
                format_kv("🕒", "最近时间", latest.get("time_beijing", "")),
                format_kv("📌", "最近状态", latest.get("status", "")),
                format_kv("⚙️", "最近触发", latest.get("trigger", "")),
                format_kv("🧩", "最近选题", latest.get("topic_text", "")),
                format_kv("📄", "最近发帖", latest.get("post_text", "")),
                format_kv("💰", "最近 Cost", f"{float(latest.get('total_cost_cny') or 0.0):.6f} 元"),
            ]
        )
    return "\n".join(lines)


def learning_summary(next_learn_at: datetime) -> str:
    counts = learning_counts()
    top_posts = top_learning_posts(limit=3)
    lines = format_header("👀 观察学习状态")
    lines.extend(
        [
            format_kv("📚", "样本总数", counts["total"]),
            format_kv("⭐", "高质量", counts["high_quality"]),
            format_kv("👁️", "值得观察", counts["worth_watching"]),
            format_kv("🕒", "最近时间", counts["latest_time"]),
            format_kv("📌", "最近状态", counts["latest_status"]),
            format_kv("⏭️", "下次观察学习", next_learn_at.strftime('%Y-%m-%d %H:%M:%S %Z')),
        ]
    )
    if top_posts:
        lines.append("")
        lines.append("🏷️ 最近高质量样本:")
        for item in top_posts:
            lines.append(
                f"- @{item.get('author_handle', '')} | {item.get('quality_label', '')} | "
                f"{str(item.get('post_text') or '')[:80]}"
            )
    return "\n".join(lines)


def _revisit_record_eligible(rec: dict, kind: str) -> bool:
    if kind == "post":
        if rec.get("dry_run") or rec.get("status") != "posted":
            return False
    else:
        rc = rec.get("send_returncode")
        if rc is None or int(rc) != 0:
            return False
        if not str(rec.get("reply_url") or "").strip():
            return False
        if not (rec.get("reply_text") or rec.get("reply") or "").strip():
            return False
        return bool(rec.get("reply_url"))
    return bool(rec.get("post_url"))


def revisit_counts() -> dict:
    """Aggregate state across both post and reply histories for status output."""
    pending = 0
    succeeded = 0
    failed = 0
    skipped = 0  # dry-runs / not-posted / no URL
    waiting = 0  # posted but <24h old
    now = _beijing_now()
    for kind, directory in (("post", POST_HISTORY_DIR), ("reply", HISTORY_DIR)):
        for path in sorted(directory.glob("*.json")):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not _revisit_record_eligible(rec, kind):
                skipped += 1
                continue
            eng = rec.get("engagement_24h") or {}
            if eng.get("metrics"):
                succeeded += 1
                continue
            if eng.get("failed"):
                failed += 1
                continue
            # time_beijing format: "YYYY-MM-DD HH:MM:SS <TZ>" where the
            # trailing token has historically been "CST", "+0800", or even
            # "Asia/Shanghai" depending on the writer. Strip the trailing
            # tz token and apply Asia/Shanghai directly — all writers in this
            # repo emit Beijing local time regardless of the suffix.
            posted_at = None
            time_str = (rec.get("time_beijing") or "").strip()
            if time_str:
                parts = time_str.split()
                if len(parts) >= 2:
                    date_part, time_part = parts[0], parts[1]
                    try:
                        posted_at = datetime.strptime(
                            f"{date_part} {time_part}", "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=BEIJING_TZ)
                    except Exception:
                        posted_at = None
            if posted_at and (now - posted_at) < timedelta(hours=24):
                waiting += 1
            else:
                pending += 1
    return {
        "pending": pending,
        "waiting_24h": waiting,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
    }


def revisit_summary(next_revisit_at: datetime) -> str:
    counts = revisit_counts()
    latest = load_json(LATEST_REVISIT_RUN_PATH, {})
    lines = format_header("📈 反馈回访状态")
    lines.extend(
        [
            format_kv("⏳", "待回访(>24h)", counts["pending"]),
            format_kv("⌛", "等待中(<24h)", counts["waiting_24h"]),
            format_kv("✅", "已成功", counts["succeeded"]),
            format_kv("⚠️", "失败标记", counts["failed"]),
            format_kv("⏭️", "跳过", counts["skipped"]),
            format_kv("🌙", "下次回访", next_revisit_at.strftime('%Y-%m-%d %H:%M:%S %Z')),
        ]
    )
    if latest:
        lines.extend(
            [
                "",
                format_kv("🕒", "最近时间", latest.get("time_beijing", "")),
                format_kv("⚙️", "最近触发", latest.get("trigger", "")),
                format_kv("📊", "最近处理", f"{latest.get('processed', 0)} 条 (✅{latest.get('succeeded', 0)} ⚠️{latest.get('failed', 0)})"),
            ]
        )
    return "\n".join(lines)


def count_hotspot_posts_today(date_str: str) -> int:
    total = 0
    for path in sorted(POST_HISTORY_DIR.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if item.get("date_beijing") == date_str and item.get("topic_source") == "hotspot":
            total += 1
    return total


def hotspot_daily_limit() -> int:
    try:
        return max(1, int(os.environ.get("X_HOTSPOT_DAILY_LIMIT", "3")))
    except ValueError:
        return 3


def hotspot_summary(next_hotspot_at: datetime) -> str:
    from src.hotspot.store import hotspot_stats
    stats = hotspot_stats()
    latest = load_json(LATEST_HOTSPOT_RUN_PATH, {})
    today = _beijing_now().strftime("%Y-%m-%d")
    lines = format_header("🔥 热点发现状态")
    lines.extend([
        format_kv("📊", "今日发现", stats["today_discovered"]),
        format_kv("📚", "历史总计", stats["total_discovered"]),
        format_kv("📅", "今日热点发帖", f"{count_hotspot_posts_today(today)}/{hotspot_daily_limit()}"),
        format_kv("🕒", "下次发现", next_hotspot_at.strftime('%Y-%m-%d %H:%M:%S %Z')),
    ])
    if latest:
        lines.extend([
            "",
            format_kv("🕒", "最近时间", latest.get("time_beijing", "")),
            format_kv("⚙️", "最近触发", latest.get("trigger", "")),
            format_kv("📊", "最近发现", f"{latest.get('discovered', 0)} 条 (✅{latest.get('added', 0)})"),
        ])
    return "\n".join(lines)


def maybe_send_revisit_report(now: datetime, run_proc: subprocess.Popen[str] | None) -> None:
    """After the midnight revisit has run, push a 24h engagement digest."""
    if not telegram_enabled() or (run_proc and run_proc.poll() is None):
        return
    if not in_revisit_window(now):
        return

    state = load_json(REVISIT_REPORT_STATE_PATH, {"last_reported_window": ""})
    # Revisit now runs in the 00:00 hour; the report key is that Beijing date.
    window_start_date = (now if now.hour >= REVISIT_WINDOW_START_HOUR else now - timedelta(days=1)).strftime("%Y-%m-%d")
    if state.get("last_reported_window") == window_start_date:
        return

    # Only summarize records that got their metrics filled today.
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    completed_today: list[dict] = []
    for kind, directory in (("post", POST_HISTORY_DIR), ("reply", HISTORY_DIR)):
        for path in sorted(directory.glob("*.json")):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            eng = rec.get("engagement_24h") or {}
            metrics = eng.get("metrics")
            if not metrics:
                continue
            checked_at = str(eng.get("checked_at") or "")
            if today not in checked_at and yesterday not in checked_at:
                continue
            completed_today.append(
                {"kind": kind, "record": rec, "metrics": metrics, "score": eng.get("score") or 0}
            )

    if not completed_today:
        # Nothing to report yet; try again on the next loop tick.
        return

    completed_today.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    n_posts = sum(1 for it in completed_today if it["kind"] == "post")
    n_replies = sum(1 for it in completed_today if it["kind"] == "reply")
    lines = format_header("📈 24h 反馈汇总")
    lines.append(format_kv("🌙", "窗口日期", window_start_date))
    lines.append(format_kv("📊", "已回访", f"{len(completed_today)} (📝{n_posts} 💬{n_replies})"))
    lines.append("")
    for item in completed_today[:5]:
        rec = item["record"]
        m = item["metrics"]
        if item["kind"] == "post":
            text = str(rec.get("post_text") or "")[:60]
            tag = "📝"
        else:
            text = str(rec.get("reply_text") or rec.get("reply") or "")[:60]
            tag = "💬"
        lines.append(
            f"• {tag} {rec.get('time_beijing', '')[:16]} 👁️{m.get('views',0)} 💚{m.get('likes',0)} 💬{m.get('replies',0)} 🔁{m.get('reposts',0)}"
        )
        lines.append(f"  {text}")
    try:
        telegram_notify("\n".join(lines))
        write_json(REVISIT_REPORT_STATE_PATH, {"last_reported_window": window_start_date})
    except Exception as exc:
        logger.warning(f"revisit report failed: {exc}")


def aggregate_daily_costs(date_str: str) -> dict:
    history_dir = HISTORY_DIR
    records = []
    for path in sorted(history_dir.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if item.get("date_beijing") == date_str:
            records.append(item)

    schedule_records = [item for item in records if item.get("trigger") == "schedule"]
    manual_records = [item for item in records if item.get("trigger") != "schedule"]
    return {
        "date_beijing": date_str,
        "all_runs": len(records),
        "schedule_runs": len(schedule_records),
        "manual_runs": len(manual_records),
        "all_cost_cny": round(sum(float(item.get("total_cost_cny") or 0.0) for item in records), 8),
        "schedule_cost_cny": round(sum(float(item.get("total_cost_cny") or 0.0) for item in schedule_records), 8),
        "manual_cost_cny": round(sum(float(item.get("total_cost_cny") or 0.0) for item in manual_records), 8),
        "reply_count": sum(1 for item in records if item.get("action") == "reply"),
        "quote_count": sum(1 for item in records if item.get("action") == "quote"),
        "repost_count": sum(1 for item in records if item.get("action") == "repost"),
    }


def maybe_send_daily_cost_report(now: datetime, run_proc: subprocess.Popen[str] | None) -> None:
    if not telegram_enabled() or (run_proc and run_proc.poll() is None) or now.hour < 23:
        return

    state = load_json(DAILY_REPORT_STATE_PATH, {"last_reported_date": ""})
    today = now.strftime("%Y-%m-%d")
    if state.get("last_reported_date") == today:
        return

    summary = aggregate_daily_costs(today)
    lines = [
        "💰 每日 Cost 汇总",
        "",
        format_kv("📅", "日期", summary["date_beijing"]),
        format_kv("💬", "定时运行次数", summary["schedule_runs"]),
        format_kv("💰", "定时总 Cost", f"{summary['schedule_cost_cny']:.6f} 元"),
        format_kv("📊", "全部运行次数", summary["all_runs"]),
        format_kv("🧾", "全部总 Cost", f"{summary['all_cost_cny']:.6f} 元"),
    ]
    action_parts = []
    if summary["reply_count"]:
        action_parts.append(f"💬 回复 x{summary['reply_count']}")
    if summary["quote_count"]:
        action_parts.append(f"🔁 引用 x{summary['quote_count']}")
    if summary["repost_count"]:
        action_parts.append(f"🔄 转发 x{summary['repost_count']}")
    if action_parts:
        lines.append(format_kv("🎬", "操作分布", "  ".join(action_parts)))
    telegram_notify("\n".join(lines))
    write_json(DAILY_REPORT_STATE_PATH, {"last_reported_date": today})
