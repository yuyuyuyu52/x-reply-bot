"""Sanity checks for src.hotspot.store.

These pin down:
  - Beijing-time timestamp formatting is host-tz independent (Asia/Shanghai
    regression — prior bug recorded local-tz time when host was not CST).
  - is_seen dedup keys on `source:id`.
  - recent_hotspots day-cutoff filter ignores anything older than `days`.
  - sqlite3.connect is called with `timeout=10` so concurrent writers don't
    raise OperationalError under brief lock contention.
  - Every connection opened by the store is closed (contextlib.closing) — we
    count open + close calls on a sqlite3.connect spy.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import src.hotspot.store as store  # noqa: E402

BEIJING_TZ = timezone(timedelta(hours=8))


class _FrozenBase(datetime):
    """Patchable datetime subclass; subclasses set `_FIXED`."""

    _FIXED: datetime = datetime(2026, 5, 11, 12, 0, 0, tzinfo=BEIJING_TZ)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls._FIXED.astimezone().replace(tzinfo=None)
        return cls._FIXED.astimezone(tz)


def _freeze(monkeypatch, when: datetime) -> None:
    """Pin store._now_beijing()'s underlying datetime.now to `when`."""

    class _Frozen(_FrozenBase):
        _FIXED = when

    monkeypatch.setattr(store, "datetime", _Frozen, raising=True)


@pytest.fixture(autouse=True)
def _isolate_hotspot_db(tmp_state, monkeypatch):
    """`tmp_state` retargets `src.common.HOTSPOT_STORE_PATH`, but `src.hotspot.store`
    binds the path at import time via `from src.common import HOTSPOT_STORE_PATH`,
    so we must also retarget the store module's own reference."""
    db_path = tmp_state / "hotspot.db"
    monkeypatch.setattr(store, "HOTSPOT_STORE_PATH", db_path, raising=True)
    yield db_path


@pytest.fixture
def freeze(monkeypatch):
    """Return a setter that freezes the store's datetime to a given instant."""

    def _set(when: datetime) -> None:
        _freeze(monkeypatch, when)

    return _set


def test_insert_records_beijing_timestamp_regardless_of_host_tz(tmp_state, freeze, monkeypatch):
    """Regression: timestamp must be Beijing wall-clock even when host TZ is NY."""
    # Force host TZ to America/New_York; without ZoneInfo("Asia/Shanghai") use,
    # strftime would emit EDT/EST instead of CST.
    original_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/New_York")
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        when = datetime(2026, 5, 11, 12, 30, 45, tzinfo=BEIJING_TZ)
        freeze(when)
        store.insert_hotspot("hn", "123", "Title", "https://example.com/x")
        rows = store.recent_hotspots(days=1)
        assert len(rows) == 1
        ts = rows[0]["discovered_at"]
        # "%Z" for Asia/Shanghai resolves to "CST" via zoneinfo.
        assert ts == "2026-05-11 12:30:45 CST", ts
        # Cross-check: must not contain EDT/EST/UTC.
        for forbidden in ("EDT", "EST", "UTC", "GMT"):
            assert forbidden not in ts
    finally:
        if original_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_tz)
        if hasattr(time, "tzset"):
            time.tzset()


def test_is_seen_dedup(tmp_state, freeze):
    freeze(datetime(2026, 5, 11, 12, 0, 0, tzinfo=BEIJING_TZ))
    assert store.is_seen("hn", "42") is False
    store.insert_hotspot("hn", "42", "Title", "https://example.com/42")
    assert store.is_seen("hn", "42") is True
    # Different source with the same id is a different key.
    assert store.is_seen("reddit", "42") is False
    # Different id, same source.
    assert store.is_seen("hn", "43") is False


def test_recent_hotspots_filters_by_day_cutoff(tmp_state, monkeypatch):
    """Insert 3 hotspots at 0d / 1d / 3d ago; recent(days=2) returns the first two."""
    today = datetime(2026, 5, 11, 12, 0, 0, tzinfo=BEIJING_TZ)
    yesterday = datetime(2026, 5, 10, 12, 0, 0, tzinfo=BEIJING_TZ)
    long_ago = datetime(2026, 5, 8, 12, 0, 0, tzinfo=BEIJING_TZ)

    _freeze(monkeypatch, long_ago)
    store.insert_hotspot("hn", "old", "Old", "https://example.com/old")
    _freeze(monkeypatch, yesterday)
    store.insert_hotspot("hn", "mid", "Mid", "https://example.com/mid")
    _freeze(monkeypatch, today)
    store.insert_hotspot("hn", "new", "New", "https://example.com/new")

    # _now_beijing() reads "today" — cutoff = 2026-05-09.
    rows = store.recent_hotspots(days=2, limit=50)
    ids = {row["id"] for row in rows}
    assert ids == {"hn:mid", "hn:new"}, ids


