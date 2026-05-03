#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
import random
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from common import (
    DAILY_REPORT_STATE_PATH,
    LATEST_POST_RUN_PATH,
    LATEST_RUN_PATH,
    TELEGRAM_STATE_PATH,
    ensure_state_dirs,
    load_env_file,
    load_json,
    post_topic_summary,
    telegram_chat_id,
    telegram_enabled,
    telegram_notify,
    telegram_token,
    write_json,
)
from learning_store import learning_counts, top_learning_posts
from persona_store import add_event as persona_add_event

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "state" / "logs" / "bot.log"
LOCK_PATH = ROOT / "state" / "bot.lock"


def format_header(title: str) -> list[str]:
    return [title, ""]


def format_kv(icon: str, label: str, value) -> str:
    return f"{icon} {label}: {value}"


def log(message: str) -> None:
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


def _safe_notify(text: str) -> None:
    if not telegram_enabled():
        return
    try:
        telegram_notify(text)
    except Exception as exc:
        log(f"telegram_notify failed: {exc}")


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
    write_json(TELEGRAM_STATE_PATH, state)


def next_scheduled_after(now: datetime) -> datetime:
    jitter_seconds = int(os.environ.get("X_REPLY_JITTER_SECONDS", "1800"))
    cursor = now.replace(minute=0, second=0, microsecond=0)
    while True:
        if 7 <= cursor.hour <= 23:
            random.seed(cursor.strftime("%Y%m%d%H"))
            candidate = cursor + timedelta(seconds=random.randint(0, jitter_seconds))
            if candidate > now:
                return candidate
        cursor += timedelta(hours=1)


def proactive_schedule_hours() -> list[int]:
    raw = os.environ.get("X_POST_SCHEDULE_HOURS", "11,19")
    hours: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hour = int(part)
        except ValueError:
            continue
        if 0 <= hour <= 23:
            hours.append(hour)
    return sorted(set(hours)) or [11, 19]


def next_proactive_after(now: datetime) -> datetime:
    jitter_seconds = int(os.environ.get("X_POST_JITTER_SECONDS", "1800"))
    hours = proactive_schedule_hours()
    base_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for day_offset in range(0, 14):
        day = base_day + timedelta(days=day_offset)
        for hour in hours:
            candidate_base = day.replace(hour=hour)
            random.seed("post-" + candidate_base.strftime("%Y%m%d%H"))
            candidate = candidate_base + timedelta(seconds=random.randint(0, jitter_seconds))
            if candidate > now:
                return candidate
    fallback = base_day + timedelta(days=1)
    return fallback.replace(hour=hours[0], minute=0, second=0, microsecond=0)


