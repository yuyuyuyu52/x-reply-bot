#!/usr/bin/env python3
"""Article-mode post handler.

Extracted from post_once.py. ``_handle_article`` orchestrates the
article_send.py subprocess and writes the post-history record. The
single-post and thread paths live in their own modules so this file
stays focused on long-form articles.
"""
from __future__ import annotations

import json
import sys
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


def _article_notify_text(record: dict) -> str:
    lines = [
        "📰 主动发帖 (文章)",
        "",
        f"🕒 时间: {record['time_beijing']}",
        f"⚙️ 触发: {record['trigger']}",
        f"🧪 模式: {'dry-run' if record['dry_run'] else 'send'}",
        f"📌 状态: {record.get('status', '')}",
        f"🏷️ 类型: article",
        f"🧩 选题来源: {record.get('topic_source', '')}",
        f"📝 选题: {record.get('topic_text', '')}",
        f"📰 标题: {record.get('article_title', '')}",
        f"💰 Cost: {record['total_cost_cny']:.6f} 元",
    ]
    if record.get("image_query"):
        lines.append(f"🖼️ 配图: {record['image_query']}")
    lines.extend([
        "",
        "📄 正文预览:",
        record.get("article_body", "")[:200] + ("..." if len(record.get("article_body", "")) > 200 else ""),
        "",
        f"🧠 理由: {record.get('article_reason', '')}",
    ])
    if record.get("post_url"):
        lines.extend(["", f"🔗 文章链接: {record['post_url']}"])
    text = "\n".join(lines)
    if len(text) > 3800:
        return text[:3750] + "\n\n[通知过长，已截断]"
    return text


def _build_article_record(topic: dict, plan: dict, args, started: datetime) -> dict:
    return {
        "time_beijing": started.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date_beijing": started.strftime("%Y-%m-%d"),
        "trigger": args.trigger,
        "dry_run": args.dry_run,
        "article_mode": True,
        "status": "planned",
        "topic_id": topic.get("id", ""),
        "topic_type": "article",
        "topic_text": topic.get("text", ""),
        "topic_source": topic.get("source", ""),
        "article_title": plan.get("title", ""),
        "article_body": plan.get("body", ""),
        "article_reason": plan.get("article_reason", ""),
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


def _handle_article(topic: dict, plan: dict, args, started: datetime, stamp: str, lock_fh) -> int:
    title = plan.get("title", "")
    body = plan.get("body", "")
    image_query = plan.get("image_query", "")

    if not title or not body:
        record = _build_article_record(topic, plan, args, started)
        record["status"] = "article_no_content"
        write_json(LATEST_POST_RUN_PATH, record)
        write_json(post_history_path_for(stamp), record)
        notify_telegram(record, stamp, _article_notify_text(record))
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 1

    if args.dry_run:
        record = _build_article_record(topic, plan, args, started)
        record["status"] = "dry_run_ready"
        record["article_body_preview"] = body[:200]
        write_json(LATEST_POST_RUN_PATH, record)
        write_json(post_history_path_for(stamp), record)
        notify_telegram(record, stamp, _article_notify_text(record))
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0

    # Send the article
    send_cmd = [
        sys.executable, str(ROOT / "src/post/article_send.py"),
        "--title", title,
        "--body", body,
    ]
    if image_query:
        send_cmd.extend(["--image-query", image_query])

    send = run(send_cmd)
    sys.stdout.write(send.stdout)
    sys.stderr.write(send.stderr)

    # Parse output. article_send.py emits a JSON object with ``ok`` and
    # ``sent_ok`` (set together; ``ok`` is the publish-success flag based
    # on whether the page URL redirected to /status/). ``url`` is that
    # final URL. Old code did ``ok = bool(url)`` which is fine for
    # articles in steady state but conflates "send confirmed" with
    # "URL parsed" — for robustness use the explicit ok flag and
    # distinguish URL-unresolved from a genuine send failure.
    send_payload = {}
    if send.stdout.strip():
        try:
            send_payload = parse_json_object(send.stdout)
        except Exception as exc:
            logger.warning("article_send parse error: %s", exc)
    sent_ok = bool(send_payload.get("ok") or send_payload.get("sent_ok"))
    article_url = str(send_payload.get("url") or "").strip()

    # Parse image cost
    image_cost_cny = 0.0
    image_info = {}
    for line in (send.stdout or "").splitlines():
        if line.startswith("IMAGE_INFO: "):
            try:
                image_info = json.loads(line[len("IMAGE_INFO: "):])
                image_cost_cny = float(image_info.get("cost_cny") or 0)
            except Exception:
                pass

    record = _build_article_record(topic, plan, args, started)
    record["image_info"] = image_info
    record["post_url"] = article_url
    if sent_ok:
        record["status"] = "article_posted" if article_url else "article_sent_url_unresolved"
        if not article_url:
            logger.warning("article send confirmed but URL unresolved; not retrying")
    else:
        record["status"] = "article_send_failed"
    record["total_cost_cny"] = round(float(record["total_cost_cny"]) + image_cost_cny, 8)
    record["send_returncode"] = send.returncode

    # Mark topic used whenever the send was confirmed, even if URL is
    # unresolved — the post already landed; leaving the topic ``pending``
    # would cause a follow-up run to re-post the same article.
    if sent_ok:
        if topic.get("source") != "auto":
            mark_post_topic_status(
                str(topic.get("id") or ""),
                "used",
                topic_extra_update(record["status"], record["time_beijing"], dry_run=False),
            )
        add_recent_post(f"[文章] {title}", "article")

    write_json(LATEST_POST_RUN_PATH, record)
    write_json(post_history_path_for(stamp), record)
    notify_telegram(record, stamp, _article_notify_text(record))
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if sent_ok else 1
