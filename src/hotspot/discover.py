#!/usr/bin/env python3
"""Hotspot discovery: fetch from external sources, LLM filter, score."""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone

from src.common import chat_json_result
from src.hotspot.store import is_seen, insert_hotspot

HN_TOP_STORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{}.json"
HN_MAX_FETCH = 30

HOTSPOT_FILTER_PROMPT = """\
你在筛选与指定关注方向相关的热点新闻，用于 X 账号发帖。

输出严格 JSON：
{"relevant": true, "score": 3, "reason": "...", "angle": "...", "cn_summary": "..."}

关注方向：AI 与 LLM、web3/加密货币、金融科技、半导体、光模块/硬件、创业/startup、产品/增长、开发者工具、自媒体创作。

规则：
- relevant: 是否与上述方向相关
- score: 1-5 热度与讨论价值评分
  - 5=高热度且有独特切入角度，非常值得发帖
  - 4=相关且有观点空间
  - 3=相关但偏资讯，发帖角度有限
  - 2=弱相关
  - 1=不相关
- reason: 简短说明为什么相关或不相关，30字内
- angle: 推荐的发帖切入角度，20字内（不相关时为空）
- cn_summary: 中文摘要，60字内
- 必须用中文输出 reason、angle、cn_summary
- 只输出 JSON，不要 markdown 包裹
"""


def fetch_hn_top_stories(limit: int = HN_MAX_FETCH) -> list[dict]:
    """Fetch top stories from Hacker News API."""
    req = urllib.request.Request(
        HN_TOP_STORIES_URL,
        headers={"User-Agent": "x-reply-bot/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        ids = json.loads(resp.read().decode())

    stories: list[dict] = []
    for sid in ids[:limit]:
        try:
            item_req = urllib.request.Request(
                HN_ITEM_URL.format(sid),
                headers={"User-Agent": "x-reply-bot/1.0"},
            )
            with urllib.request.urlopen(item_req, timeout=10) as resp:
                item = json.loads(resp.read().decode())
        except Exception:
            continue
        if not item or not item.get("title"):
            continue
        stories.append({
            "id": str(item.get("id", "")),
            "title": item.get("title", ""),
            "url": item.get("url") or f"https://news.ycombinator.com/item?id={item['id']}",
            "score": item.get("score", 0),
            "descendants": item.get("descendants", 0),
        })
    return stories


def filter_hotspot(story: dict) -> dict:
    """Use LLM to filter and score a single story."""
    response = chat_json_result(
        [
            {"role": "system", "content": HOTSPOT_FILTER_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "title": story["title"],
                        "hn_score": story["score"],
                        "hn_comments": story["descendants"],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        temperature=0.3,
        max_tokens=300,
    )
    payload = response["payload"]
    return {
        "relevant": bool(payload.get("relevant")),
        "score": int(payload.get("score") or 0),
        "reason": str(payload.get("reason") or "").strip(),
        "angle": str(payload.get("angle") or "").strip(),
        "cn_summary": str(payload.get("cn_summary") or "").strip(),
        "cost": response.get("cost", {}),
        "usage": response.get("usage", {}),
    }


def discover_hotspots(sources: list[str] | None = None) -> dict:
    """Run a discovery cycle. Returns a dict with stats + discovered items.

    sources: list of source names, e.g. ["hn"]. Defaults to ["hn"].
    """
    if sources is None:
        sources = ["hn"]

    started = datetime.now().astimezone()
    all_stories: list[dict] = []
    total_cost = 0.0

    # --- Fetch ---
    for source in sources:
        if source == "hn":
            try:
                stories = fetch_hn_top_stories()
                all_stories.extend({"source": "hn", **s} for s in stories)
            except Exception as exc:
                return {
                    "ok": False,
                    "error": f"hn_fetch_failed: {exc}",
                    "discovered": 0,
                    "added": 0,
                    "skipped_seen": 0,
                    "filtered_out": 0,
                    "items": [],
                    "total_cost_cny": 0.0,
                }

    # --- Filter: dedup + LLM score ---
    discovered = 0
    added = 0
    skipped_seen = 0
    filtered_out = 0
    items: list[dict] = []

    for story in all_stories:
        source = story["source"]
        sid = story["id"]
        if is_seen(source, sid):
            skipped_seen += 1
            continue

        discovered += 1
        result = filter_hotspot(story)
        total_cost += float(result["cost"].get("total_cost") or 0.0)
        relevant = result["relevant"] and result["score"] >= 3

        insert_hotspot(
            source=source,
            hotspot_id=sid,
            title=story["title"],
            url=story["url"],
            hn_score=story.get("score", 0),
            hn_descendants=story.get("descendants", 0),
            relevance_score=result["score"],
            relevance_reason=result["reason"],
            angle=result["angle"],
            cn_summary=result["cn_summary"],
        )

        if relevant:
            added += 1
            items.append({
                "source": source,
                "id": sid,
                "title": story["title"],
                "url": story["url"],
                "hn_score": story.get("score", 0),
                "hn_descendants": story.get("descendants", 0),
                "relevance_score": result["score"],
                "relevance_reason": result["reason"],
                "angle": result["angle"],
                "cn_summary": result["cn_summary"],
            })
        else:
            filtered_out += 1

    return {
        "ok": True,
        "discovered": discovered,
        "added": added,
        "skipped_seen": skipped_seen,
        "filtered_out": filtered_out,
        "items": items,
        "total_cost_cny": round(total_cost, 8),
    }
