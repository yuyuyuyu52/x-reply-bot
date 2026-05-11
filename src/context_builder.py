#!/usr/bin/env python3
"""Shared context builders for LLM prompts (learning references + persona).

Used by both generate_reply.py and post_generate.py to avoid duplicating
the learning-context and persona-context assembly logic.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.common import HISTORY_DIR, POST_HISTORY_DIR
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


def _beijing_now() -> datetime:
    return datetime.now().astimezone()


def _find_feedback_file(stamp: str) -> Path | None:
    """Find a history or post_history JSON file by stamp (filename without .json)."""
    safe = stamp.replace("/", "_").replace("\\", "_")
    for d in (HISTORY_DIR, POST_HISTORY_DIR):
        p = d / f"{safe}.json"
        if p.exists():
            return p
    return None


def scan_reviewable_entries(days: int = 3) -> list[dict]:
    """Scan recent history/post_history for entries without human_feedback.

    Returns a list of dicts: {stamp, kind, text_preview, time_beijing}
    kind is 'reply' for HISTORY_DIR, 'post' for POST_HISTORY_DIR.
    """
    cutoff = _beijing_now() - timedelta(days=days)
    entries: list[dict] = []

    for d, kind in [(HISTORY_DIR, "reply"), (POST_HISTORY_DIR, "post")]:
        if not d.exists():
            continue
        for f in sorted(d.iterdir(), reverse=True):
            if not f.suffix == ".json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError):
                continue
            if "human_feedback" in data:
                continue
            time_str = str(data.get("time_beijing") or data.get("time") or "")
            try:
                normalized = time_str.strip().replace("CST", "").strip()
                normalized = normalized.replace(" ", "T", 1)
                if "T" in normalized:
                    head, sep, tail = normalized.partition("T")
                    tail = tail.lstrip().replace(" ", "")
                    if len(tail) >= 5 and tail[-5] in "+-" and ":" not in tail[-5:]:
                        tail = tail[:-2] + ":" + tail[-2:]
                    normalized = head + sep + tail
                t = datetime.fromisoformat(normalized)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone(timedelta(hours=8)))
            except (ValueError, TypeError):
                continue
            if t < cutoff:
                # Files are iterated newest-first; once we've crossed the cutoff,
                # every subsequent file is older still — stop scanning this dir.
                break
            stamp = f.stem
            if kind == "reply":
                text = str(data.get("reply_text") or data.get("post_text") or "")[:80]
            else:
                text = str(data.get("post_text") or data.get("best_effort_post_text") or "")[:80]
            entries.append({
                "stamp": stamp,
                "kind": kind,
                "text_preview": text,
                "time_beijing": time_str,
            })

    entries.sort(key=lambda e: e["time_beijing"], reverse=True)
    return entries


def write_feedback(stamp: str, score: int, comment: str = "") -> dict | None:
    """Write human_feedback to the matching history file. Returns the updated record or None."""
    p = _find_feedback_file(stamp)
    if not p:
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return None
    now = _beijing_now()
    data["human_feedback"] = {
        "score": score,
        "comment": comment.strip(),
        "rated_at": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def build_feedback_context(limit: int = 4) -> str:
    """Build a text block with recent human feedback examples for prompt injection."""
    # Cap good/bad samples per the original [:2] slicing logic; we only need
    # a handful of recent examples to seed the prompt. Once both buckets are
    # filled (5 each, generously), stop scanning older files.
    target_per_bucket = 5
    good: list[dict] = []
    bad: list[dict] = []
    for d in (HISTORY_DIR, POST_HISTORY_DIR):
        if not d.exists():
            continue
        for f in sorted(d.iterdir(), reverse=True):
            if len(good) >= target_per_bucket and len(bad) >= target_per_bucket:
                break
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError):
                continue
            fb = data.get("human_feedback")
            if not fb or not isinstance(fb, dict):
                continue
            score = fb.get("score")
            if not isinstance(score, (int, float)):
                continue
            text = str(data.get("reply_text") or data.get("post_text") or data.get("best_effort_post_text") or "")[:120]
            comment = str(fb.get("comment") or "").strip()
            entry = {"score": int(score), "text": text, "comment": comment}
            if entry["score"] >= 4 and len(good) < target_per_bucket:
                good.append(entry)
            elif entry["score"] <= 2 and len(bad) < target_per_bucket:
                bad.append(entry)

    if not good and not bad:
        return ""

    good = good[:2]
    bad = bad[:2]

    lines: list[str] = []
    if good:
        lines.append("【好评示例（这些回复/帖子收到了高分，可参考其风格）】")
        for s in good:
            note = f" — {s['comment']}" if s["comment"] else ""
            lines.append(f"- [{s['score']}分] {s['text']}{note}")
    if bad:
        lines.append("【差评示例（避免类似风格或内容）】")
        for s in bad:
            note = f" — {s['comment']}" if s["comment"] else ""
            lines.append(f"- [{s['score']}分] {s['text']}{note}")

    if not lines:
        return ""

    gen_hint = ""
    if good:
        gen_hint += "尽量模仿好评示例的语气、结构和具体程度。"
    if bad:
        gen_hint += "避免差评示例中的问题（太空泛、太AI、太像总结）。"
    if gen_hint:
        lines.append(gen_hint)

    return "\n".join(lines)[:800]
