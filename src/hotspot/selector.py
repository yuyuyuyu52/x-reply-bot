#!/usr/bin/env python3
"""Pick the most-postable hotspot row from the store.

Local scoring = relevance_score × freshness_weight(age_hours).
Then one LLM call decides among top 5 vs. today's already-posted summaries.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from src.common import chat_json_result
from src.hotspot import store
from src.logger import get_logger

logger = get_logger(__name__)

BEIJING_TZ = ZoneInfo("Asia/Shanghai")

CANDIDATE_HOURS = 24
MIN_SCORE = 3
TOP_N_FOR_LLM = 5

SELECTOR_PROMPT = """\
你在为一个中文 X 账号挑选今天最该发的一条热点。

输入会给你 N 个候选热点（按本地新鲜度+相关度初排），以及今天已经发过的热点摘要列表。

输出严格 JSON：
{"best_index": 整数, "reason": "..."}

规则：
- best_index = 0..N-1，表示选中候选数组的第几个；
- 若所有候选的主题都跟"已发列表"中某条本质上是同一件事（同公司同事件同产品），
  返回 best_index = -1；
- 若多个候选都不跟已发重复，挑发帖价值更高的一个（更新更猛 / 切入角度更独特）；
- reason 用中文，30 字内。
- 只输出 JSON，不要 markdown 包裹。
"""


def _parse_discovered(raw: str) -> Optional[datetime]:
    """Parse 'YYYY-MM-DD HH:MM:SS CST' → aware datetime in Beijing tz."""
    if not raw:
        return None
    try:
        head = raw.rsplit(" ", 1)[0]
        return datetime.strptime(head, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TZ)
    except ValueError:
        return None


def _freshness_weight(age_hours: float) -> float:
    if age_hours <= 6:
        return 1.0
    if age_hours >= 24:
        return 0.0
    return 1.0 - 0.7 * (age_hours - 6) / 18.0


def _local_score(row: dict, now: datetime) -> float:
    disc = _parse_discovered(row.get("discovered_at", ""))
    if disc is None:
        age = 0.0
    else:
        age = max(0.0, (now - disc).total_seconds() / 3600.0)
    return float(row.get("relevance_score") or 0) * _freshness_weight(age)


def _row_to_topic(row: dict) -> dict:
    source = str(row.get("source") or "")
    raw_id = row["id"].split(":", 1)[1] if ":" in str(row.get("id") or "") else ""
    return {
        "id": f"hotspot-{source}-{raw_id}",
        "type": "news_react",
        "text": str(row.get("cn_summary") or ""),
        "source": "hotspot",
        "status": "pending",
        "subject": str(row.get("title") or ""),
        "event_or_context": (
            f"今天[{source}] {row.get('relevance_reason') or ''} "
            f"| 原链接: {row.get('url') or ''}"
        ),
        "stance": str(row.get("angle") or ""),
        "evidence_hint": (
            f"热度: {row.get('hn_score') or 0}↑ "
            f"{row.get('hn_descendants') or 0}💬 "
            f"| 相关度: {row.get('relevance_score') or 0}/5"
        ),
        "_pool": "hotspot",
        "_pool_ref": str(row.get("id") or ""),
    }


def pick_best(now: datetime | None = None) -> dict | None:
    when = now or datetime.now(tz=BEIJING_TZ)

    try:
        candidates = store.unposted_candidates_within(hours=CANDIDATE_HOURS, min_score=MIN_SCORE)
    except Exception as exc:
        logger.warning("selector: store query failed: %s", exc)
        return None

    if not candidates:
        logger.info("selector: no candidates")
        return None

    ranked = sorted(
        candidates,
        key=lambda r: (-_local_score(r, when), -int(r.get("relevance_score") or 0),
                       str(r.get("id") or "")),
    )[:TOP_N_FOR_LLM]

    try:
        posted = store.posted_today_summaries()
    except Exception as exc:
        logger.warning("selector: posted_today_summaries failed: %s", exc)
        posted = []

    llm_input = {
        "candidates": [
            {
                "index": i,
                "title": r.get("title") or "",
                "cn_summary": r.get("cn_summary") or "",
                "source": r.get("source") or "",
                "relevance_score": r.get("relevance_score") or 0,
                "angle": r.get("angle") or "",
            }
            for i, r in enumerate(ranked)
        ],
        "already_posted_today": posted,
    }

    try:
        response = chat_json_result(
            [
                {"role": "system", "content": SELECTOR_PROMPT},
                {"role": "user", "content": json.dumps(llm_input, ensure_ascii=False)},
            ],
            temperature=0.2,
            max_tokens=200,
        )
    except Exception as exc:
        logger.warning("selector: LLM call failed: %s", exc)
        return None

    payload = response.get("payload") or {}
    try:
        idx = int(payload.get("best_index"))
    except (TypeError, ValueError):
        logger.warning("selector: LLM returned non-int best_index: %r", payload)
        return None

    if idx == -1:
        logger.info("selector: LLM says all candidates duplicate today's posts (reason=%s)",
                    payload.get("reason"))
        return None
    if idx < 0 or idx >= len(ranked):
        logger.warning("selector: LLM returned out-of-range index %d (n=%d)", idx, len(ranked))
        return None

    selected = ranked[idx]
    logger.info("selector: picked %s (score=%d, reason=%s)",
                selected.get("id"), selected.get("relevance_score"), payload.get("reason"))
    return _row_to_topic(selected)