def test_recent_hotspots_cutoff_does_not_break_across_midnight(tmp_state, monkeypatch):
    """If `now` is just after midnight and a hotspot was inserted just before
    midnight `days` ago, the cutoff should still include it (cutoff is by date
    not by full timestamp)."""
    # Now = 2026-05-11 00:05 CST. days=1 → cutoff = "2026-05-10".
    _freeze(monkeypatch, datetime(2026, 5, 10, 23, 55, 0, tzinfo=BEIJING_TZ))
    store.insert_hotspot("hn", "boundary", "Boundary", "https://example.com/b")
    _freeze(monkeypatch, datetime(2026, 5, 11, 0, 5, 0, tzinfo=BEIJING_TZ))
    rows = store.recent_hotspots(days=1, limit=50)
    assert any(r["id"] == "hn:boundary" for r in rows)


def test_mark_added_to_queue(tmp_state, freeze):
    freeze(datetime(2026, 5, 11, 12, 0, 0, tzinfo=BEIJING_TZ))
    store.insert_hotspot("hn", "1", "T", "https://example.com/1")
    rows = store.recent_hotspots(days=1)
    assert rows[0]["added_to_queue"] == 0
    store.mark_added_to_queue("hn", "1")
    rows = store.recent_hotspots(days=1)
    assert rows[0]["added_to_queue"] == 1


def test_hotspot_stats(tmp_state, monkeypatch):
    today = datetime(2026, 5, 11, 12, 0, 0, tzinfo=BEIJING_TZ)
    earlier = datetime(2026, 5, 8, 9, 0, 0, tzinfo=BEIJING_TZ)
    _freeze(monkeypatch, earlier)
    store.insert_hotspot("hn", "old", "T", "https://example.com/old")
    _freeze(monkeypatch, today)
    store.insert_hotspot("hn", "a", "T", "https://example.com/a")
    store.insert_hotspot("hn", "b", "T", "https://example.com/b")
    store.mark_added_to_queue("hn", "a")

    stats = store.hotspot_stats()
    assert stats["total_discovered"] == 3
    assert stats["total_added_to_queue"] == 1
    assert stats["today_discovered"] == 2
    assert stats["today_added_to_queue"] == 1


# ---------------------------------------------------------------------------
# SQLite connection regressions: timeout + close.
# ---------------------------------------------------------------------------


def test_sqlite_connect_invoked_with_timeout_10(tmp_state, monkeypatch, freeze):
    """Regression: every connection must pass timeout=10 so concurrent writers
    block (briefly) instead of immediately raising OperationalError."""
    freeze(datetime(2026, 5, 11, 12, 0, 0, tzinfo=BEIJING_TZ))
    calls: list[dict] = []
    real_connect = sqlite3.connect

    def _spy(*args, **kwargs):
        calls.append({"args": args, "kwargs": dict(kwargs)})
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", _spy)
    store.insert_hotspot("hn", "1", "T", "https://example.com/1")

    assert calls, "sqlite3.connect was never called"
    for call in calls:
        # timeout=10 is keyword-only in store._get_conn.
        assert call["kwargs"].get("timeout") == 10, call


def test_connections_are_closed_via_contextlib_closing(tmp_state, monkeypatch, freeze):
    """Regression: each opened sqlite3 connection should be closed before the
    function returns. We wrap sqlite3.connect to track open + close pairs."""
    freeze(datetime(2026, 5, 11, 12, 0, 0, tzinfo=BEIJING_TZ))

    real_connect = sqlite3.connect
    closed: list[bool] = []

    class _TrackedConn:
        # __slots__ so attribute writes that aren't in this set fall through to
        # the wrapped connection via __setattr__ — needed because the store
        # does `conn.row_factory = sqlite3.Row`.
        _SELF_ATTRS = {"_conn", "_idx"}

        def __init__(self, conn):
            object.__setattr__(self, "_conn", conn)
            closed.append(False)  # opened, not yet closed
            object.__setattr__(self, "_idx", len(closed) - 1)

        def close(self):
            closed[self._idx] = True
            return self._conn.close()

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __setattr__(self, name, value):
            if name in _TrackedConn._SELF_ATTRS:
                object.__setattr__(self, name, value)
            else:
                setattr(self._conn, name, value)

    def _wrap(*args, **kwargs):
        return _TrackedConn(real_connect(*args, **kwargs))

    monkeypatch.setattr(sqlite3, "connect", _wrap)

    # A batch of operations that each open + close one connection.
    store.insert_hotspot("hn", "a", "T", "https://example.com/a")
    store.insert_hotspot("hn", "b", "T", "https://example.com/b")
    store.is_seen("hn", "a")
    store.recent_hotspots(days=1)
    store.hotspot_stats()
    store.mark_added_to_queue("hn", "a")

    assert closed, "no connections were opened"
    assert all(closed), f"some connections leaked: {closed}"


def test_concurrent_writes_do_not_raise_operational_error(tmp_state, freeze):
    """Soft regression: two near-simultaneous writers using the timeout=10
    connection should not raise sqlite3.OperationalError."""
    freeze(datetime(2026, 5, 11, 12, 0, 0, tzinfo=BEIJING_TZ))
    errors: list[Exception] = []

    def _writer(prefix: str) -> None:
        try:
            for i in range(5):
                store.insert_hotspot("hn", f"{prefix}-{i}", "T", f"https://example.com/{prefix}-{i}")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(p,)) for p in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent writes raised: {errors!r}"
    rows = store.recent_hotspots(days=1, limit=100)
    assert len(rows) == 10