def learning_enabled() -> bool:
    return os.environ.get("X_LEARN_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def learning_interval_seconds() -> int:
    try:
        return max(300, int(os.environ.get("X_LEARN_INTERVAL_SECONDS", "900")))
    except ValueError:
        return 900


def learning_guard_seconds() -> int:
    try:
        return max(60, int(os.environ.get("X_LEARN_GUARD_SECONDS", "600")))
    except ValueError:
        return 600


def next_learning_after(now: datetime) -> datetime:
    return now + timedelta(seconds=learning_interval_seconds())


def latest_summary() -> str:
    latest = load_json(LATEST_RUN_PATH, {})
    if not latest:
        return "ℹ️ 最近还没有成功记录。"
    lines = [
        format_kv("🕒", "最近时间", latest.get("time_beijing", "")),
        format_kv("⚙️", "最近触发", latest.get("trigger", "")),
        format_kv("🔗", "帖子", latest.get("post_url", "")),
        format_kv("🎯", "选中理由", latest.get("selection_reason", "")),
        format_kv("💭", "回复", latest.get("reply_text", "")),
        format_kv("🧠", "回复理由", latest.get("reply_reason", "")),
        format_kv("💰", "本次 Cost", f"{float(latest.get('total_cost_cny') or 0.0):.6f} 元"),
    ]
    return "\n".join(lines)


def status_text(
    run_proc: subprocess.Popen[str] | None,
    next_run_at: datetime,
    next_post_run_at: datetime,
    next_learn_at: datetime,
    active_label: str,
) -> str:
    now = datetime.now().astimezone()
    lines = format_header("📊 Bot 状态")
    if run_proc and run_proc.poll() is None:
        lines.append(format_kv("⏳", "当前", f"正在执行 {active_label}"))
    else:
        lines.append(format_kv("✅", "当前", "空闲"))
    lines.append(format_kv("🕒", "现在", now.strftime('%Y-%m-%d %H:%M:%S %Z')))
    lines.append(format_kv("💬", "下次回复", next_run_at.strftime('%Y-%m-%d %H:%M:%S %Z')))
    lines.append(format_kv("📝", "下次主动发帖", next_post_run_at.strftime('%Y-%m-%d %H:%M:%S %Z')))
    if learning_enabled():
        lines.append(format_kv("👀", "下次观察学习", next_learn_at.strftime('%Y-%m-%d %H:%M:%S %Z')))
    lines.append("")
    lines.append(latest_summary())
    return "\n".join(lines)


def count_scheduled_posts(date_str: str) -> int:
    history_dir = ROOT / "state" / "post_history"
    total = 0
    for path in sorted(history_dir.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if item.get("date_beijing") == date_str and item.get("trigger") == "schedule":
            total += 1
    return total


def post_daily_limit() -> int:
    try:
        return max(1, int(os.environ.get("X_POST_DAILY_LIMIT", "2")))
    except ValueError:
        return 2


def post_summary(next_post_run_at: datetime) -> str:
    latest = load_json(LATEST_POST_RUN_PATH, {})
    queue = post_topic_summary()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    lines = format_header("📝 主动发帖状态")
    lines.extend(
        [
            format_kv("📥", "queue_pending", queue["pending"]),
            format_kv("✅", "queue_used", queue["used"]),
            format_kv("⏭️", "queue_skipped", queue["skipped"]),
            format_kv("📚", "queue_total", queue["total"]),
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


def start_job(script: str, trigger: str) -> subprocess.Popen[str]:
    log(f"{script} start trigger={trigger}")
    return subprocess.Popen(
        [sys.executable, str(ROOT / script), "--trigger", trigger],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def finish_run(run_proc: subprocess.Popen[str], trigger: str, label: str) -> None:
    output = run_proc.stdout.read() if run_proc.stdout else ""
    for line in (output or "").splitlines():
        log(f"{label}[{trigger}] {line}")
    code = run_proc.returncode
    log(f"{label} end trigger={trigger} code={code}")
    if code != 0 and telegram_enabled():
        try:
            telegram_notify(
                "\n".join(
                    [
                        "⚠️ 任务失败",
                        "",
                        format_kv("🧩", "任务", label),
                        format_kv("⚙️", "触发", trigger),
                        format_kv("🔢", "exit_code", code),
                        "",
                        "📄 最近输出:",
                        (output or "").strip()[-1500:] or "(empty)",
                    ]
                )
            )
        except Exception as exc:
            log(f"telegram failure notify failed: {exc}")


def aggregate_daily_costs(date_str: str) -> dict:
    history_dir = ROOT / "state" / "history"
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
    }


def maybe_send_daily_cost_report(now: datetime, run_proc: subprocess.Popen[str] | None) -> None:
    if not telegram_enabled() or (run_proc and run_proc.poll() is None) or now.hour < 23:
        return

    state = load_json(DAILY_REPORT_STATE_PATH, {"last_reported_date": ""})
    today = now.strftime("%Y-%m-%d")
    if state.get("last_reported_date") == today:
        return

    summary = aggregate_daily_costs(today)
    telegram_notify(
        "\n".join(
            [
                "💰 每日 Cost 汇总",
                "",
                format_kv("📅", "日期", summary["date_beijing"]),
                format_kv("💬", "定时运行次数", summary["schedule_runs"]),
                format_kv("💰", "定时总 Cost", f"{summary['schedule_cost_cny']:.6f} 元"),
                format_kv("📊", "全部运行次数", summary["all_runs"]),
                format_kv("🧾", "全部总 Cost", f"{summary['all_cost_cny']:.6f} 元"),
            ]
        )
    )
    write_json(DAILY_REPORT_STATE_PATH, {"last_reported_date": today})


def handle_command(
    text: str,
    run_proc: subprocess.Popen[str] | None,
    next_run_at: datetime,
    next_post_run_at: datetime,
    next_learn_at: datetime,
    run_trigger: str,
    active_label: str,
) -> tuple[subprocess.Popen[str] | None, datetime, datetime, datetime, str, str]:
    stripped = (text or "").strip()
    if not stripped:
        return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label
    command = stripped.split()[0].lower()
    if command.startswith("/run"):
        if run_proc and run_proc.poll() is None:
            _safe_notify("⏳ 当前已有任务在执行。")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label
        _safe_notify("💬 回复\n\n✅ 已收到 /run，开始执行。")
        return start_job("run_once.py", "telegram"), next_run_at, next_post_run_at, next_learn_at, "telegram", "run_once.py"

    if command.startswith("/status"):
        _safe_notify(status_text(run_proc, next_run_at, next_post_run_at, next_learn_at, active_label))
        return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label

    if command.startswith("/post_once"):
        if run_proc and run_proc.poll() is None:
            _safe_notify("⏳ 当前已有任务在执行。")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label
        _safe_notify("📝 主动发帖\n\n✅ 已收到 /post_once，开始执行。")
        return start_job("post_once.py", "telegram"), next_run_at, next_post_run_at, next_learn_at, "telegram", "post_once.py"

    if command.startswith("/post_dry_run"):
        if run_proc and run_proc.poll() is None:
            _safe_notify("⏳ 当前已有任务在执行。")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label
        _safe_notify("📝 主动发帖\n\n🧪 已收到 /post_dry_run，开始生成候选但不会发送。")
        return (
            subprocess.Popen(
                [sys.executable, str(ROOT / "post_once.py"), "--trigger", "telegram", "--dry-run"],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            ),
            next_run_at,
            next_post_run_at,
            next_learn_at,
            "telegram",
            "post_once.py --dry-run",
        )

    if command.startswith("/post_status"):
        _safe_notify(post_summary(next_post_run_at))
        return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label

    if command.startswith("/learn_status"):
        _safe_notify(learning_summary(next_learn_at))
        return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label

    if command.startswith("/learn_once"):
        if run_proc and run_proc.poll() is None:
            _safe_notify("⏳ 当前已有任务在执行。")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label
        _safe_notify("👀 观察学习\n\n✅ 已收到 /learn_once，开始执行。")
        return start_job("observe_feed.py", "telegram"), next_run_at, next_post_run_at, next_learn_at, "telegram", "observe_feed.py"

    if command.startswith("/event"):
        body = stripped[len("/event"):].strip()
        if not body:
            _safe_notify("⚠️ 用法：/event <事件描述>，例如：/event 今天和朋友聊了关于XX的事")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label
        try:
            evt = persona_add_event(body)
            _safe_notify(f"✅ 已记录事件\n\n📅 {evt['timestamp']}\n📝 {evt['raw']}")
        except Exception as exc:
            log(f"persona_add_event failed: {exc}")
            _safe_notify(f"❌ 记录失败：{exc}")
        return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label

    return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label


def poll_updates(
    run_proc: subprocess.Popen[str] | None,
    next_run_at: datetime,
    next_post_run_at: datetime,
    next_learn_at: datetime,
    run_trigger: str,
    active_label: str,
) -> tuple[subprocess.Popen[str] | None, datetime, datetime, datetime, str, str]:
    if not telegram_enabled():
        return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label

    state = read_tg_state()
    params = {
        "timeout": 1,
        "offset": state.get("update_offset", 0),
    }
    data = tg_api("getUpdates", params=params, timeout=5)
    results = data.get("result") or []
    allowed_chat = telegram_chat_id()
    new_offset = state.get("update_offset", 0)
    for item in results:
        update_id = int(item.get("update_id", 0))
        new_offset = max(new_offset, update_id + 1)
        try:
            message = item.get("message") or {}
            chat = message.get("chat") or {}
            text = str(message.get("text") or "")
            if str(chat.get("id") or "") != allowed_chat:
                continue
            run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label = handle_command(
                text,
                run_proc,
                next_run_at,
                next_post_run_at,
                next_learn_at,
                run_trigger,
                active_label,
            )
        except Exception as exc:
            log(f"telegram update {update_id} handler error: {exc}")
        finally:
            if new_offset != state.get("update_offset", 0):
                try:
                    write_tg_state({"update_offset": new_offset})
                    state["update_offset"] = new_offset
                except Exception as exc:
                    log(f"telegram offset persist failed: {exc}")
    return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label


def main() -> int:
    load_env_file()
    ensure_state_dirs()
    lock_fh = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("bot daemon already running")
        return 0
    now = datetime.now().astimezone()
    next_run_at = next_scheduled_after(now)
    next_post_run_at = next_proactive_after(now)
    next_learn_at = next_learning_after(now)
    run_proc: subprocess.Popen[str] | None = None
    run_trigger = ""
    active_label = ""

    try:
        log("bot daemon started")
        while True:
            now = datetime.now().astimezone()

            if run_proc and run_proc.poll() is not None:
                finished_at = datetime.now().astimezone()
                carry_over_post_slot = (
                    not active_label.startswith("post_once.py")
                    and next_post_run_at <= finished_at
                )
                carry_over_reply_slot = (
                    active_label != "run_once.py"
                    and next_run_at <= finished_at
                )
                finish_run(run_proc, run_trigger, active_label or "job")
                run_proc = None
                run_trigger = ""
                active_label = ""
                next_run_at = finished_at if carry_over_reply_slot else next_scheduled_after(finished_at)
                if carry_over_post_slot:
                    next_post_run_at = finished_at
                else:
                    next_post_run_at = next_proactive_after(finished_at)
                next_learn_at = next_learning_after(finished_at)

            if run_proc is None and now >= next_run_at:
                run_proc = start_job("run_once.py", "schedule")
                run_trigger = "schedule"
                active_label = "run_once.py"
            elif run_proc is None and now >= next_post_run_at:
                queue = post_topic_summary()
                today = now.strftime("%Y-%m-%d")
                if count_scheduled_posts(today) < post_daily_limit():
                    run_proc = start_job("post_once.py", "schedule")
                    run_trigger = "schedule"
                    active_label = "post_once.py"
                next_post_run_at = next_proactive_after(now)
            elif (
                run_proc is None
                and learning_enabled()
                and now >= next_learn_at
                and (next_run_at - now).total_seconds() > learning_guard_seconds()
                and (next_post_run_at - now).total_seconds() > learning_guard_seconds()
            ):
                run_proc = start_job("observe_feed.py", "schedule")
                run_trigger = "schedule"
                active_label = "observe_feed.py"
                next_learn_at = next_learning_after(now)

            try:
                run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label = poll_updates(
                    run_proc,
                    next_run_at,
                    next_post_run_at,
                    next_learn_at,
                    run_trigger,
                    active_label,
                )
            except Exception as exc:
                log(f"telegram poll error: {exc}")

            try:
                maybe_send_daily_cost_report(now, run_proc)
            except Exception as exc:
                log(f"daily cost report error: {exc}")

            time.sleep(5)
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise
    except Exception:
        log("bot daemon crashed")
        log(traceback.format_exc())
        raise
