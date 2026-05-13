#!/usr/bin/env python3
"""Unified service layer for selecting and marking post topics.

Priority chain in next_topic_to_post():
    1. topics.next_pending_post_topic()  # manual / Telegram queue
    2. hotspot.selector.pick_best()      # hotspot pool with LLM dedup
    3. None                              # caller falls back to auto

Also handles one-time idempotent migration of legacy `source=hotspot`
pending rows in post_topics.json — they get `status=skipped` so the
old queueing semantics don't leak into the new pipeline.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.common import POST_TOPICS_LOCK_PATH, blocking_lock
from src.hotspot import selector
from src.hotspot import store as hotspot_store
from src.logger import get_logger
from src import topics

logger = get_logger(__name__)

BEIJING_TZ = ZoneInfo("Asia/Shanghai")

_migration_done = False


def _now_beijing_str() -> str:
    return datetime.now(tz=BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _migrate_legacy_hotspot_topics_once() -> None:
    global _migration_done
    if _migration_done:
        return
    try:
        with blocking_lock(POST_TOPICS_LOCK_PATH):
            data = topics.load_post_topics()
            changed = 0
            for t in data.get("topics", []):
                if t.get("source") == "hotspot" and (t.get("status") or "pending") == "pending":
                    t["status"] = "skipped"
                    t["skip_reason"] = "migrated_to_db_pool"
                    t["migrated_at"] = _now_beijing_str()
                    changed += 1
            if changed:
                topics.save_post_topics(data)
                logger.info("postable_pool: migrated %d legacy hotspot topics", changed)
    except Exception as exc:
        logger.warning("postable_pool: legacy migration failed: %s", exc)
    _migration_done = True


def next_topic_to_post() -> dict | None:
    _migrate_legacy_hotspot_topics_once()

    manual = topics.next_pending_post_topic()
    if manual:
        manual["_pool"] = "manual"
        manual["_pool_ref"] = ""
        return manual

    return selector.pick_best()


def mark_topic_used(topic: dict, status: str = "used", extra: dict | None = None) -> None:
    pool = str(topic.get("_pool") or "")
    if pool == "manual":
        try:
            topics.mark_post_topic_status(str(topic.get("id") or ""), status, extra)
        except Exception as exc:
            logger.error("postable_pool: mark manual failed for %s: %s", topic.get("id"), exc)
        return

    if pool == "hotspot":
        # "used" = posted successfully. "skipped" = LLM rewrite-review rejected
        # content (permanent — don't re-pick). Other statuses (e.g. send_failed)
        # are transient → leave posted_at empty so the row stays in the pool.
        if status not in ("used", "skipped"):
            return
        ref = str(topic.get("_pool_ref") or "")
        if ":" not in ref:
            logger.warning("postable_pool: bad _pool_ref %r for hotspot topic %s", ref, topic.get("id"))
            return
        source, hotspot_id = ref.split(":", 1)
        try:
            hotspot_store.mark_posted(source, hotspot_id)
        except Exception as exc:
            logger.error("postable_pool: mark hotspot %s failed: %s", ref, exc)
        return

    logger.warning("postable_pool: mark_topic_used with missing _pool on %r", topic.get("id"))


def pool_status() -> dict:
    _migrate_legacy_hotspot_topics_once()
    manual = topics.post_topic_summary()
    try:
        unposted = hotspot_store.unposted_candidates_within(hours=24, min_score=3)
        stats = hotspot_store.hotspot_stats()
        posted = hotspot_store.posted_today_summaries()
        hotspot = {
            "pool_size_24h": len(unposted),
            "discovered_today": int(stats.get("today_discovered") or 0),
            "posted_today": len(posted),
        }
    except Exception as exc:
        logger.warning("postable_pool: hotspot status query failed: %s", exc)
        hotspot = {"pool_size_24h": 0, "discovered_today": 0, "posted_today": 0}
    return {"manual": manual, "hotspot": hotspot}
