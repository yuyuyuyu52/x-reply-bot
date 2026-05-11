"""Full helper coverage for ``src.learning.store``.

Companion to ``test_learning_quality.py`` (which covers ``_best_label`` /
``QUALITY_RANK``). This file exercises the SQLite-backed helpers and locks
down the regressions documented in their respective sections:

* Every helper closes the SQLite connection it opens (no leaks).
* ``sqlite3.connect`` is called with a non-default ``timeout``.
* ``ensure_learning_storage`` migrates legacy tables that are missing
  columns added in later SCHEMA revisions.
* ``upsert_learning_post`` never downgrades a quality_label and merges
  engagement counts via ``max``.
* ``recent_learning_references`` / ``top_learning_posts`` order by quality
  and respect the limit.
* ``record_learning_run`` appends a row including the Beijing-time stamp.

All tests are isolated to ``tmp_state`` so they cannot touch the real
``state/learning.db``.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


CST = timezone(timedelta(hours=8))


def _fresh_module(tmp_state):
    """Return ``src.learning.store`` with paths re-pointed at ``tmp_state``.

    ``tmp_state`` already re-targets STATE_DIR on the module if it was
    previously imported. We import here (post-fixture) so attribute reads
    see the patched values regardless of import order.
    """
    from src.learning import store as learning_store

    # Defensive: confirm the conftest fixture re-pointed the path constants.
    assert str(learning_store.LEARNING_DB_PATH).startswith(str(tmp_state))
    assert str(learning_store.LEARNING_HISTORY_DIR).startswith(str(tmp_state))
    assert str(learning_store.LATEST_LEARNING_RUN_PATH).startswith(str(tmp_state))
    return learning_store


def _sample_post(**overrides):
    base = {
        "status_url": "https://x.com/u/status/1",
        "observed_at": "2026-05-11 12:00:00 CST",
        "trigger": "schedule",
        "author_handle": "@alice",
        "author_name": "Alice",
        "relative_time": "1h",
        "post_text": "hello world",
        "language": "en",
        "views": 100,
        "replies": 2,
        "reposts": 3,
        "likes": 10,
        "bookmarks": 1,
        "engagement_score": 1.5,
        "quality_label": "seen",
        "quality_score": 0.5,
        "format_guess": "short",
        "hook_type": "question",
        "style_summary": "punchy",
        "structure_pattern": "hook->point->cta",
        "why_it_works": "specific",
        "imitation_takeaway": "be specific",
        "innovation_direction": "use numbers",
        "quality_reason": "ok",
        "raw": {"foo": "bar"},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Regression: connection-close + timeout kwargs
# ---------------------------------------------------------------------------


class _TrackingConnection:
    """Thin wrapper around a real sqlite3.Connection that records close().

    Forwards attribute reads AND writes (e.g. ``conn.row_factory = ...``)
    through to the wrapped connection so callers don't notice the wrapper.
    """

    def __init__(self, real, tracker):
        # Use object.__setattr__ for internal state so our __setattr__
        # override doesn't recurse / forward these to the real conn.
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_tracker", tracker)
        object.__setattr__(self, "_closed", False)
        tracker["opened"] += 1

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        if name in {"_real", "_tracker", "_closed"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)

    def close(self):
        if not self._closed:
            self._tracker["closed"] += 1
            object.__setattr__(self, "_closed", True)
        return self._real.close()


@pytest.fixture
def tracking_sqlite(monkeypatch):
    """Patch ``sqlite3.connect`` (as seen by ``learning_store``) to count opens/closes.

    Also captures the kwargs each call was made with — so tests can assert
    ``timeout=10`` is being passed.
    """
    tracker = {"opened": 0, "closed": 0, "calls": []}
    real_connect = sqlite3.connect

    def fake_connect(*args, **kwargs):
        tracker["calls"].append({"args": args, "kwargs": dict(kwargs)})
        real = real_connect(*args, **kwargs)
        return _TrackingConnection(real, tracker)

    # learning_store does `import sqlite3` then `sqlite3.connect(...)`, so
    # patching the attribute on the module's sqlite3 reference is enough.
    from src.learning import store as learning_store

    monkeypatch.setattr(learning_store.sqlite3, "connect", fake_connect)
    return tracker


def test_connection_leaks_none(tmp_state, tracking_sqlite):
    """Every helper opens via sqlite3.connect must close it. Regression: contextlib.closing."""
    store = _fresh_module(tmp_state)

    store.ensure_learning_storage()
    store.upsert_learning_post(_sample_post())
    store.recent_learning_references(limit=5)
    store.top_learning_posts(limit=3)
    store.learning_counts()
    store.record_learning_run({
        "stamp": "20260511_120000",
        "time_beijing": "2026-05-11 12:00:00 CST",
        "trigger": "schedule",
        "status": "ok",
        "scanned_count": 1,
        "analyzed_count": 1,
        "saved_count": 1,
        "high_quality_count": 0,
        "worth_watching_count": 0,
        "total_cost_cny": 0.0,
        "summary": "test",
    })

    assert tracking_sqlite["opened"] > 0
    assert tracking_sqlite["closed"] == tracking_sqlite["opened"], (
        f"Connection leak: opened={tracking_sqlite['opened']} closed={tracking_sqlite['closed']}"
    )


def test_sqlite_timeout_kwarg(tmp_state, tracking_sqlite):
    """Regression: sqlite3.connect must be called with timeout=10 to avoid 'database is locked'."""
    store = _fresh_module(tmp_state)
    store.ensure_learning_storage()

    assert tracking_sqlite["calls"], "sqlite3.connect was never called"
    for call in tracking_sqlite["calls"]:
        timeout = call["kwargs"].get("timeout")
        assert timeout is not None and timeout > 0, (
            f"sqlite3.connect was called without a positive timeout: kwargs={call['kwargs']}"
        )


# ---------------------------------------------------------------------------
# Regression: SCHEMA migration adds missing columns
# ---------------------------------------------------------------------------


def test_migration_adds_missing_columns(tmp_state):
    """A legacy DB missing recently-added columns must be migrated to the full schema.

    We seed a table that has the columns referenced by the SCHEMA indexes
    (so the ``CREATE INDEX IF NOT EXISTS`` statements in SCHEMA don't blow
    up before ``_migrate_schema`` runs) but lacks newer columns such as
    ``bookmarks`` / ``format_guess`` / ``imitation_takeaway``. After
    ``ensure_learning_storage`` runs, all expected columns must exist.
    """
    store = _fresh_module(tmp_state)

    # Pre-create a "legacy" learned_posts table missing many columns added
    # in later releases. Include the columns SCHEMA's CREATE INDEX
    # statements reference so index creation doesn't fail before migration.
    store.LEARNING_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(store.LEARNING_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE learned_posts (
                status_url TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL DEFAULT '',
                quality_label TEXT NOT NULL DEFAULT 'seen',
                quality_score REAL NOT NULL DEFAULT 0,
                engagement_score REAL NOT NULL DEFAULT 0
            )
            """
        )

    # Sanity: before migration the table is sparse — confirm a column we
    # expect the migration to ADD is currently missing.
    with sqlite3.connect(store.LEARNING_DB_PATH) as conn:
        cols_before = {row[1] for row in conn.execute("PRAGMA table_info(learned_posts)").fetchall()}
    assert "bookmarks" not in cols_before
    assert "format_guess" not in cols_before
    assert "imitation_takeaway" not in cols_before

    # Run migration via the public entrypoint.
    store.ensure_learning_storage()

    # Every non-PK column in EXPECTED_COLUMNS must now exist.
    with sqlite3.connect(store.LEARNING_DB_PATH) as conn:
        cols_after = {row[1] for row in conn.execute("PRAGMA table_info(learned_posts)").fetchall()}

    expected = set(store.EXPECTED_COLUMNS["learned_posts"].keys())
    missing = expected - cols_after
    assert not missing, f"Migration left columns missing: {missing}"

    # Inserting a full record must succeed against the migrated schema.
    store.upsert_learning_post(_sample_post(quality_label="high_quality"))
    refs = store.recent_learning_references(limit=5)
    assert len(refs) == 1
    assert refs[0]["status_url"] == "https://x.com/u/status/1"
    assert refs[0]["views"] == 100  # column added by the migration accepts data

    # And the bookmarks column (added by migration) holds the inserted value.
    with sqlite3.connect(store.LEARNING_DB_PATH) as conn:
        bm = conn.execute(
            "SELECT bookmarks FROM learned_posts WHERE status_url = ?",
            ("https://x.com/u/status/1",),
        ).fetchone()
    assert bm[0] == 1


