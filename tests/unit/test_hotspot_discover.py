from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import src.hotspot.discover as discover
from src.hotspot.discover import _configured_sources, _producthunt_access_token, _select_llm_candidates


def test_configured_sources_default_to_fast_heat_sources(monkeypatch):
    monkeypatch.delenv("X_HOTSPOT_SOURCES", raising=False)

    assert _configured_sources() == ["hn", "producthunt", "reddit", "hf_papers"]


def test_configured_sources_allows_known_overrides_and_dedupes(monkeypatch):
    monkeypatch.setenv("X_HOTSPOT_SOURCES", "openai, hn, unknown, hn")

    assert _configured_sources() == ["openai", "hn"]


def test_select_llm_candidates_prioritizes_prd_relevant_hot_items():
    stories = [
        {"source": "hn", "id": "seen", "title": "Seen", "score": 999, "descendants": 999},
        {"source": "hn", "id": "generic", "title": "Database internals benchmark", "score": 150, "descendants": 50},
        {"source": "producthunt", "id": "cursor-agent", "title": "Cursor agent workflow for vibe coding", "score": 80, "descendants": 12},
        {"source": "reddit", "id": "claude-code", "title": "Claude Code changed my solo dev workflow", "score": 70, "descendants": 30},
        {"source": "hf_papers", "id": "paper", "title": "New LLM benchmark paper", "score": 180, "descendants": 0},
    ]

    selected, skipped_seen = _select_llm_candidates(
        stories,
        limit=3,
        is_seen_func=lambda source, sid: source == "hn" and sid == "seen",
    )

    assert skipped_seen == 1
    assert [(item["source"], item["id"]) for item in selected] == [
        ("producthunt", "cursor-agent"),
        ("reddit", "claude-code"),
        ("hn", "generic"),
    ]


def test_producthunt_source_skips_without_token(monkeypatch):
    monkeypatch.delenv("PRODUCT_HUNT_TOKEN", raising=False)
    monkeypatch.delenv("X_PRODUCT_HUNT_TOKEN", raising=False)
    monkeypatch.delenv("X_PRODUCT_HUNT_API_KEY", raising=False)
    monkeypatch.delenv("X_PRODUCT_HUNT_API_SECRET", raising=False)

    assert discover.fetch_producthunt_posts() == []


def test_producthunt_access_token_uses_client_credentials(monkeypatch):
    monkeypatch.delenv("PRODUCT_HUNT_TOKEN", raising=False)
    monkeypatch.delenv("X_PRODUCT_HUNT_TOKEN", raising=False)
    monkeypatch.setenv("X_PRODUCT_HUNT_API_KEY", "client-id")
    monkeypatch.setenv("X_PRODUCT_HUNT_API_SECRET", "client-secret")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"access_token":"client-token"}'

    seen = {}

    def _fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = req.data.decode("utf-8")
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    assert _producthunt_access_token() == "client-token"
    assert seen["url"].endswith("/v2/oauth/token")
    assert '"grant_type": "client_credentials"' in seen["body"]


def test_producthunt_posts_query_is_scoped_to_beijing_today(monkeypatch):
    monkeypatch.setenv("X_PRODUCT_HUNT_TOKEN", "token")

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 5, 13, 11, 20, 0, tzinfo=timezone(timedelta(hours=8)))
            return fixed if tz is None else fixed.astimezone(tz)

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"data":{"posts":{"edges":[]}}}'

    seen = {}

    def _fake_urlopen(req, timeout):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(discover, "datetime", _FrozenDatetime)
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    assert discover.fetch_producthunt_posts() == []
    assert "postedAfter" in seen["body"]["query"]
    assert seen["body"]["variables"]["postedAfter"] == "2026-05-12T11:20:00+08:00"


def test_company_x_scrape_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("X_HOTSPOT_ENABLE_X_SCRAPE", raising=False)
    run_harness = MagicMock()
    monkeypatch.setattr("src.common.run_harness", run_harness, raising=False)

    assert discover._fetch_company_x_profile("openai") == []
    run_harness.assert_not_called()
