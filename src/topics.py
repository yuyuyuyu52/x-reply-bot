#!/usr/bin/env python3
"""Post topic queue management.

Extracted from common.py to isolate topic queue concerns.
"""
from __future__ import annotations

from src.common import (
    POST_TOPICS_LOCK_PATH,
    POST_TOPICS_PATH,
    VALID_POST_TOPIC_TYPES,
    blocking_lock,
    load_json,
    write_json,
)


def topic_summary_text(topic: dict) -> str:
    text = str(topic.get("text") or "").strip()
    if text:
        return text
    stance = str(topic.get("stance") or "").strip()
    if stance:
        return stance
    subject = str(topic.get("subject") or "").strip()
    context = str(topic.get("event_or_context") or "").strip()
    return " / ".join(part for part in [subject, context] if part)


def normalize_post_topic(item: dict) -> dict:
    normalized = dict(item or {})
    topic_type = str(normalized.get("type") or "").strip().lower()
    if topic_type not in VALID_POST_TOPIC_TYPES:
        topic_type = "argument"
    normalized["type"] = topic_type

    for key in ["id", "text", "source", "status", "subject", "event_or_context", "stance", "evidence_hint"]:
        normalized[key] = str(normalized.get(key) or "").strip()

    if not normalized["status"]:
        normalized["status"] = "pending"
    if not normalized["source"]:
        normalized["source"] = "manual"

    if not normalized["stance"] and normalized["text"]:
        normalized["stance"] = normalized["text"]
    if not normalized["text"]:
        normalized["text"] = topic_summary_text(normalized)

    return normalized


def load_post_topics() -> dict:
    data = load_json(POST_TOPICS_PATH, {"topics": []})
    if not isinstance(data, dict):
        return {"topics": []}
    topics = data.get("topics")
    if not isinstance(topics, list):
        data["topics"] = []
    else:
        data["topics"] = [normalize_post_topic(item) for item in topics if isinstance(item, dict)]
    return data


def save_post_topics(data: dict) -> None:
    write_json(POST_TOPICS_PATH, data)


def next_pending_post_topic() -> dict | None:
    data = load_post_topics()
    for item in data.get("topics", []):
        if (item.get("status") or "pending") == "pending":
            return item
    return None


def mark_post_topic_status(topic_id: str, status: str, extra: dict | None = None) -> dict:
    # Use blocking_lock to serialize concurrent writers (e.g. daemon job
    # marking a topic 'used' while a Telegram-triggered post_once or the
    # post_topics.py CLI is appending). Plain blocking flock waits briefly
    # — the critical section is microseconds, so contention never stalls.
    with blocking_lock(POST_TOPICS_LOCK_PATH):
        data = load_post_topics()
        updated = None
        for item in data.get("topics", []):
            if str(item.get("id") or "") != topic_id:
                continue
            item["status"] = status
            if extra:
                item.update(extra)
            updated = item
            break
        save_post_topics(data)
    return updated or {}


def post_topic_summary() -> dict:
    data = load_post_topics()
    topics = data.get("topics", [])
    summary = {"pending": 0, "used": 0, "skipped": 0, "total": len(topics)}
    for item in topics:
        status = str(item.get("status") or "pending")
        if status in summary:
            summary[status] += 1
    return summary
