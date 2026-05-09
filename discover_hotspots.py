#!/usr/bin/env python3
"""Hotspot discovery entrypoint – fetch trends, filter, add to topic queue."""
from __future__ import annotations

import argparse
import fcntl
import json
import sys
from datetime import datetime
from pathlib import Path

from src.common import (
    HOTSPOT_HISTORY_DIR,
    LATEST_HOTSPOT_RUN_PATH,
    POST_TOPICS_PATH,
    ensure_state_dirs,
    load_env_file,
    load_json,
    normalize_post_topic,
    save_post_topics,
    telegram_enabled,
    telegram_notify,
    write_json,
)
from src.hotspot.discover import discover_hotspots
from src.hotspot.store import mark_added_to_queue
from src.logger import get_logger

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent
HOTSPOT_LOCK_PATH = ROOT / "state" / "hotspot_discover.lock"


def _persist(record: dict, stamp: str) -> None:
    write_json(LATEST_HOTSPOT_RUN_PATH, record)
    HOTSPOT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    write_json(HOTSPOT_HISTORY_DIR / f"{stamp}.json", record)


def _notify(record: dict) -> None:
    if not telegram_enabled():
        return
    items = record.get("added_items", [])
    lines = [
        "🔥 热点发现",
        "",
        f"🕒 时间: {record['time_beijing']}",
        f"⚙️ 触发: {record['trigger']}",
        f"📊 发现: {record['discovered']} 条新热点",
        f"✅ 入库: {record['added']} 条",
        f"⏭️ 已见: {record['skipped_seen']} 条",
        f"❌ 过滤: {record['filtered_out']} 条",
        f"💰 Cost: {record['total_cost_cny']:.6f} 元",
    ]
    if items:
        lines.append("")
        lines.append("📌 新入队热点:")
        for item in items[:5]:
            stars = "⭐" * max(1, item.get("relevance_score", 3))
            lines.append(f"• {stars} [{item.get('source', '')}] {item.get('cn_summary', '')}")
            lines.append(f"  🎯 {item.get('angle', '')}")
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3750] + "\n\n[通知过长，已截断]"
    try:
        telegram_notify(text)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_env_file()
    ensure_state_dirs()

    lock_fh = HOTSPOT_LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning("hotspot_discover already running")
        return 3

    started = datetime.now().astimezone()
    stamp = started.strftime("%Y%m%d_%H%M%S")
    logger.info("hotspot_discover start trigger=%s dry_run=%s", args.trigger, args.dry_run)

    try:
        result = discover_hotspots()
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()

    if not result.get("ok"):
        record = {
            "time_beijing": started.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "date_beijing": started.strftime("%Y-%m-%d"),
            "trigger": args.trigger,
            "status": "error",
            "error": result.get("error", ""),
            "total_cost_cny": result.get("total_cost_cny", 0.0),
        }
        _persist(record, stamp)
        logger.error("hotspot_discover failed: %s", result.get("error", ""))
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 1

    # --- Add relevant hotspots to topic queue ---
    items = result.get("items", [])
    added_items: list[dict] = []
    if items and not args.dry_run:
        data = load_json(POST_TOPICS_PATH, {"topics": []})
        topics = data.get("topics", [])
        for item in items:
            topic = normalize_post_topic({
                "id": f"hotspot-{item['source']}-{item['id']}",
                "type": "news_react",
                "text": item["cn_summary"],
                "source": "hotspot",
                "status": "pending",
                "subject": item["title"],
                "event_or_context": f"今天[{item['source']}] {item['relevance_reason']} | 原链接: {item['url']}",
                "stance": item["angle"],
                "evidence_hint": f"热度: {item['hn_score']}↑ {item['hn_descendants']}💬 | 相关度: {item['relevance_score']}/5",
            })
            topics.append(topic)
            mark_added_to_queue(item["source"], item["id"])
            added_items.append({
                "source": item["source"],
                "title": item["title"],
                "cn_summary": item["cn_summary"],
                "angle": item["angle"],
                "relevance_score": item["relevance_score"],
            })
        save_post_topics(data)

    record = {
        "time_beijing": started.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date_beijing": started.strftime("%Y-%m-%d"),
        "trigger": args.trigger,
        "dry_run": args.dry_run,
        "status": "ok",
        "discovered": result.get("discovered", 0),
        "added": result.get("added", 0),
        "skipped_seen": result.get("skipped_seen", 0),
        "filtered_out": result.get("filtered_out", 0),
        "added_items": added_items,
        "total_cost_cny": result.get("total_cost_cny", 0.0),
    }
    _persist(record, stamp)
    _notify(record)
    logger.info("hotspot_discover done discovered=%d added=%d", record["discovered"], record["added"])
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