def test_posted_at_column_added_on_open(tmp_path, monkeypatch):
    """旧库（无 posted_at 列）打开后应自动加列且不破坏数据。"""
    db_path = tmp_path / "legacy_hotspot.db"
    legacy = sqlite3.connect(str(db_path))
    legacy.executescript("""
        CREATE TABLE hotspots (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            hn_score INTEGER NOT NULL DEFAULT 0,
            hn_descendants INTEGER NOT NULL DEFAULT 0,
            relevance_score INTEGER NOT NULL DEFAULT 0,
            relevance_reason TEXT NOT NULL DEFAULT '',
            angle TEXT NOT NULL DEFAULT '',
            cn_summary TEXT NOT NULL DEFAULT '',
            discovered_at TEXT NOT NULL DEFAULT '',
            added_to_queue INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO hotspots(id, source, relevance_score, discovered_at)
        VALUES ('hn:legacy', 'hn', 4, '2026-05-13 10:00:00 CST');
    """)
    legacy.commit()
    legacy.close()

    monkeypatch.setattr(store, "HOTSPOT_STORE_PATH", db_path)

    # First call triggers schema migration.
    assert store.is_seen("hn", "legacy") is True

    with sqlite3.connect(str(db_path)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(hotspots)")}
        assert "posted_at" in cols
        # Existing row preserved with empty posted_at default.
        row = conn.execute(
            "SELECT posted_at FROM hotspots WHERE id = 'hn:legacy'"
        ).fetchone()
        assert row[0] == ""


def test_mark_posted_sets_timestamp(tmp_path, monkeypatch):
    db_path = tmp_path / "hot.db"
    monkeypatch.setattr(store, "HOTSPOT_STORE_PATH", db_path)
    store.insert_hotspot("hn", "42", "title", "url", relevance_score=4)
    assert store.is_seen("hn", "42")

    _freeze(monkeypatch, datetime(2026, 5, 13, 14, 30, 0, tzinfo=BEIJING_TZ))
    store.mark_posted("hn", "42")

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT posted_at FROM hotspots WHERE id='hn:42'").fetchone()
    assert row["posted_at"].startswith("2026-05-13 14:30:00")


def test_mark_posted_missing_row_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "HOTSPOT_STORE_PATH", tmp_path / "hot.db")
    # No insert — should not raise.
    store.mark_posted("hn", "nonexistent")


def test_unposted_candidates_within_filters_correctly(tmp_path, monkeypatch):
    db_path = tmp_path / "hot.db"
    monkeypatch.setattr(store, "HOTSPOT_STORE_PATH", db_path)

    _freeze(monkeypatch, datetime(2026, 5, 13, 12, 0, 0, tzinfo=BEIJING_TZ))

    # Fresh + high score → keep
    store.insert_hotspot("hn", "fresh", "Fresh post", "u1", relevance_score=4)
    # Fresh + low score → drop
    store.insert_hotspot("hn", "low", "Low score", "u2", relevance_score=2)
    # Older than 24h → drop (manually backdate)
    store.insert_hotspot("hn", "old", "Old post", "u3", relevance_score=5)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE hotspots SET discovered_at='2026-05-11 12:00:00 CST' WHERE id='hn:old'"
        )
        conn.commit()
    # Already posted → drop
    store.insert_hotspot("hn", "done", "Already posted", "u4", relevance_score=5)
    store.mark_posted("hn", "done")

    rows = store.unposted_candidates_within(hours=24, min_score=3)
    ids = [r["id"] for r in rows]
    assert ids == ["hn:fresh"]
    assert rows[0]["title"] == "Fresh post"
    assert rows[0]["relevance_score"] == 4


def test_posted_today_summaries_returns_only_today(tmp_path, monkeypatch):
    db_path = tmp_path / "hot.db"
    monkeypatch.setattr(store, "HOTSPOT_STORE_PATH", db_path)

    _freeze(monkeypatch, datetime(2026, 5, 13, 9, 0, 0, tzinfo=BEIJING_TZ))
    store.insert_hotspot("hn", "yest", "Yesterday hot", "u1",
                         relevance_score=4, cn_summary="昨日话题")
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE hotspots SET posted_at='2026-05-12 22:00:00 CST' WHERE id='hn:yest'"
        )
        conn.commit()

    _freeze(monkeypatch, datetime(2026, 5, 13, 12, 0, 0, tzinfo=BEIJING_TZ))
    store.insert_hotspot("hn", "today1", "Today hot 1", "u2",
                         relevance_score=4, cn_summary="今天 Claude 更新")
    store.mark_posted("hn", "today1")
    store.insert_hotspot("hn", "today2", "Today hot 2", "u3",
                         relevance_score=5, cn_summary="另一条今日热点")
    store.mark_posted("hn", "today2")

    summaries = store.posted_today_summaries()
    assert sorted(summaries) == sorted(["今天 Claude 更新", "另一条今日热点"])


if __name__ == "__main__":
    unittest.main()
