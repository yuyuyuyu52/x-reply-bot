#!/usr/bin/env python3
"""X post metrics parsing and engagement scoring.

Extracted from observe_feed.py so that revisit.py (and any future consumer)
can import these without pulling in the entire observe_feed module.
"""
from __future__ import annotations

import math
import re

from src.common import LATEST_POST_RUN_PATH, load_json


def normalize_count(raw: str) -> int:
    token = (raw or "").strip().replace(",", "").replace("，", "")
    if not token:
        return 0
    units = {
        "k": 1_000,
        "m": 1_000_000,
        "b": 1_000_000_000,
        "万": 10_000,
        "亿": 100_000_000,
    }
    lower = token.lower()
    for suffix, multiplier in units.items():
        if lower.endswith(suffix):
            try:
                return int(float(lower[:-len(suffix)]) * multiplier)
            except ValueError:
                return 0
    try:
        return int(float(lower))
    except ValueError:
        return 0


def first_number_token(text: str) -> int:
    match = re.search(r"([0-9][0-9,\.]*\s*[kmbKMB万亿]?)", text or "")
    return normalize_count(match.group(1)) if match else 0


def parse_metrics(aria_labels: list[str]) -> dict:
    metrics = {"views": 0, "replies": 0, "reposts": 0, "likes": 0, "bookmarks": 0}
    for label in aria_labels or []:
        lowered = label.lower()
        value = first_number_token(label)
        if any(key in lowered for key in ["回复", "reply"]):
            metrics["replies"] = max(metrics["replies"], value)
        elif any(key in lowered for key in ["转帖", "转推", "转发", "retweet", "repost"]):
            metrics["reposts"] = max(metrics["reposts"], value)
        elif any(key in lowered for key in ["喜欢", "like"]):
            metrics["likes"] = max(metrics["likes"], value)
        elif any(key in lowered for key in ["书签", "bookmark"]):
            metrics["bookmarks"] = max(metrics["bookmarks"], value)
        elif any(key in lowered for key in ["观看", "查看", "view"]):
            metrics["views"] = max(metrics["views"], value)
    return metrics


def engagement_score(metrics: dict) -> float:
    views = max(int(metrics.get("views") or 0), 0)
    replies = max(int(metrics.get("replies") or 0), 0)
    reposts = max(int(metrics.get("reposts") or 0), 0)
    likes = max(int(metrics.get("likes") or 0), 0)
    bookmarks = max(int(metrics.get("bookmarks") or 0), 0)
    return round(
        math.log1p(views) * 1.2
        + math.log1p(replies) * 4.5
        + math.log1p(reposts) * 4.0
        + math.log1p(likes) * 3.2
        + math.log1p(bookmarks) * 1.8,
        4,
    )


def infer_own_handle() -> str:
    latest_post = load_json(LATEST_POST_RUN_PATH, {})
    url = str(latest_post.get("post_url") or "")
    match = re.search(r"https://x\.com/([^/]+)/status/\d+", url)
    return match.group(1).lower() if match else ""
