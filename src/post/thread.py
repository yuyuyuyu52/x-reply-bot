#!/usr/bin/env python3
"""Thread-mode post handler.

Extracted from post_once.py. ``_handle_thread`` posts the head segment
via post_send.py and chains each follow-up via send_reply.py, retrying
each segment up to 3 times. The single-post and article paths live in
their own modules.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime

from src.common import (
    LATEST_POST_RUN_PATH,
    mark_post_topic_status,
    parse_json_object,
    post_history_path_for,
    write_json,
)
from src.logger import get_logger
from src.persona_store import add_recent_post
from src.post.handlers_common import (
    ROOT,
    notify_telegram,
    run,
    topic_extra_update,
)

logger = get_logger(__name__)


def _thread_notify_text(record: dict) -> str:
    segments = record.get("thread_segments", [])
    seg_lines = []
    for s in segments:
        url = s.get("url", "")
        text_preview = s["text"][:80]
        line = f"  [{s['index']+1}/{record.get('thread_segment_count', len(segments))}] {text_preview}..."
        if url:
            line += f"\n    🔗 {url}"
        seg_lines.append(line)
    lines = [
        "🧵 主动发帖 (帖串)",
        "",
        f"🕒 时间: {record['time_beijing']}",
        f"⚙️ 触发: {record['trigger']}",
        f"🧪 模式: {'dry-run' if record['dry_run'] else 'send'}",
        f"📌 状态: {record.get('status', '')}",
        f"🏷️ 类型: thread",
        f"🧩 选题来源: {record.get('topic_source', '')}",
        f"📝 选题: {record.get('topic_text', '')}",
        f"🧵 段数: {record.get('thread_segment_count', '?')}",
        f"🎯 切入角度: {record.get('thread_angle', '')}",
        f"💰 Cost: {record['total_cost_cny']:.6f} 元",
    ]
    if record.get("image_query"):
        lines.append(f"🖼️ 配图: {record['image_query']}")
    lines.extend(["", "📄 帖串内容:"])
    lines.extend(seg_lines)
    lines.extend(["", f"🧠 理由: {record.get('thread_reason', '')}"])
    if record.get("post_url"):
        lines.extend(["", f"🔗 首帖链接: {record['post_url']}"])
    text = "\n".join(lines)
    if len(text) > 3800:
        return text[:3750] + "\n\n[通知过长，已截断]"
    return text


def _build_thread_record(topic: dict, plan: dict, args, started: datetime) -> dict:
    return {
        "time_beijing": started.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date_beijing": started.strftime("%Y-%m-%d"),
        "trigger": args.trigger,
        "dry_run": args.dry_run,
        "thread_mode": True,
        "status": "planned",
        "topic_id": topic.get("id", ""),
        "topic_type": "thread",
        "topic_text": topic.get("text", ""),
        "topic_source": topic.get("source", ""),
        "thread_segments": plan.get("segments", []),
        "thread_segment_count": len(plan.get("segments", [])),
        "thread_angle": plan.get("thread_angle", ""),
        "thread_reason": plan.get("thread_reason", ""),
        "image_query": plan.get("image_query", ""),
        "review_pass": bool(plan.get("review_pass")),
        "review_reason": plan.get("review_reason", ""),
        "review_rewrite_hint": plan.get("review_rewrite_hint", ""),
        "rewritten": bool(plan.get("rewritten")),
        "generate_usage": plan.get("generate_usage", {}),
        "generate_cost": plan.get("generate_cost", {}),
        "review_usage": plan.get("review_usage", {}),
        "review_cost": plan.get("review_cost", {}),
        "rewrite_usage": plan.get("rewrite_usage", {}),
        "rewrite_cost": plan.get("rewrite_cost", {}),
        "total_cost_cny": float(plan.get("total_cost_cny") or 0.0),
        "post_url": "",
    }


def _handle_thread(topic: dict, plan: dict, args, started: datetime, stamp: str, lock_fh) -> int:
    segments = plan.get("segments", [])
    if not segments:
        record = _build_thread_record(topic, plan, args, started)
        record["status"] = "thread_no_segments"
        write_json(LATEST_POST_RUN_PATH, record)
        write_json(post_history_path_for(stamp), record)
        notify_telegram(record, stamp, _thread_notify_text(record))
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 1

    if args.dry_run:
        record = _build_thread_record(topic, plan, args, started)
        record["status"] = "dry_run_ready"
        record["thread_segments_preview"] = [s["text"][:80] for s in segments]
        write_json(LATEST_POST_RUN_PATH, record)
        write_json(post_history_path_for(stamp), record)
        notify_telegram(record, stamp, _thread_notify_text(record))
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0

    # --- Send each segment ---
    segment_urls = []
    segment_results = []
    last_url = ""
    all_ok = True
    any_url_unresolved = False

    for i, seg in enumerate(segments):
        seg_text = seg["text"]
        sent_ok = False
        url = ""

        for attempt in range(3):
            if i == 0:
                send = run([
                    sys.executable, str(ROOT / "src/post/post_send.py"),
                    "--text", seg_text,
                ])
                send_payload = {}
                if send.returncode == 0 and send.stdout.strip():
                    try:
                        send_payload = parse_json_object(send.stdout)
                    except Exception:
                        pass
                # post_send.py's JSON contract: ``ok`` reflects the DOM
                # marker (``你的帖子已发送``). ``url`` is best-effort from a
                # profile-timeline lookup and can be empty even when send
                # succeeded. Old code keyed retries off URL → double-posts.
                sent_ok = bool(send_payload.get("ok") or send_payload.get("sent_ok"))
                url = str(send_payload.get("url") or "").strip()
            else:
                send = run([
                    sys.executable, str(ROOT / "src/reply/send_reply.py"),
                    "--url", last_url,
                    "--reply", seg_text,
                    "--action", "reply",
                    "--max-len", "280",
                    "--return-reply-url",
                ])
                # send_reply returns 0 only when its inner ok=true marker
                # fired, so returncode is a reliable send-success flag here.
                sent_ok = send.returncode == 0
                if sent_ok:
                    for line in (send.stdout or "").splitlines():
                        if line.startswith("REPLY_URL: "):
                            url = line[len("REPLY_URL: "):].strip()

            if sent_ok:
                break
            logger.warning("thread segment %d attempt %d failed (returncode=%s)", i, attempt + 1, send.returncode)
            time.sleep(3)

        if not sent_ok:
            all_ok = False
            logger.error("thread segment %d failed after 3 attempts, aborting remaining", i)
            break

        # Send confirmed. URL may still be empty if the profile-timeline
        # lookup missed — log + record but DO NOT retry (post already
        # landed; another attempt would duplicate). For segments 2+ we
        # also need ``last_url`` to chain replies — if it's missing on a
        # mid-thread segment, we can't continue.
        if not url:
            any_url_unresolved = True
            logger.warning(
                "thread segment %d sent but URL unresolved (post already landed; not retrying)",
                i,
            )
            if i < len(segments) - 1:
                logger.error(
                    "thread segment %d URL missing — cannot chain remaining segments, aborting",
                    i,
                )
                segment_results.append({"index": i, "url": "", "text": seg_text, "url_unresolved": True})
                all_ok = False
                break

        last_url = url
        segment_urls.append(url)
        segment_results.append({"index": i, "url": url, "text": seg_text, "url_unresolved": not bool(url)})

    record = _build_thread_record(topic, plan, args, started)
    record["thread_segments"] = segment_results
    record["thread_segment_urls"] = segment_urls
    if all_ok:
        record["status"] = "thread_posted_url_unresolved" if any_url_unresolved else "thread_posted"
    else:
        record["status"] = "thread_partial"
    record["post_url"] = segment_urls[0] if segment_urls else ""

    if all_ok:
        # Mark topic used even when URL is unresolved — the post landed,
        # so we must NOT leave the topic pending or a follow-up run will
        # re-post the same content.
        if topic.get("source") != "auto":
            mark_post_topic_status(
                str(topic.get("id") or ""),
                "used",
                topic_extra_update(record["status"], record["time_beijing"], dry_run=False),
            )
        add_recent_post(segments[0]["text"], "thread")

    write_json(LATEST_POST_RUN_PATH, record)
    write_json(post_history_path_for(stamp), record)
    notify_telegram(record, stamp, _thread_notify_text(record))
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if all_ok else 1
