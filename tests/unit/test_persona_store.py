"""Unit tests for src.persona_store.

Locks down:
- Concurrent-write safety: the `.tmp.<pid>` staging filename + flock on
  `PERSONA_LOCK_PATH` (regression — earlier versions used a shared `.tmp`
  staging path so two writers could clobber each other).
- Field migration: legacy `timestamp`/`date` keys are mirrored to
  `time_beijing`/`date_beijing` on read while preserving the originals.
- New writes use canonical Beijing-time keys exclusively.
- `_relative_date` Chinese strings for today/yesterday/N-days/weeks-ago.
- `get_generation_context` slice + truncate caps (events 10, posts 8 × 100ch).
- `add_event` / `add_recent_post` ring-buffer caps (50, 15).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

import src.common as common_mod  # noqa: E402
import src.persona_store as persona_store  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def persona_paths(tmp_state, monkeypatch):
    """Make sure persona_store sees the tmp-state paths.

    The conftest `tmp_state` fixture retargets `src.common.PERSONA_PATH` but
    `src.persona_store` imports the constant by name at module load — that
    binding is now stale. Re-bind it explicitly for the duration of the test.
    """
    monkeypatch.setattr(persona_store, "PERSONA_PATH", common_mod.PERSONA_PATH)
    monkeypatch.setattr(persona_store, "PERSONA_LOCK_PATH", common_mod.PERSONA_LOCK_PATH)
    return {
        "persona": common_mod.PERSONA_PATH,
        "lock": common_mod.PERSONA_LOCK_PATH,
    }


# ---------------------------------------------------------------------------
# _migrate_record
# ---------------------------------------------------------------------------


class MigrateRecordTests(unittest.TestCase):
    def test_copies_legacy_timestamp_to_time_beijing(self):
        rec = {"timestamp": "2026-05-01 10:00:00 CST", "raw": "x"}
        out = persona_store._migrate_record(rec)
        self.assertEqual(out["time_beijing"], "2026-05-01 10:00:00 CST")
        # Original keys must survive — we don't rename, we mirror.
        self.assertEqual(out["timestamp"], "2026-05-01 10:00:00 CST")

    def test_copies_legacy_date_to_date_beijing(self):
        rec = {"date": "2026-05-01", "raw": "x"}
        out = persona_store._migrate_record(rec)
        self.assertEqual(out["date_beijing"], "2026-05-01")
        self.assertEqual(out["date"], "2026-05-01")

    def test_idempotent_when_canonical_present(self):
        rec = {
            "timestamp": "OLD",
            "date": "OLDDATE",
            "time_beijing": "NEW",
            "date_beijing": "NEWDATE",
        }
        out = persona_store._migrate_record(rec)
        # Should not overwrite the canonical fields with legacy values.
        self.assertEqual(out["time_beijing"], "NEW")
        self.assertEqual(out["date_beijing"], "NEWDATE")

    def test_non_dict_passthrough(self):
        self.assertIsNone(persona_store._migrate_record(None))
        self.assertEqual(persona_store._migrate_record("not a dict"), "not a dict")


# ---------------------------------------------------------------------------
# load_persona + on-read migration
# ---------------------------------------------------------------------------


def test_load_persona_missing_returns_skeleton(persona_paths):
    data = persona_store.load_persona()
    assert data == {"static": {}, "events": [], "recent_posts": []}


def test_load_persona_migrates_legacy_fields(persona_paths):
    legacy = {
        "static": {"name": "test"},
        "events": [
            {"raw": "e1", "timestamp": "2026-05-01 10:00:00 CST", "date": "2026-05-01"},
        ],
        "recent_posts": [
            {"text": "p1", "timestamp": "2026-05-02 10:00:00 CST", "date": "2026-05-02"},
        ],
    }
    persona_paths["persona"].write_text(json.dumps(legacy), encoding="utf-8")

    loaded = persona_store.load_persona()
    ev = loaded["events"][0]
    assert ev["time_beijing"] == "2026-05-01 10:00:00 CST"
    assert ev["date_beijing"] == "2026-05-01"
    # Originals preserved.
    assert ev["timestamp"] == "2026-05-01 10:00:00 CST"
    assert ev["date"] == "2026-05-01"

    rp = loaded["recent_posts"][0]
    assert rp["time_beijing"] == "2026-05-02 10:00:00 CST"
    assert rp["date_beijing"] == "2026-05-02"


def test_load_persona_skips_non_dict_entries(persona_paths):
    persona_paths["persona"].write_text(
        json.dumps({"events": ["bad", {"raw": "ok"}], "recent_posts": [None, {"text": "ok"}]}),
        encoding="utf-8",
    )
    loaded = persona_store.load_persona()
    assert len(loaded["events"]) == 1
    assert len(loaded["recent_posts"]) == 1


# ---------------------------------------------------------------------------
# New writes use canonical keys only
# ---------------------------------------------------------------------------


def test_add_event_uses_canonical_keys(persona_paths):
    persona_store.add_event("hello there")
    saved = json.loads(persona_paths["persona"].read_text(encoding="utf-8"))
    ev = saved["events"][-1]
    assert "time_beijing" in ev
    assert "date_beijing" in ev
    # No legacy keys on a freshly-written event.
    assert "timestamp" not in ev
    assert "date" not in ev
    assert ev["raw"] == "hello there"
    assert ev["source"] == "telegram"


def test_add_event_custom_source(persona_paths):
    persona_store.add_event("note", source="cron")
    saved = json.loads(persona_paths["persona"].read_text(encoding="utf-8"))
    assert saved["events"][-1]["source"] == "cron"


def test_add_recent_post_uses_canonical_keys(persona_paths):
    persona_store.add_recent_post("a post", "argument")
    saved = json.loads(persona_paths["persona"].read_text(encoding="utf-8"))
    post = saved["recent_posts"][-1]
    assert "time_beijing" in post
    assert "date_beijing" in post
    assert "timestamp" not in post
    assert "date" not in post
    assert post["text"] == "a post"
    assert post["topic_type"] == "argument"


# ---------------------------------------------------------------------------
# save_persona — per-pid tmp suffix regression
# ---------------------------------------------------------------------------


def test_save_persona_uses_pid_suffixed_tmp(persona_paths, monkeypatch):
    """REGRESSION: two writers must not share the same .tmp path.

    The fix renames the staging file from `persona.tmp` to
    `persona.tmp.<pid>`. We verify by intercepting `Path.with_suffix` on
    the persona path and asserting the suffix includes the live PID.
    """
    seen_suffixes: list[str] = []
    real_with_suffix = Path.with_suffix

    def spy_with_suffix(self, suffix):
        seen_suffixes.append(suffix)
        return real_with_suffix(self, suffix)

    monkeypatch.setattr(Path, "with_suffix", spy_with_suffix)
    persona_store.save_persona({"static": {}, "events": [], "recent_posts": []})

    pid = os.getpid()
    assert any(s == f".tmp.{pid}" for s in seen_suffixes), (
        f"expected a .tmp.{pid} stage filename; saw {seen_suffixes!r}"
    )


def test_concurrent_add_event_both_persisted(persona_paths):
    """REGRESSION: two threads each appending an event must both land.

    Pre-fix, the shared `.tmp` staging path + missing flock meant the
    second writer could clobber the first. The fix combines a flock on
    `PERSONA_LOCK_PATH` with a `.tmp.<pid>` staging file.
    """
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(tag: str):
        try:
            barrier.wait(timeout=2)
            persona_store.add_event(f"event-{tag}")
        except BaseException as e:  # noqa: BLE001 -- record for assertion
            errors.append(e)

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"workers raised: {errors!r}"

    saved = json.loads(persona_paths["persona"].read_text(encoding="utf-8"))
    raws = [e["raw"] for e in saved["events"]]
    assert "event-a" in raws
    assert "event-b" in raws


# ---------------------------------------------------------------------------
# _relative_date
# ---------------------------------------------------------------------------


@pytest.fixture
def freeze_today(monkeypatch):
    """Freeze `datetime.now().astimezone().date()` inside persona_store."""
    fixed = datetime(2026, 5, 11, 12, 0, 0)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed
            return fixed.astimezone(tz)

    monkeypatch.setattr(persona_store, "datetime", _Frozen)
    return fixed


def test_relative_date_today(freeze_today):
    today = freeze_today.date().isoformat()
    assert persona_store._relative_date(today) == "今天"


def test_relative_date_yesterday(freeze_today):
    yest = (freeze_today.date() - timedelta(days=1)).isoformat()
    assert persona_store._relative_date(yest) == "昨天"


def test_relative_date_2_days_ago(freeze_today):
    d = (freeze_today.date() - timedelta(days=2)).isoformat()
    assert persona_store._relative_date(d) == "2天前"


def test_relative_date_7_days_ago(freeze_today):
    # 7 days is still <= 7 → "7天前" (boundary).
    d = (freeze_today.date() - timedelta(days=7)).isoformat()
    assert persona_store._relative_date(d) == "7天前"


def test_relative_date_8_days_ago_about_one_week(freeze_today):
    d = (freeze_today.date() - timedelta(days=8)).isoformat()
    assert persona_store._relative_date(d) == "约1周前"


def test_relative_date_30_days_ago_about_four_weeks(freeze_today):
    d = (freeze_today.date() - timedelta(days=30)).isoformat()
    # 30 // 7 == 4
    assert persona_store._relative_date(d) == "约4周前"


def test_relative_date_invalid_returns_empty(freeze_today):
    assert persona_store._relative_date("not-a-date") == ""
    assert persona_store._relative_date("") == ""
    assert persona_store._relative_date(None) == ""


# ---------------------------------------------------------------------------
# get_generation_context — slice + truncate caps
# ---------------------------------------------------------------------------


def test_get_generation_context_caps_recent_events_to_10(persona_paths):
    events = [
        {"raw": f"event{i}", "date_beijing": "2026-05-01"}
        for i in range(15)
    ]
    persona_paths["persona"].write_text(
        json.dumps({"static": {}, "events": events, "recent_posts": []}),
        encoding="utf-8",
    )
    ctx = persona_store.get_generation_context()
    assert len(ctx["recent_events"]) == 10
    # The last 10 are kept (slice is `[-10:]`).
    assert ctx["recent_events"][0]["raw"] == "event5"
    assert ctx["recent_events"][-1]["raw"] == "event14"


def test_get_generation_context_caps_recent_posts_to_8(persona_paths):
    posts = [
        {"text": f"post{i}", "date_beijing": "2026-05-01", "topic_type": "argument"}
        for i in range(12)
    ]
    persona_paths["persona"].write_text(
        json.dumps({"static": {}, "events": [], "recent_posts": posts}),
        encoding="utf-8",
    )
    ctx = persona_store.get_generation_context()
    assert len(ctx["recent_posts"]) == 8
    assert ctx["recent_posts"][0]["text"] == "post4"
    assert ctx["recent_posts"][-1]["text"] == "post11"


def test_get_generation_context_truncates_post_text_to_100(persona_paths):
    long_text = "x" * 250
    posts = [{"text": long_text, "date_beijing": "2026-05-01", "topic_type": "story"}]
    persona_paths["persona"].write_text(
        json.dumps({"static": {}, "events": [], "recent_posts": posts}),
        encoding="utf-8",
    )
    ctx = persona_store.get_generation_context()
    assert len(ctx["recent_posts"][0]["text"]) == 100
    assert ctx["recent_posts"][0]["text"] == "x" * 100


def test_get_generation_context_preserves_static(persona_paths):
    persona_paths["persona"].write_text(
        json.dumps({"static": {"name": "TestBot", "bio": "hi"}, "events": [], "recent_posts": []}),
        encoding="utf-8",
    )
    ctx = persona_store.get_generation_context()
    assert ctx["static"] == {"name": "TestBot", "bio": "hi"}


def test_get_generation_context_uses_legacy_date_fallback(persona_paths):
    # Pre-migration entries have only `date` / `timestamp`; the
    # `_migrate_record` step inside `load_persona` should have mirrored them
    # into `date_beijing`, so the consumer still sees the date.
    persona_paths["persona"].write_text(
        json.dumps({
            "static": {},
            "events": [{"raw": "legacy", "date": "2026-05-01"}],
            "recent_posts": [{"text": "old", "date": "2026-05-02", "topic_type": "casual"}],
        }),
        encoding="utf-8",
    )
    ctx = persona_store.get_generation_context()
    assert ctx["recent_posts"][0]["date"] == "2026-05-02"


# ---------------------------------------------------------------------------
# Ring-buffer caps for add_event / add_recent_post
# ---------------------------------------------------------------------------


def test_add_event_caps_at_50(persona_paths):
    for i in range(55):
        persona_store.add_event(f"e{i}")
    saved = json.loads(persona_paths["persona"].read_text(encoding="utf-8"))
    assert len(saved["events"]) == 50
    # Newest survives, oldest 5 dropped.
    raws = [e["raw"] for e in saved["events"]]
    assert raws[0] == "e5"
    assert raws[-1] == "e54"


def test_add_recent_post_caps_at_15(persona_paths):
    for i in range(20):
        persona_store.add_recent_post(f"post{i}", "argument")
    saved = json.loads(persona_paths["persona"].read_text(encoding="utf-8"))
    assert len(saved["recent_posts"]) == 15
    texts = [p["text"] for p in saved["recent_posts"]]
    assert texts[0] == "post5"
    assert texts[-1] == "post19"
