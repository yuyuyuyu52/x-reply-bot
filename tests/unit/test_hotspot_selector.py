"""Unit tests for src.hotspot.selector.pick_best."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import src.hotspot.selector as selector  # noqa: E402

BEIJING_TZ = timezone(timedelta(hours=8))


def _row(
    *,
    source="hn",
    hotspot_id="1",
    title="t",
    url="u",
    relevance_score=4,
    relevance_reason="r",
    angle="a",
    cn_summary="cn",
    hn_score=10,
    hn_descendants=5,
    age_hours=1.0,
    now=None,
):
    base = now or datetime(2026, 5, 13, 12, 0, 0, tzinfo=BEIJING_TZ)
    discovered = (base - timedelta(hours=age_hours)).strftime("%Y-%m-%d %H:%M:%S %Z")
    return {
        "id": f"{source}:{hotspot_id}",
        "source": source,
        "title": title,
        "url": url,
        "hn_score": hn_score,
        "hn_descendants": hn_descendants,
        "relevance_score": relevance_score,
        "relevance_reason": relevance_reason,
        "angle": angle,
        "cn_summary": cn_summary,
        "discovered_at": discovered,
        "posted_at": "",
    }


def test_pick_best_returns_none_when_no_candidates(monkeypatch):
    monkeypatch.setattr(selector.store, "unposted_candidates_within", lambda *a, **k: [])
    monkeypatch.setattr(selector.store, "posted_today_summaries", lambda: [])
    monkeypatch.setattr(selector, "chat_json_result", MagicMock(side_effect=AssertionError("should not call LLM")))
    assert selector.pick_best() is None


def test_pick_best_single_candidate_returns_topic_dict(monkeypatch):
    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=BEIJING_TZ)
    row = _row(
        source="hn", hotspot_id="42",
        title="Claude Code 3.0 announced",
        url="https://hn.x/42",
        relevance_score=5,
        relevance_reason="主流 AI 编程工具更新",
        angle="工作流变化",
        cn_summary="Claude Code 3.0 发布",
        hn_score=300, hn_descendants=120,
        age_hours=2.0, now=now,
    )
    monkeypatch.setattr(selector.store, "unposted_candidates_within", lambda *a, **k: [row])
    monkeypatch.setattr(selector.store, "posted_today_summaries", lambda: [])
    monkeypatch.setattr(
        selector, "chat_json_result",
        MagicMock(return_value={"payload": {"best_index": 0, "reason": "唯一候选"},
                                "cost": {"total_cost": 0.001}, "usage": {}})
    )

    topic = selector.pick_best(now=now)
    assert topic is not None
    assert topic["id"] == "hotspot-hn-42"
    assert topic["type"] == "news_react"
    assert topic["text"] == "Claude Code 3.0 发布"
    assert topic["source"] == "hotspot"
    assert topic["status"] == "pending"
    assert topic["subject"] == "Claude Code 3.0 announced"
    assert "今天[hn]" in topic["event_or_context"]
    assert "https://hn.x/42" in topic["event_or_context"]
    assert topic["stance"] == "工作流变化"
    assert "300↑" in topic["evidence_hint"] and "120💬" in topic["evidence_hint"]
    assert "5/5" in topic["evidence_hint"]
    assert topic["_pool"] == "hotspot"
    assert topic["_pool_ref"] == "hn:42"


def test_pick_best_orders_by_score_times_freshness(monkeypatch):
    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=BEIJING_TZ)
    # row_a: score=4 age=1h (weight 1.0) → 4.0
    # row_b: score=5 age=20h (weight ≈ 1 - 0.7*14/18 = 0.456) → ≈ 2.28
    # row_c: score=3 age=3h (weight 1.0) → 3.0
    # Expected top 5 order: a, c, b
    row_a = _row(source="hn", hotspot_id="a", relevance_score=4, age_hours=1.0, now=now,
                 cn_summary="A summary")
    row_b = _row(source="hn", hotspot_id="b", relevance_score=5, age_hours=20.0, now=now,
                 cn_summary="B summary")
    row_c = _row(source="hn", hotspot_id="c", relevance_score=3, age_hours=3.0, now=now,
                 cn_summary="C summary")
    monkeypatch.setattr(selector.store, "unposted_candidates_within",
                        lambda *a, **k: [row_a, row_b, row_c])
    monkeypatch.setattr(selector.store, "posted_today_summaries", lambda: [])
    seen_payload = {}
    def fake_llm(messages, **kwargs):
        seen_payload["user"] = messages[-1]["content"]
        return {"payload": {"best_index": 0, "reason": "ok"},
                "cost": {"total_cost": 0.0}, "usage": {}}
    monkeypatch.setattr(selector, "chat_json_result", fake_llm)

    topic = selector.pick_best(now=now)
    assert topic["_pool_ref"] == "hn:a"  # top of local ranking
    # The LLM saw all three in order [a, c, b]
    payload = seen_payload["user"]
    pos_a = payload.find('"A summary"')
    pos_c = payload.find('"C summary"')
    pos_b = payload.find('"B summary"')
    assert 0 <= pos_a < pos_c < pos_b


def test_pick_best_returns_none_when_llm_says_all_dup(monkeypatch):
    row = _row(source="hn", hotspot_id="1", cn_summary="重复主题")
    monkeypatch.setattr(selector.store, "unposted_candidates_within", lambda *a, **k: [row])
    monkeypatch.setattr(selector.store, "posted_today_summaries", lambda: ["重复主题"])
    monkeypatch.setattr(
        selector, "chat_json_result",
        MagicMock(return_value={"payload": {"best_index": -1, "reason": "全部跟已发重复"},
                                "cost": {"total_cost": 0.001}, "usage": {}})
    )
    assert selector.pick_best() is None


def test_pick_best_returns_none_when_llm_index_out_of_range(monkeypatch):
    row = _row()
    monkeypatch.setattr(selector.store, "unposted_candidates_within", lambda *a, **k: [row])
    monkeypatch.setattr(selector.store, "posted_today_summaries", lambda: [])
    monkeypatch.setattr(
        selector, "chat_json_result",
        MagicMock(return_value={"payload": {"best_index": 99, "reason": "x"},
                                "cost": {}, "usage": {}})
    )
    assert selector.pick_best() is None


def test_pick_best_returns_none_when_llm_raises(monkeypatch):
    row = _row()
    monkeypatch.setattr(selector.store, "unposted_candidates_within", lambda *a, **k: [row])
    monkeypatch.setattr(selector.store, "posted_today_summaries", lambda: [])
    monkeypatch.setattr(
        selector, "chat_json_result",
        MagicMock(side_effect=RuntimeError("LLM down"))
    )
    assert selector.pick_best() is None
