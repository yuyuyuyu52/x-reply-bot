from __future__ import annotations

from discover_hotspots import _queue_daily_hotspot_topics


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
