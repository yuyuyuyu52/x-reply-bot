#!/usr/bin/env python3
"""Hotspot discovery: fetch from external sources, LLM filter, score."""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime

from src.common import chat_json_result
from src.hotspot.store import is_seen, insert_hotspot
from src.logger import get_logger

logger = get_logger(__name__)

HN_TOP_STORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{}.json"
HN_MAX_FETCH = 30

HOTSPOT_FILTER_PROMPT = """\
你在筛选与指定关注方向相关的热点新闻，用于 X 账号发帖。

输出严格 JSON：
{"relevant": true, "score": 3, "reason": "...", "angle": "...", "cn_summary": "..."}

关注方向（按优先级排序）：
- 【最高优先级】AI + vibe coding：AI 编程工具、AI 工作流自动化、AI agent、LLM 辅助开发、cursor/windsurf/copilot 等工具生态、低代码与 AI 结合
- 【高优先级】AI 工作流：AI 如何改变日常工作方式、AI 自动化实践、人与 AI 协作模式
- 【中等优先级】创业/startup、产品/增长、开发者工具、web3/加密货币
- 【低优先级】AI 其他方面（模型发布、学术论文、融资新闻）——仅供评分参考，不优先选取

规则：
- relevant: 是否与上述方向相关，AI+vibe coding 和 AI 工作流优先判断
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
    logger.info("hn_fetch: requesting top stories list")
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                HN_TOP_STORIES_URL,
                headers={"User-Agent": "x-reply-bot/1.0"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                ids = json.loads(resp.read().decode())
            break
        except Exception:
            if attempt < 2:
                logger.warning("hn_fetch: top stories attempt %d failed, retrying", attempt + 1)
                time.sleep(1)
            else:
                raise

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
    logger.info("hn_fetch: got %d stories from top list", len(stories))
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
                logger.info("discover: fetching HN top stories")
                stories = fetch_hn_top_stories()
                all_stories.extend({"source": "hn", **s} for s in stories)
                logger.info("discover: HN fetch complete, %d total stories", len(all_stories))
            except Exception as exc:
                logger.error("discover: HN fetch failed: %s", exc)
                return {
                    "ok": False,
                    "error": f"hn_fetch_failed: {exc}",
                    "discovered": 0,
                    "added": 0,
                    "skipped_seen": 0,
                    "filtered_out": 0,
                    "errors": 0,
                    "items": [],
                    "total_cost_cny": 0.0,
                }

    # --- Filter: dedup + LLM score ---
    discovered = 0
    added = 0
    skipped_seen = 0
    filtered_out = 0
    errors = 0
    items: list[dict] = []

    for story in all_stories:
        source = story["source"]
        sid = story["id"]
        if is_seen(source, sid):
            skipped_seen += 1
            continue

        discovered += 1
        try:
            result = filter_hotspot(story)
        except Exception as exc:
            errors += 1
            logger.warning(
                "discover: LLM filter failed for hn:%s title=%.40s: %s",
                sid, story.get("title", ""), exc,
            )
            continue

        total_cost += float(result["cost"].get("total_cost") or 0.0)
        relevant = result["relevant"] and result["score"] >= 3
        logger.info(
            "discover: hn:%s score=%d relevant=%s reason=%s",
            sid, result["score"], relevant, result["reason"],
        )

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

    logger.info(
        "discover: batch complete discovered=%d added=%d skipped_seen=%d filtered_out=%d errors=%d cost_cny=%.6f",
        discovered, added, skipped_seen, filtered_out, errors, total_cost,
    )

    return {
        "ok": True,
        "discovered": discovered,
        "added": added,
        "skipped_seen": skipped_seen,
        "filtered_out": filtered_out,
        "errors": errors,
        "items": items,
        "total_cost_cny": round(total_cost, 8),
    }