def test_migration_is_idempotent(tmp_state):
    """Calling ensure_learning_storage twice must not raise (re-running ALTERs)."""
    store = _fresh_module(tmp_state)
    store.ensure_learning_storage()
    store.ensure_learning_storage()  # second pass must be a no-op


# ---------------------------------------------------------------------------
# upsert_learning_post
# ---------------------------------------------------------------------------


def test_upsert_insert_new_row(tmp_state):
    store = _fresh_module(tmp_state)
    store.upsert_learning_post(_sample_post(quality_label="high_quality"))

    refs = store.recent_learning_references(limit=10)
    assert len(refs) == 1
    row = refs[0]
    assert row["status_url"] == "https://x.com/u/status/1"
    assert row["quality_label"] == "high_quality"
    assert row["author_handle"] == "@alice"


def test_upsert_no_status_url_is_noop(tmp_state):
    store = _fresh_module(tmp_state)
    store.upsert_learning_post(_sample_post(status_url=""))
    counts = store.learning_counts()
    assert counts["total"] == 0


def test_upsert_upgrades_quality_label(tmp_state):
    """seen -> high_quality must upgrade."""
    store = _fresh_module(tmp_state)
    store.upsert_learning_post(_sample_post(quality_label="seen"))
    store.upsert_learning_post(_sample_post(quality_label="high_quality"))

    counts = store.learning_counts()
    assert counts["total"] == 1
    assert counts["high_quality"] == 1


