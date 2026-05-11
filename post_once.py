#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import fcntl
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from src.common import (
    exclusive_lock,
    LATEST_POST_RUN_PATH,
    POST_LOCK_PATH,
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
from src.logger import get_logger
from src.post.post_generate import generate_post_plan, generate_thread_plan, generate_article_plan
from src.post.topic_auto import generate_auto_topic
from src.persona_store import add_recent_post

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(cmd, text=True, capture_output=True, cwd=str(ROOT), env=env)


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


def _handle_article(topic: dict, plan: dict, args, started: datetime, stamp: str, lock_fh) -> int:
    title = plan.get("title", "")
    body = plan.get("body", "")
    image_query = plan.get("image_query", "")

    if not title or not body:
        record = _build_article_record(topic, plan, args, started)
        record["status"] = "article_no_content"
        write_json(LATEST_POST_RUN_PATH, record)
        write_json(post_history_path_for(stamp), record)
        notify_telegram(record, stamp)
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 1

    if args.dry_run:
        record = _build_article_record(topic, plan, args, started)
        record["status"] = "dry_run_ready"
        record["article_body_preview"] = body[:200]
        write_json(LATEST_POST_RUN_PATH, record)
        write_json(post_history_path_for(stamp), record)
        notify_telegram(record, stamp)
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
    notify_telegram(record, stamp)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if sent_ok else 1


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
        notify_telegram(record, stamp)
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 1

    if args.dry_run:
        record = _build_thread_record(topic, plan, args, started)
        record["status"] = "dry_run_ready"
        record["thread_segments_preview"] = [s["text"][:80] for s in segments]
        write_json(LATEST_POST_RUN_PATH, record)
        write_json(post_history_path_for(stamp), record)
        notify_telegram(record, stamp)
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
    notify_telegram(record, stamp)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0 if all_ok else 1


def notify_text(record: dict) -> str:
    if record.get("article_mode"):
        return _article_notify_text(record)
    if record.get("thread_mode"):
        return _thread_notify_text(record)
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


def notify_telegram(record: dict, stamp: str) -> None:
    if not telegram_enabled():
        return
    try:
        tg_resp = telegram_notify(notify_text(record))
        record["telegram_notify"] = {"ok": True, "response": tg_resp}
    except Exception as exc:
        record["telegram_notify"] = {"ok": False, "error": str(exc)}
        print(f"TELEGRAM_NOTIFY_ERROR: {exc}")
    write_json(LATEST_POST_RUN_PATH, record)
    write_json(post_history_path_for(stamp), record)


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
    logger.info("post_once start trigger=%s dry_run=%s stamp=%s", args.trigger, args.dry_run, stamp)

    try:
        topic = next_pending_post_topic()
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
            record["status"] = "dry_run_rejected" if args.dry_run else "skipped_no_candidate"
            if not args.dry_run and topic.get("source") != "auto":
                mark_post_topic_status(
                    str(topic.get("id") or ""),
                    "skipped",
                    topic_extra_update(record["status"], record["time_beijing"], dry_run=False),
                )
            write_json(LATEST_POST_RUN_PATH, record)
            write_json(post_history_path_for(stamp), record)
            notify_telegram(record, stamp)
            print(json.dumps(record, ensure_ascii=False, indent=2))
            return 0 if args.dry_run else 1

        if args.dry_run:
            record["status"] = "dry_run_ready"
            write_json(LATEST_POST_RUN_PATH, record)
            write_json(post_history_path_for(stamp), record)
            notify_telegram(record, stamp)
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
            if topic.get("source") != "auto":
                mark_post_topic_status(
                    str(topic.get("id") or ""),
                    "used",
                    topic_extra_update(record["status"], record["time_beijing"], dry_run=False),
                )
            add_recent_post(record["post_text"], str(topic.get("type", "")))

        write_json(LATEST_POST_RUN_PATH, record)
        write_json(post_history_path_for(stamp), record)
        # HIGH-5: TG 失败一定不能让本函数抛/返回非 0。post_send 已成功的情况下，
        # 上抛会让 bot_daemon 把整次当作"发送失败"再发一条失败通知，引导用户重发，
        # 但实际帖子已经发出去 → 重发风险。所以独立 try/except，结果写进 record。
        notify_telegram(record, stamp)
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
