#!/usr/bin/env python3
"""Hotspot discovery entrypoint – fetch trends, filter, add to topic queue."""
from __future__ import annotations

import argparse
import fcntl
import json
from datetime import datetime
from pathlib import Path

from src.common import (
    HOTSPOT_HISTORY_DIR,
    HOTSPOT_LOCK_PATH,
    LATEST_HOTSPOT_RUN_PATH,
    ensure_state_dirs,
    load_env_file,
    telegram_enabled,
    telegram_notify,
    write_json,
)
from src.hotspot.discover import discover_hotspots
from src.logger import get_logger

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent


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
        f"📊 评估候选: {record['discovered']} 条",
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
    elif record.get("filtered_items"):
        lines.append("")
        lines.append("🧪 被过滤样例:")
        for item in record.get("filtered_items", [])[:5]:
            title = str(item.get("title") or "")[:80]
            score = item.get("relevance_score", 0)
            reason = item.get("relevance_reason", "")
            lines.append(f"• [{item.get('source', '')}] {title} ({score}/5)")
            if reason:
                lines.append(f"  {reason}")
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

        items = result.get("items", [])
        added_items: list[dict] = []
        if items and not args.dry_run:
            for item in items:
                added_items.append({
                    "source": item["source"],
                    "title": item["title"],
                    "cn_summary": item["cn_summary"],
                    "angle": item["angle"],
                    "relevance_score": item["relevance_score"],
                })

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
            "source_stats": result.get("source_stats", {}),
            "source_durations": result.get("source_durations", {}),
            "added_items": added_items,
            "filtered_items": result.get("filtered_items", []),
            "total_cost_cny": result.get("total_cost_cny", 0.0),
        }
        _persist(record, stamp)
        _notify(record)
        logger.info("hotspot_discover done discovered=%d added=%d", record["discovered"], record["added"])
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()


if __name__ == "__main__":
    raise SystemExit(main())
