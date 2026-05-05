#!/usr/bin/env python3
"""Shared context builders for LLM prompts (learning references + persona).

Used by both generate_reply.py and post_generate.py to avoid duplicating
the learning-context and persona-context assembly logic.
"""
from __future__ import annotations

from src.learning_store import recent_learning_references
from src.persona_store import get_generation_context


def build_learning_context(limit: int = 4) -> str:
    """Build a text block summarising recent high-engagement post patterns."""
    try:
        refs = recent_learning_references(limit=limit)
        if not refs:
            return ""
        lines = ["【近期高互动帖规律】"]
        for r in refs:
            hook = (r.get("hook_type") or "").strip()
            why = (r.get("why_it_works") or "").strip()
            takeaway = (r.get("imitation_takeaway") or "").strip()
            if hook and why:
                entry = f"- {hook} → {why}"
                if takeaway:
                    entry += f"（可借鉴：{takeaway}）"
                lines.append(entry)
        if len(lines) == 1:
            return ""
        return "\n".join(lines)[:500]
    except Exception:
        return ""


def build_persona_context(include_events: bool = False) -> str:
    """Build a text block describing the account persona.

    Args:
        include_events: If True, include recent_events from the persona store
            (used by post_generate; reply generation omits events).
    """
    try:
        ctx = get_generation_context()
        static = ctx.get("static") or {}
        if not static:
            return ""
        parts = []
        for k, v in list(static.items())[:6]:
            if isinstance(v, str) and v.strip():
                parts.append(f"{k}: {v.strip()}")
            elif isinstance(v, list) and v:
                items = [str(x).strip() for x in v[:3] if str(x).strip()]
                if items:
                    parts.append(f"{k}: " + " / ".join(x[:50] for x in items))
        recent_posts = ctx.get("recent_posts") or []
        if recent_posts:
            samples = [p["text"][:60] for p in recent_posts[-3:] if p.get("text")]
            if samples:
                parts.append("近期发帖: " + " / ".join(samples))
        if include_events:
            events = ctx.get("recent_events") or []
            if events:
                event_lines = [str(e.get("text") or e.get("event") or "")[:80] for e in events[-3:]]
                event_lines = [e for e in event_lines if e.strip()]
                if event_lines:
                    parts.append("近期事件: " + " / ".join(event_lines))
        if not parts:
            return ""
        return ("【账号人设】\n" + "\n".join(parts))[:600]
    except Exception:
        return ""


def persona_context_dict() -> dict:
    """Return the raw persona context dict (used by post_generate for JSON payloads)."""
    try:
        return get_generation_context()
    except Exception:
        return {}
