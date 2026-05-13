"""Sanity test for reporters.post_summary using pool_status."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def test_post_summary_shows_manual_and_hotspot_pool(tmp_state, monkeypatch):
    import src.reporters as reporters
    monkeypatch.setattr(
        reporters.postable_pool, "pool_status",
        lambda: {
            "manual": {"pending": 2, "used": 5, "skipped": 1, "total": 8},
            "hotspot": {"pool_size_24h": 7, "discovered_today": 25, "posted_today": 1},
        },
    )
    monkeypatch.setattr(reporters, "count_scheduled_posts", lambda day: 1)

    out = reporters.post_summary(datetime(2026, 5, 13, 19, 0, 0, tzinfo=timezone(timedelta(hours=8))))
    assert "人工待发" in out and "2" in out
    assert "热点池" in out and "7" in out
    assert "今日定时已发" in out
