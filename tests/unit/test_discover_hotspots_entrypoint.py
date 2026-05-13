from __future__ import annotations

from unittest.mock import MagicMock

import discover_hotspots
from discover_hotspots import _notify


def test_notify_labels_discovered_as_candidates_and_includes_filtered_sample(monkeypatch):
    sent = {}
    monkeypatch.setattr(discover_hotspots, "telegram_enabled", lambda: True)
    monkeypatch.setattr(discover_hotspots, "telegram_notify", lambda text: sent.setdefault("text", text))

    _notify({
        "time_beijing": "2026-05-13 13:23:35 CST",
        "trigger": "telegram",
        "discovered": 10,
        "added": 0,
        "skipped_seen": 0,
        "filtered_out": 10,
        "total_cost_cny": 0.044276,
        "added_items": [],
        "filtered_items": [
            {"source": "hn", "title": "Generic infra topic", "relevance_score": 1, "relevance_reason": "偏基础设施"},
        ],
    })

    assert "📊 评估候选: 10 条" in sent["text"]
    assert "发现: 10 条新热点" not in sent["text"]
    assert "被过滤样例" in sent["text"]
    assert "Generic infra topic" in sent["text"]


def test_main_does_not_touch_post_topics_json(tmp_state, monkeypatch):
    """discover entrypoint must not read or write post_topics.json anymore."""
    import discover_hotspots as entry

    monkeypatch.setattr("sys.argv", ["discover_hotspots.py", "--trigger", "manual"])
    monkeypatch.setattr(entry, "telegram_enabled", lambda: False)
    monkeypatch.setattr(
        entry,
        "discover_hotspots",
        lambda: {
            "ok": True, "discovered": 5, "added": 2, "skipped_seen": 0,
            "filtered_out": 3, "source_stats": {}, "source_durations": {},
            "items": [
                {"source": "hn", "id": "1", "title": "t1", "url": "u1",
                 "hn_score": 10, "hn_descendants": 2, "rank_score": 1.0,
                 "relevance_score": 4, "relevance_reason": "r",
                 "angle": "a", "cn_summary": "s"},
            ],
            "filtered_items": [],
            "total_cost_cny": 0.0,
        },
    )

    sentinel_load = MagicMock(side_effect=AssertionError("load_post_topics must not be called"))
    sentinel_save = MagicMock(side_effect=AssertionError("save_post_topics must not be called"))
    sentinel_mark = MagicMock(side_effect=AssertionError("mark_added_to_queue must not be called"))
    monkeypatch.setattr(entry, "load_post_topics", sentinel_load, raising=False)
    monkeypatch.setattr(entry, "save_post_topics", sentinel_save, raising=False)
    monkeypatch.setattr(entry, "mark_added_to_queue", sentinel_mark, raising=False)

    rc = entry.main()
    assert rc == 0
