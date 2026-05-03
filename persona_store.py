#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, date

from common import PERSONA_PATH


def load_persona() -> dict:
    try:
        data = json.loads(PERSONA_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"static": {}, "events": [], "recent_posts": []}
    return {
        "static": data.get("static") or {},
        "events": [e for e in (data.get("events") or []) if isinstance(e, dict)],
        "recent_posts": [p for p in (data.get("recent_posts") or []) if isinstance(p, dict)],
    }


def save_persona(data: dict) -> None:
    PERSONA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PERSONA_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PERSONA_PATH)


def add_event(raw: str, source: str = "telegram") -> dict:
    persona = load_persona()
    now = datetime.now().astimezone()
    event: dict = {
        "id": f"evt-{now.strftime('%Y%m%d-%H%M%S')}",
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date": now.strftime("%Y-%m-%d"),
        "raw": raw.strip(),
        "source": source,
    }
    events = persona["events"]
    events.append(event)
    persona["events"] = events[-50:]
    save_persona(persona)
    return event


def add_recent_post(text: str, topic_type: str) -> None:
    persona = load_persona()
    now = datetime.now().astimezone()
    post: dict = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date": now.strftime("%Y-%m-%d"),
        "text": text.strip(),
        "topic_type": topic_type,
    }
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
            "relative_date": _relative_date(e.get("date", "")),
        }
        for e in persona["events"][-10:]
    ]
    recent_posts = [
        {
            "date": p.get("date", ""),
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
