#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from common import (
    LATEST_POST_RUN_PATH,
    ensure_state_dirs,
    load_env_file,
    mark_post_topic_status,
    next_pending_post_topic,
    parse_json_object,
    post_history_path_for,
    telegram_enabled,
    telegram_notify,
    write_json,
)
from post_generate import generate_post_plan

ROOT = Path(__file__).resolve().parent
POST_LOCK_PATH = ROOT / "state" / "post_once.lock"


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, cwd=str(ROOT))


def notify_text(record: dict) -> str:
    shown_text = record.get("post_text") or record.get("best_effort_post_text") or "(none)"
    lines = [
        "📝 主动发帖",
        "",
        f"🕒 时间: {record['time_beijing']}",
        f"⚙️ 触发: {record['trigger']}",
        f"🧪 模式: {'dry-run' if record['dry_run'] else 'send'}",
        f"📌 状态: {record.get('status', '')}",
        f"🏷️ 类型: {record.get('topic_type', '')}",
        f"🧩 选题来源: {record['topic_source']}",
        f"📝 选题: {record['topic_text']}",
        f"🎯 选中理由: {record['selected_reason']}",
        f"💰 Cost: {record['total_cost_cny']:.6f} 元",
        "",
        "📄 发帖内容:",
        shown_text,
    ]
    if record.get("status") == "dry_run_rejected":
        lines.extend(
            [
                "",
                f"🧠 审稿结论: {record.get('review_reason', '')}",
                f"✍️ 重写提示: {record.get('review_rewrite_hint', '')}",
            ]
        )
    if record.get("post_url"):
        lines.extend(["", f"🔗 帖子链接: {record['post_url']}"])
    text = "\n".join(lines)
    if len(text) > 3800:
        return text[:3750] + "\n\n[通知过长，已截断；完整内容见本机 state/post_history]"
    return text


def topic_extra_update(status: str, stamp: str, dry_run: bool) -> dict:
    data = {
        "last_seen_at": stamp,
        "last_status": status,
    }
    if not dry_run and status == "used":
        data["used_at"] = stamp
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--trigger", default="manual")
    args = parser.parse_args()

    load_env_file()
    ensure_state_dirs()
    lock_fh = POST_LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("post_once already running")
        return 3

    started = datetime.now().astimezone()
    stamp = started.strftime("%Y%m%d_%H%M%S")

    try:
        topic = next_pending_post_topic()
        if not topic:
            payload = {
                "time_beijing": started.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "trigger": args.trigger,
                "dry_run": args.dry_run,
                "status": "no_pending_topic",
            }
            write_json(LATEST_POST_RUN_PATH, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 2

        plan = generate_post_plan(topic)
        selected_candidate = plan.get("selected_candidate")
        best_candidate = plan.get("best_candidate") or selected_candidate
        record = {
            "time_beijing": started.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "date_beijing": started.strftime("%Y-%m-%d"),
            "trigger": args.trigger,
            "dry_run": args.dry_run,
            "status": "planned",
            "topic_id": topic.get("id", ""),
            "topic_type": topic.get("type", ""),
            "topic_text": topic.get("text", ""),
            "topic_source": topic.get("source", ""),
            "topic_subject": topic.get("subject", ""),
            "topic_context": topic.get("event_or_context", ""),
            "topic_stance": topic.get("stance", ""),
            "topic_evidence_hint": topic.get("evidence_hint", ""),
            "candidates": plan.get("candidates", []),
            "selected_index": plan.get("selected_index", -1),
            "selected_reason": plan.get("selected_reason", ""),
            "review_pass": bool(plan.get("review_pass")),
            "review_reason": plan.get("review_reason", ""),
            "review_rewrite_hint": plan.get("review_rewrite_hint", ""),
            "rewritten": bool(plan.get("rewritten")),
            "post_text": (selected_candidate or {}).get("text", ""),
            "best_effort_post_text": (best_candidate or {}).get("text", ""),
            "best_effort_post_reason": (best_candidate or {}).get("reason", ""),
            "candidate_cost": plan.get("candidate_cost", {}),
            "candidate_usage": plan.get("candidate_usage", {}),
            "rerank_cost": plan.get("rerank_cost", {}),
            "rerank_usage": plan.get("rerank_usage", {}),
            "review_cost": plan.get("review_cost", {}),
            "review_usage": plan.get("review_usage", {}),
            "rewrite_cost": plan.get("rewrite_cost", {}),
            "rewrite_usage": plan.get("rewrite_usage", {}),
            "total_cost_cny": float(plan.get("total_cost_cny") or 0.0),
            "post_url": "",
        }

        if not selected_candidate:
            record["status"] = "dry_run_rejected" if args.dry_run else "skipped_no_candidate"
            if not args.dry_run:
                mark_post_topic_status(
                    str(topic.get("id") or ""),
                    "skipped",
                    topic_extra_update(record["status"], record["time_beijing"], dry_run=False),
                )
            write_json(LATEST_POST_RUN_PATH, record)
            write_json(post_history_path_for(stamp), record)
            if telegram_enabled():
                telegram_notify(notify_text(record))
            print(json.dumps(record, ensure_ascii=False, indent=2))
            return 0 if args.dry_run else 1

        if args.dry_run:
            record["status"] = "dry_run_ready"
            write_json(LATEST_POST_RUN_PATH, record)
            write_json(post_history_path_for(stamp), record)
            if telegram_enabled():
                telegram_notify(notify_text(record))
            print(json.dumps(record, ensure_ascii=False, indent=2))
            return 0

        send = run([sys.executable, str(ROOT / "post_send.py"), "--text", record["post_text"]])
        sys.stdout.write(send.stdout)
        sys.stderr.write(send.stderr)
        record["send_returncode"] = send.returncode
        record["send_stdout"] = send.stdout
        record["send_stderr"] = send.stderr
        send_payload = {}
        if send.returncode == 0 and send.stdout.strip():
            try:
                send_payload = parse_json_object(send.stdout)
            except Exception as exc:
                record["send_parse_error"] = str(exc)
        record["post_url"] = str(send_payload.get("url") or "").strip()
        record["status"] = "posted" if send.returncode == 0 else "send_failed"

        if send.returncode == 0:
            mark_post_topic_status(
                str(topic.get("id") or ""),
                "used",
                topic_extra_update(record["status"], record["time_beijing"], dry_run=False),
            )

        write_json(LATEST_POST_RUN_PATH, record)
        write_json(post_history_path_for(stamp), record)
        if telegram_enabled():
            telegram_notify(notify_text(record))
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0 if send.returncode == 0 else 1
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()


if __name__ == "__main__":
    raise SystemExit(main())
