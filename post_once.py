#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import fcntl
import sys
from datetime import datetime
from pathlib import Path

from src.common import (
    LATEST_POST_RUN_PATH,
    POST_LOCK_PATH,
    ensure_state_dirs,
    load_env_file,
    parse_json_object,
    post_history_path_for,
    write_json,
)
from src import postable_pool
from src.logger import get_logger
from src.persona_store import add_recent_post
from src.post.article import _handle_article
from src.post.article_generate import generate_article_plan
from src.post.handlers_common import notify_telegram, run, topic_extra_update
from src.post.post_generate import generate_post_plan
from src.post.thread import _handle_thread
from src.post.thread_generate import generate_thread_plan
from src.post.topic_auto import generate_auto_topic

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent


def notify_text(record: dict) -> str:
    """Build the single-post Telegram notify text.

    Thread / article handlers build their own text via
    ``src.post.thread._thread_notify_text`` /
    ``src.post.article._article_notify_text`` — single-post is the only
    shape this function handles.
    """
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
    if record.get("image_query"):
        lines.extend(["", f"🖼️ 配图: {record['image_query']}"])
    if record.get("post_url"):
        lines.extend(["", f"🔗 帖子链接: {record['post_url']}"])
    text = "\n".join(lines)
    if len(text) > 3800:
        return text[:3750] + "\n\n[通知过长，已截断；完整内容见本机 state/post_history]"
    return text


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
    logger.info("post_once start trigger=%s dry_run=%s stamp=%s", args.trigger, args.dry_run, stamp)

    try:
        topic = postable_pool.next_topic_to_post()
        if not topic:
            try:
                logger.info("post_once generating auto topic")
                topic = generate_auto_topic()
            except Exception as exc:
                logger.error("post_once auto_topic failed: %s", exc, exc_info=True)
                payload = {
                    "time_beijing": started.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "trigger": args.trigger,
                    "dry_run": args.dry_run,
                    "status": "auto_topic_failed",
                    "error": str(exc),
                }
                write_json(LATEST_POST_RUN_PATH, payload)
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 1

        if topic.get("type") == "thread":
            plan = generate_thread_plan(topic)
            return _handle_thread(topic, plan, args, started, stamp, lock_fh)

        if topic.get("type") == "article":
            plan = generate_article_plan(topic)
            return _handle_article(topic, plan, args, started, stamp, lock_fh)

        plan = generate_post_plan(topic)
        selected_candidate = plan.get("selected_candidate")
        best_candidate = plan.get("best_candidate") or selected_candidate
        image_query = str((selected_candidate or {}).get("image_query") or "").strip()
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
            "image_query": image_query if image_query else "",
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
            has_best = bool(best_candidate and best_candidate.get("text", "").strip())
            record["status"] = "skipped_no_candidate"
            if has_best:
                record["status"] = "skipped_rewritten"
            if not args.dry_run and topic.get("_pool") in ("manual", "hotspot"):
                postable_pool.mark_topic_used(
                    topic,
                    status="skipped",
                    extra=topic_extra_update(record["status"], record["time_beijing"], dry_run=False),
                )
            write_json(LATEST_POST_RUN_PATH, record)
            write_json(post_history_path_for(stamp), record)
            notify_telegram(record, stamp, notify_text(record))
            print(json.dumps(record, ensure_ascii=False, indent=2))
            # skipped_rewritten means review caught a bad ending but rewrite
            # produced a usable candidate — not a real failure
            return 0 if (args.dry_run or has_best) else 1

        if args.dry_run:
            record["status"] = "dry_run_ready"
            write_json(LATEST_POST_RUN_PATH, record)
            write_json(post_history_path_for(stamp), record)
            notify_telegram(record, stamp, notify_text(record))
            print(json.dumps(record, ensure_ascii=False, indent=2))
            return 0

        send_cmd = [
            sys.executable, str(ROOT / "src/post/post_send.py"),
            "--text", record["post_text"],
        ]
        if image_query:
            send_cmd.extend(["--image-query", image_query])
        send = run(send_cmd)
        sys.stdout.write(send.stdout)
        sys.stderr.write(send.stderr)
        record["send_returncode"] = send.returncode
        record["send_stdout"] = send.stdout
        record["send_stderr"] = send.stderr

        # Parse image cost from send output
        image_cost_cny = 0.0
        image_info = {}
        for line in (send.stdout or "").splitlines():
            if line.startswith("IMAGE_INFO: "):
                try:
                    image_info = json.loads(line[len("IMAGE_INFO: "):])
                    image_cost_cny = float(image_info.get("cost_cny") or 0)
                except Exception:
                    pass
        record["image_info"] = image_info
        record["total_cost_cny"] = round(
            float(record["total_cost_cny"]) + image_cost_cny,
            8,
        )

        send_payload = {}
        if send.returncode == 0 and send.stdout.strip():
            try:
                send_payload = parse_json_object(send.stdout)
            except Exception as exc:
                record["send_parse_error"] = str(exc)
        record["post_url"] = str(send_payload.get("url") or "").strip()
        record["status"] = "posted" if send.returncode == 0 else "send_failed"

        if send.returncode == 0:
            if topic.get("_pool") in ("manual", "hotspot"):
                postable_pool.mark_topic_used(
                    topic,
                    status="used",
                    extra=topic_extra_update(record["status"], record["time_beijing"], dry_run=False),
                )
            add_recent_post(record["post_text"], str(topic.get("type", "")))

        write_json(LATEST_POST_RUN_PATH, record)
        write_json(post_history_path_for(stamp), record)
        # HIGH-5: TG 失败一定不能让本函数抛/返回非 0。post_send 已成功的情况下，
        # 上抛会让 bot_daemon 把整次当作"发送失败"再发一条失败通知，引导用户重发，
        # 但实际帖子已经发出去 → 重发风险。所以独立 try/except，结果写进 record。
        notify_telegram(record, stamp, notify_text(record))
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
