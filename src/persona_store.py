#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
from datetime import datetime, date

from src.common import PERSONA_PATH, PERSONA_LOCK_PATH, exclusive_lock


def _migrate_record(rec: dict) -> dict:
    """One-time-on-read migration: copy legacy `timestamp`/`date` to
    canonical `time_beijing`/`date_beijing`. Idempotent."""
    if not isinstance(rec, dict):
        return rec
    if "time_beijing" not in rec and rec.get("timestamp"):
        rec["time_beijing"] = rec["timestamp"]
    if "date_beijing" not in rec and rec.get("date"):
        rec["date_beijing"] = rec["date"]
    return rec


def load_persona() -> dict:
    try:
        data = json.loads(PERSONA_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"static": {}, "events": [], "recent_posts": []}
    events = [_migrate_record(e) for e in (data.get("events") or []) if isinstance(e, dict)]
    recent_posts = [_migrate_record(p) for p in (data.get("recent_posts") or []) if isinstance(p, dict)]
    return {
        "static": data.get("static") or {},
        "events": events,
        "recent_posts": recent_posts,
    }


def save_persona(data: dict) -> None:
    PERSONA_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Per-pid stage file so two concurrent writers don't stomp the same .tmp.
    tmp = PERSONA_PATH.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PERSONA_PATH)


class _persona_lock:
    """Blocking wrapper around the non-blocking `exclusive_lock` helper.

    `common.exclusive_lock` uses `fcntl.LOCK_EX | LOCK_NB`, which raises
    `BlockingIOError` on contention. Persona updates must not be dropped, so
    we retry briefly to convert non-blocking semantics into blocking ones."""

    def __init__(self, retries: int = 50, sleep_seconds: float = 0.1):
        self._retries = retries
        self._sleep = sleep_seconds
        self._cm = None

    def __enter__(self):
        PERSONA_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        last_err: Exception | None = None
        for _ in range(self._retries):
            cm = exclusive_lock(PERSONA_LOCK_PATH)
            try:
                cm.__enter__()
            except BlockingIOError as e:
                last_err = e
                time.sleep(self._sleep)
                continue
            self._cm = cm
            return self
        raise last_err if last_err is not None else BlockingIOError(
            "could not acquire persona lock"
        )

    def __exit__(self, exc_type, exc, tb):
        if self._cm is not None:
            return self._cm.__exit__(exc_type, exc, tb)
        return False


def add_event(raw: str, source: str = "telegram") -> dict:
    now = datetime.now().astimezone()
    event: dict = {
        "id": f"evt-{now.strftime('%Y%m%d-%H%M%S')}",
        "time_beijing": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date_beijing": now.strftime("%Y-%m-%d"),
        "raw": raw.strip(),
        "source": source,
    }
    with _persona_lock():
        persona = load_persona()
        events = persona["events"]
        events.append(event)
        persona["events"] = events[-50:]
        save_persona(persona)
    return event


def add_recent_post(text: str, topic_type: str) -> None:
    now = datetime.now().astimezone()
    post: dict = {
        "time_beijing": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date_beijing": now.strftime("%Y-%m-%d"),
        "text": text.strip(),
        "topic_type": topic_type,
    }
    with _persona_lock():
        persona = load_persona()
        posts = persona["recent_posts"]
        posts.append(post)
        persona["recent_posts"] = posts[-15:]
        save_persona(persona)


def _relative_date(event_date_str: str) -> str:
    try:
        delta = (datetime.now().astimezone().date() - date.fromisoformat(event_date_str)).days
    except (ValueError, TypeError):
        return ""
    if delta == 0:
        return "今天"
    if delta == 1:
        return "昨天"
    if delta <= 7:
        return f"{delta}天前"
    weeks = delta // 7
    return f"约{weeks}周前"


def get_generation_context() -> dict:
    persona = load_persona()
    recent_events = [
        {
            "raw": e.get("raw", ""),
            "relative_date": _relative_date(e.get("date_beijing", "") or e.get("date", "")),
        }
        for e in persona["events"][-10:]
    ]
    recent_posts = [
        {
            "date": p.get("date_beijing", "") or p.get("date", ""),
            "topic_type": p.get("topic_type", ""),
            "text": str(p.get("text", ""))[:100],
        }
        for p in persona["recent_posts"][-8:]
    ]
    return {
        "static": persona["static"],
        "recent_events": recent_events,
        "recent_posts": recent_posts,
    }
