"""Unit tests for src.postable_pool."""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def pool(tmp_state, monkeypatch):
    """Fresh postable_pool module with state isolated to tmp_state."""
    import src.postable_pool as mod
    importlib.reload(mod)
    return mod


def test_next_topic_to_post_returns_manual_first(pool, monkeypatch):
    fake_topic = {"id": "m1", "source": "manual", "status": "pending", "text": "hi"}
    monkeypatch.setattr(pool.topics, "next_pending_post_topic", lambda: fake_topic)
    monkeypatch.setattr(
        pool.selector, "pick_best",
        MagicMock(side_effect=AssertionError("should not call selector when manual present"))
    )

    topic = pool.next_topic_to_post()
    assert topic["id"] == "m1"
    assert topic["_pool"] == "manual"
    assert topic.get("_pool_ref") == ""


def test_next_topic_to_post_falls_back_to_hotspot(pool, monkeypatch):
    monkeypatch.setattr(pool.topics, "next_pending_post_topic", lambda: None)
    hotspot_topic = {
        "id": "hotspot-hn-9", "type": "news_react", "source": "hotspot",
        "status": "pending", "text": "x", "_pool": "hotspot", "_pool_ref": "hn:9",
    }
    monkeypatch.setattr(pool.selector, "pick_best", lambda: hotspot_topic)

    topic = pool.next_topic_to_post()
    assert topic["_pool"] == "hotspot"
    assert topic["_pool_ref"] == "hn:9"


def test_next_topic_to_post_returns_none_when_all_empty(pool, monkeypatch):
    monkeypatch.setattr(pool.topics, "next_pending_post_topic", lambda: None)
    monkeypatch.setattr(pool.selector, "pick_best", lambda: None)
    assert pool.next_topic_to_post() is None


def test_mark_topic_used_manual_dispatches_to_topics(pool, monkeypatch):
    called = {}
    def fake_mark(topic_id, status, extra):
        called["args"] = (topic_id, status, extra)
    monkeypatch.setattr(pool.topics, "mark_post_topic_status", fake_mark)
    spy = MagicMock()
    monkeypatch.setattr(pool.hotspot_store, "mark_posted", spy)

    topic = {"id": "m1", "_pool": "manual", "_pool_ref": ""}
    pool.mark_topic_used(topic, status="used", extra={"used_at": "now"})

    assert called["args"] == ("m1", "used", {"used_at": "now"})
    spy.assert_not_called()


def test_mark_topic_used_hotspot_dispatches_to_store(pool, monkeypatch):
    spy_store = MagicMock()
    monkeypatch.setattr(pool.hotspot_store, "mark_posted", spy_store)
    spy_topics = MagicMock()
    monkeypatch.setattr(pool.topics, "mark_post_topic_status", spy_topics)

    topic = {"id": "hotspot-hn-9", "_pool": "hotspot", "_pool_ref": "hn:9"}
    pool.mark_topic_used(topic, status="used")

    spy_store.assert_called_once_with("hn", "9")
    spy_topics.assert_not_called()


def test_mark_topic_used_hotspot_ignores_non_used_status(pool, monkeypatch):
    spy_store = MagicMock()
    monkeypatch.setattr(pool.hotspot_store, "mark_posted", spy_store)
    topic = {"_pool": "hotspot", "_pool_ref": "hn:9"}
    pool.mark_topic_used(topic, status="failed")
    spy_store.assert_not_called()


def test_mark_topic_used_hotspot_dispatches_skipped_to_store(pool, monkeypatch):
    """Skipped (e.g. LLM rewrite review rejected) marks hotspot as consumed
    too — otherwise it'd be re-picked next run and burn LLM cost."""
    spy_store = MagicMock()
    monkeypatch.setattr(pool.hotspot_store, "mark_posted", spy_store)
    topic = {"id": "hotspot-hn-9", "_pool": "hotspot", "_pool_ref": "hn:9"}
    pool.mark_topic_used(topic, status="skipped")
    spy_store.assert_called_once_with("hn", "9")


def test_mark_topic_used_missing_pool_field_warns_and_noops(pool, monkeypatch, caplog):
    spy_store = MagicMock()
    spy_topics = MagicMock()
    monkeypatch.setattr(pool.hotspot_store, "mark_posted", spy_store)
    monkeypatch.setattr(pool.topics, "mark_post_topic_status", spy_topics)

    pool.mark_topic_used({"id": "orphan"}, status="used")
    spy_store.assert_not_called()
    spy_topics.assert_not_called()


def test_legacy_migration_marks_pending_hotspot_topics_skipped(pool, monkeypatch):
    # Seed post_topics.json with legacy pending hotspot rows.
    from src.common import POST_TOPICS_PATH
    POST_TOPICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    initial = {
        "topics": [
            {"id": "manual1", "source": "manual", "status": "pending", "text": "m"},
            {"id": "hotspot-old1", "source": "hotspot", "status": "pending", "text": "h1"},
            {"id": "hotspot-old2", "source": "hotspot", "status": "pending", "text": "h2"},
            {"id": "hotspot-used", "source": "hotspot", "status": "used", "text": "u"},
        ]
    }
    POST_TOPICS_PATH.write_text(json.dumps(initial), encoding="utf-8")

    monkeypatch.setattr(pool.topics, "next_pending_post_topic", lambda: None)
    monkeypatch.setattr(pool.selector, "pick_best", lambda: None)

    pool.next_topic_to_post()  # Triggers migration.

    after = json.loads(POST_TOPICS_PATH.read_text(encoding="utf-8"))
    by_id = {t["id"]: t for t in after["topics"]}
    assert by_id["manual1"]["status"] == "pending"
    assert by_id["hotspot-old1"]["status"] == "skipped"
    assert by_id["hotspot-old1"]["skip_reason"] == "migrated_to_db_pool"
    assert "migrated_at" in by_id["hotspot-old1"]
    assert by_id["hotspot-old2"]["status"] == "skipped"
    assert by_id["hotspot-used"]["status"] == "used"

    # Second call should not modify (idempotent + process-cached).
    mtime_before = POST_TOPICS_PATH.stat().st_mtime
    pool.next_topic_to_post()
    assert POST_TOPICS_PATH.stat().st_mtime == mtime_before


def test_pool_status_aggregates_both_stores(pool, monkeypatch):
    monkeypatch.setattr(pool.topics, "post_topic_summary",
                        lambda: {"pending": 2, "used": 5, "skipped": 1, "total": 8})
    monkeypatch.setattr(pool.hotspot_store, "unposted_candidates_within",
                        lambda *a, **k: [{"id": "hn:1"}, {"id": "hn:2"}, {"id": "hn:3"}])
    monkeypatch.setattr(pool.hotspot_store, "hotspot_stats",
                        lambda: {"total_discovered": 100, "total_added_to_queue": 0,
                                 "today_discovered": 25, "today_added_to_queue": 0})
    monkeypatch.setattr(pool.hotspot_store, "posted_today_summaries",
                        lambda: ["a", "b"])

    status = pool.pool_status()
    assert status["manual"]["pending"] == 2
    assert status["hotspot"]["pool_size_24h"] == 3
    assert status["hotspot"]["discovered_today"] == 25
    assert status["hotspot"]["posted_today"] == 2