def test_upsert_does_not_downgrade_quality_label(tmp_state):
    """high_quality must NOT be downgraded by a later 'seen'."""
    store = _fresh_module(tmp_state)
    store.upsert_learning_post(_sample_post(quality_label="high_quality"))
    store.upsert_learning_post(_sample_post(quality_label="seen"))

    counts = store.learning_counts()
    assert counts["high_quality"] == 1
    assert counts["total"] == 1


def test_upsert_counts_merge_via_max(tmp_state):
    """Engagement counters (views, likes, etc.) must merge via max(old, new)."""
    store = _fresh_module(tmp_state)
    store.upsert_learning_post(_sample_post(
        quality_label="high_quality",
        views=100, likes=10, reposts=3, replies=2, bookmarks=4, engagement_score=1.5,
    ))
    store.upsert_learning_post(_sample_post(
        quality_label="high_quality",
        views=50,  # lower — must NOT clobber 100
        likes=20,  # higher — must win
        reposts=1,
        replies=5,
        bookmarks=0,
        engagement_score=0.1,
    ))

    # Verify via direct DB read so we can inspect every column (the public
    # SELECTs project a subset).
    with sqlite3.connect(store.LEARNING_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT views, likes, reposts, replies, bookmarks, engagement_score "
            "FROM learned_posts WHERE status_url = ?",
            ("https://x.com/u/status/1",),
        ).fetchone()
    assert row is not None
    assert row["views"] == 100
    assert row["likes"] == 20
    assert row["reposts"] == 3
    assert row["replies"] == 5
    assert row["bookmarks"] == 4
    assert row["engagement_score"] == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# recent_learning_references / top_learning_posts
# ---------------------------------------------------------------------------


def test_recent_references_filters_low_quality(tmp_state):
    """Only high_quality / worth_watching rows are returned."""
    store = _fresh_module(tmp_state)
    store.upsert_learning_post(_sample_post(status_url="u/1", quality_label="seen"))
    store.upsert_learning_post(_sample_post(status_url="u/2", quality_label="skip"))
    store.upsert_learning_post(_sample_post(status_url="u/3", quality_label="worth_watching"))
    store.upsert_learning_post(_sample_post(status_url="u/4", quality_label="high_quality"))

    refs = store.recent_learning_references(limit=10)
    urls = {r["status_url"] for r in refs}
    assert urls == {"u/3", "u/4"}


