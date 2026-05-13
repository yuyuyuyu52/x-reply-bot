from __future__ import annotations

import discover_hotspots
from discover_hotspots import _notify, _queue_daily_hotspot_topics


def test_queue_daily_hotspot_topics_replaces_stale_pending_hotspots_first():
    data = {
        "topics": [
            {"id": "manual", "source": "manual", "status": "pending", "text": "manual topic"},
            {"id": "hotspot-old", "source": "hotspot", "status": "pending", "text": "old hot"},
            {"id": "hotspot-used", "source": "hotspot", "status": "used", "text": "used hot"},
        ]
    }
    new_topics = [
        {"id": "hotspot-new-1", "source": "hotspot", "status": "pending", "text": "new 1"},
        {"id": "hotspot-new-2", "source": "hotspot", "status": "pending", "text": "new 2"},
    ]

    skipped = _queue_daily_hotspot_topics(data, new_topics)

    assert skipped == 1
    assert [item["id"] for item in data["topics"][:2]] == ["hotspot-new-1", "hotspot-new-2"]
    assert data["topics"][2]["id"] == "manual"
    assert data["topics"][3]["id"] == "hotspot-old"
    assert data["topics"][3]["status"] == "skipped"
    assert data["topics"][4]["id"] == "hotspot-used"


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