def test_recent_references_orders_by_score(tmp_state):
    """Higher quality_score then engagement_score must come first."""
    store = _fresh_module(tmp_state)
    store.upsert_learning_post(_sample_post(
        status_url="u/low", quality_label="high_quality",
        quality_score=0.2, engagement_score=0.5,
    ))
    store.upsert_learning_post(_sample_post(
        status_url="u/mid", quality_label="high_quality",
        quality_score=0.5, engagement_score=0.1,
    ))
    store.upsert_learning_post(_sample_post(
        status_url="u/top", quality_label="high_quality",
        quality_score=0.9, engagement_score=0.3,
    ))

    refs = store.recent_learning_references(limit=5)
    ordered = [r["status_url"] for r in refs]
    assert ordered == ["u/top", "u/mid", "u/low"]


def test_recent_references_respects_limit(tmp_state):
    store = _fresh_module(tmp_state)
    for i in range(5):
        store.upsert_learning_post(_sample_post(
            status_url=f"u/{i}", quality_label="high_quality", quality_score=float(i),
        ))
    refs = store.recent_learning_references(limit=2)
    assert len(refs) == 2
    # Highest quality_score first.
    assert [r["status_url"] for r in refs] == ["u/4", "u/3"]


def test_top_learning_posts_basic(tmp_state):
    """top_learning_posts mirrors recent_learning_references' ordering with a smaller projection."""
    store = _fresh_module(tmp_state)
    store.upsert_learning_post(_sample_post(
        status_url="u/a", quality_label="worth_watching", quality_score=0.3,
    ))
    store.upsert_learning_post(_sample_post(
        status_url="u/b", quality_label="high_quality", quality_score=0.9,
    ))
    store.upsert_learning_post(_sample_post(
        status_url="u/c", quality_label="seen",  # excluded
    ))

    tops = store.top_learning_posts(limit=3)
    urls = [t["status_url"] for t in tops]
    assert urls == ["u/b", "u/a"]
    # Projection should expose at least these fields.
    assert "post_text" in tops[0]
    assert "why_it_works" in tops[0]


# ---------------------------------------------------------------------------
# record_learning_run
# ---------------------------------------------------------------------------


def test_record_learning_run_inserts_and_writes_latest(tmp_state):
    store = _fresh_module(tmp_state)
    record = {
        "stamp": "20260511_120000",
        "time_beijing": "2026-05-11 12:00:00 CST",
        "trigger": "schedule",
        "status": "ok",
        "scanned_count": 7,
        "analyzed_count": 5,
        "saved_count": 3,
        "high_quality_count": 1,
        "worth_watching_count": 2,
        "total_cost_cny": 0.123,
        "summary": "ran fine",
    }
    store.record_learning_run(record)

    # latest_learning_run.json is overwritten on every run.
    assert store.LATEST_LEARNING_RUN_PATH.exists()

    # The history archive uses the stamp as filename.
    archived = store.learning_history_path_for("20260511_120000")
    assert archived.exists(), f"Expected archive at {archived}"

    # A row should land in learning_runs with the Beijing-time stamp.
    with sqlite3.connect(store.LEARNING_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT observed_at, trigger, status, scanned_count, saved_count, summary "
            "FROM learning_runs ORDER BY id"
        ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["observed_at"] == "2026-05-11 12:00:00 CST"
    assert row["trigger"] == "schedule"
    assert row["status"] == "ok"
    assert row["scanned_count"] == 7
    assert row["saved_count"] == 3
    assert row["summary"] == "ran fine"

    # learning_counts surfaces the latest-run metadata.
    counts = store.learning_counts()
    assert counts["latest_time"] == "2026-05-11 12:00:00 CST"
    assert counts["latest_status"] == "ok"


def test_record_learning_run_multiple_appends(tmp_state):
    store = _fresh_module(tmp_state)
    for i in range(3):
        store.record_learning_run({
            "stamp": f"2026051{i}_120000",
            "time_beijing": f"2026-05-1{i} 12:00:00 CST",
            "trigger": "schedule",
            "status": "ok",
        })
    with sqlite3.connect(store.LEARNING_DB_PATH) as conn:
        count = conn.execute("SELECT COUNT(*) FROM learning_runs").fetchone()[0]
    assert count == 3


def test_learning_counts_empty(tmp_state):
    store = _fresh_module(tmp_state)
    counts = store.learning_counts()
    assert counts == {
        "total": 0,
        "high_quality": 0,
        "worth_watching": 0,
        "latest_time": "",
        "latest_status": "",
    }
