"""Coverage for the post-topic queue (``src.topics`` + ``post_topics.py``).

Locks down:

* ``VALID_POST_TOPIC_TYPES`` is the canonical set the rest of the codebase
  branches on — accidental edits change behavior silently.
* ``normalize_post_topic`` coerces unknown types to ``argument`` and fills
  missing ``status`` / ``source`` defaults.
* ``next_pending_post_topic`` is FIFO and skips non-pending entries.
* ``mark_post_topic_status`` takes the ``blocking_lock`` (regression: the
  daemon + Telegram-triggered ``post_once`` + the CLI all race on the same
  JSON queue and the lock was added later to serialize them).
* Read-modify-write through ``mark_post_topic_status`` from concurrent
  threads doesn't lose updates.

All tests use ``tmp_state`` so ``POST_TOPICS_PATH`` lives in a temp dir.
"""
from __future__ import annotations

import json
import threading

import pytest


@pytest.fixture
def tmp_topics(tmp_state, monkeypatch):
    """Return (common, topics) with POST_TOPICS_PATH / POST_TOPICS_LOCK_PATH pointed at tmp_state.

    The shared ``tmp_state`` fixture re-points ``STATE_DIR`` and any
    ``*_PATH`` / ``*_DIR`` attribute on modules that *also* define
    ``STATE_DIR``. ``src.topics`` only re-exports the path constants from
    ``src.common``, so the bindings inside ``src.topics`` keep pointing at
    the real repo state directory. We explicitly retarget them here.
    """
    from src import common, topics

    # ``src.common`` is retargeted by the tmp_state fixture itself.
    assert str(common.POST_TOPICS_PATH).startswith(str(tmp_state))

    # Force topics' own bindings to follow.
    monkeypatch.setattr(topics, "POST_TOPICS_PATH", common.POST_TOPICS_PATH, raising=False)
    monkeypatch.setattr(
        topics, "POST_TOPICS_LOCK_PATH", common.POST_TOPICS_LOCK_PATH, raising=False
    )

    return common, topics


def _fresh_modules(tmp_topics_pair):
    """Alias for readability — tests still call ``_fresh_modules(tmp_topics)``."""
    return tmp_topics_pair


# ---------------------------------------------------------------------------
# VALID_POST_TOPIC_TYPES
# ---------------------------------------------------------------------------


def test_valid_post_topic_types_set(tmp_state):
    """Pin the canonical set so additions/removals are explicit."""
    from src.common import VALID_POST_TOPIC_TYPES

    assert VALID_POST_TOPIC_TYPES == {
        "news_react",
        "story",
        "argument",
        "casual",
        "thread",
        "article",
    }


# ---------------------------------------------------------------------------
# normalize_post_topic
# ---------------------------------------------------------------------------


def test_normalize_unknown_type_coerced_to_argument(tmp_topics):
    _, topics = _fresh_modules(tmp_topics)
    out = topics.normalize_post_topic({"text": "hi", "type": "rant"})
    assert out["type"] == "argument"


def test_normalize_valid_types_preserved(tmp_topics):
    _, topics = _fresh_modules(tmp_topics)
    for valid in ("news_react", "story", "argument", "casual", "thread", "article"):
        out = topics.normalize_post_topic({"text": "x", "type": valid})
        assert out["type"] == valid, f"Type {valid!r} was mangled to {out['type']!r}"


def test_normalize_missing_type_defaults_to_argument(tmp_topics):
    _, topics = _fresh_modules(tmp_topics)
    out = topics.normalize_post_topic({"text": "no type given"})
    assert out["type"] == "argument"


def test_normalize_missing_status_defaults_pending(tmp_topics):
    _, topics = _fresh_modules(tmp_topics)
    out = topics.normalize_post_topic({"text": "hi"})
    assert out["status"] == "pending"


def test_normalize_preserves_existing_status(tmp_topics):
    _, topics = _fresh_modules(tmp_topics)
    out = topics.normalize_post_topic({"text": "hi", "status": "used"})
    assert out["status"] == "used"


def test_normalize_default_source_manual(tmp_topics):
    _, topics = _fresh_modules(tmp_topics)
    out = topics.normalize_post_topic({"text": "hi"})
    assert out["source"] == "manual"


def test_normalize_strips_whitespace(tmp_topics):
    _, topics = _fresh_modules(tmp_topics)
    out = topics.normalize_post_topic({"text": "  hello  ", "id": "  t1 "})
    assert out["text"] == "hello"
    assert out["id"] == "t1"


def test_normalize_case_insensitive_type(tmp_topics):
    _, topics = _fresh_modules(tmp_topics)
    out = topics.normalize_post_topic({"text": "x", "type": "  ARGUMENT "})
    assert out["type"] == "argument"


def test_normalize_stance_backfilled_from_text(tmp_topics):
    _, topics = _fresh_modules(tmp_topics)
    out = topics.normalize_post_topic({"text": "ai is mid"})
    assert out["stance"] == "ai is mid"


def test_normalize_text_backfilled_from_subject(tmp_topics):
    _, topics = _fresh_modules(tmp_topics)
    out = topics.normalize_post_topic({"subject": "kubernetes", "event_or_context": "outage"})
    # topic_summary_text composes "subject / event_or_context" when text is empty.
    assert "kubernetes" in out["text"]
    assert "outage" in out["text"]


# ---------------------------------------------------------------------------
# load / save / next_pending
# ---------------------------------------------------------------------------


def _write_queue(common, items):
    common.POST_TOPICS_PATH.write_text(
        json.dumps({"topics": items}, ensure_ascii=False),
        encoding="utf-8",
    )


def test_load_post_topics_empty_when_missing(tmp_topics):
    common, topics = _fresh_modules(tmp_topics)
    # POST_TOPICS_PATH should not exist yet.
    assert not common.POST_TOPICS_PATH.exists()
    data = topics.load_post_topics()
    assert data == {"topics": []}


def test_load_post_topics_normalizes_each_entry(tmp_topics):
    common, topics = _fresh_modules(tmp_topics)
    _write_queue(common, [
        {"id": "a", "text": "ok", "type": "garbage"},
        {"id": "b", "text": "hi", "type": "story"},
        "not a dict",  # ignored
    ])
    data = topics.load_post_topics()
    assert len(data["topics"]) == 2
    assert data["topics"][0]["type"] == "argument"  # coerced
    assert data["topics"][1]["type"] == "story"
    # Defaults filled in.
    for t in data["topics"]:
        assert t["status"] == "pending"
        assert t["source"] == "manual"


def test_save_and_reload_roundtrip(tmp_topics):
    common, topics = _fresh_modules(tmp_topics)
    data = {"topics": [
        topics.normalize_post_topic({"id": "x1", "text": "hello", "type": "casual"}),
    ]}
    topics.save_post_topics(data)
    reloaded = topics.load_post_topics()
    assert reloaded["topics"][0]["id"] == "x1"
    assert reloaded["topics"][0]["type"] == "casual"


def test_next_pending_returns_first_pending(tmp_topics):
    common, topics = _fresh_modules(tmp_topics)
    _write_queue(common, [
        {"id": "1", "text": "first", "status": "used"},
        {"id": "2", "text": "second", "status": "pending"},
        {"id": "3", "text": "third", "status": "pending"},
    ])
    nxt = topics.next_pending_post_topic()
    assert nxt is not None
    assert nxt["id"] == "2"


def test_next_pending_skips_non_pending_statuses(tmp_topics):
    common, topics = _fresh_modules(tmp_topics)
    _write_queue(common, [
        {"id": "1", "text": "a", "status": "used"},
        {"id": "2", "text": "b", "status": "skipped"},
    ])
    assert topics.next_pending_post_topic() is None


def test_next_pending_when_queue_empty(tmp_topics):
    _, topics = _fresh_modules(tmp_topics)
    assert topics.next_pending_post_topic() is None


# ---------------------------------------------------------------------------
# mark_post_topic_status — lock + read-modify-write
# ---------------------------------------------------------------------------


def test_mark_post_topic_status_updates_entry(tmp_topics):
    common, topics = _fresh_modules(tmp_topics)
    _write_queue(common, [
        {"id": "t1", "text": "hi", "status": "pending"},
        {"id": "t2", "text": "yo", "status": "pending"},
    ])
    updated = topics.mark_post_topic_status("t1", "used", extra={"post_url": "https://x.com/x/status/9"})
    assert updated["id"] == "t1"
    assert updated["status"] == "used"
    assert updated["post_url"] == "https://x.com/x/status/9"

    reloaded = topics.load_post_topics()
    by_id = {t["id"]: t for t in reloaded["topics"]}
    assert by_id["t1"]["status"] == "used"
    assert by_id["t1"]["post_url"] == "https://x.com/x/status/9"
    assert by_id["t2"]["status"] == "pending"  # untouched


def test_mark_post_topic_status_unknown_id_returns_empty(tmp_topics):
    common, topics = _fresh_modules(tmp_topics)
    _write_queue(common, [{"id": "t1", "text": "hi", "status": "pending"}])
    out = topics.mark_post_topic_status("does-not-exist", "used")
    assert out == {}
    # Existing entry must be untouched.
    reloaded = topics.load_post_topics()
    assert reloaded["topics"][0]["status"] == "pending"


def test_mark_post_topic_status_uses_blocking_lock(tmp_topics, monkeypatch):
    """Regression: mark_post_topic_status must hold blocking_lock around the read-modify-write.

    Without the lock, a concurrent daemon mark + CLI append race; the fix
    wired ``blocking_lock(POST_TOPICS_LOCK_PATH)`` into the function. We
    confirm that whenever mark_post_topic_status runs, blocking_lock is
    entered with the post-topics lock path.
    """
    common, topics = _fresh_modules(tmp_topics)
    _write_queue(common, [{"id": "t1", "text": "hi", "status": "pending"}])

    calls = []
    real_blocking_lock = common.blocking_lock

    def spy(path):
        calls.append(path)
        return real_blocking_lock(path)

    # mark_post_topic_status imports `blocking_lock` from src.common into
    # src.topics' namespace via `from src.common import blocking_lock`, so
    # the patch must be applied on src.topics (the binding the function
    # closes over).
    monkeypatch.setattr(topics, "blocking_lock", spy)

    topics.mark_post_topic_status("t1", "used")

    assert calls, "mark_post_topic_status did not call blocking_lock"
    # Verify it locks the *post-topics* lock path specifically (so it
    # serializes with post_topics.py CLI appends).
    assert any(str(p) == str(common.POST_TOPICS_LOCK_PATH) for p in calls), (
        f"blocking_lock was called but not with POST_TOPICS_LOCK_PATH: {calls}"
    )


def test_mark_post_topic_status_concurrent_threads(tmp_topics):
    """Two concurrent mark calls on different topics must both land.

    Exercises the lock under contention: without ``blocking_lock`` one
    writer's read-modify-write would clobber the other's update.
    """
    common, topics = _fresh_modules(tmp_topics)
    _write_queue(common, [
        {"id": f"t{i}", "text": f"topic {i}", "status": "pending"}
        for i in range(10)
    ])

    targets = [f"t{i}" for i in range(10)]
    errors = []

    def worker(topic_id):
        try:
            topics.mark_post_topic_status(topic_id, "used")
        except Exception as exc:  # pragma: no cover - surfaced via errors list
            errors.append((topic_id, exc))

    threads = [threading.Thread(target=worker, args=(tid,)) for tid in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f"Worker threads raised: {errors}"

    reloaded = topics.load_post_topics()
    by_id = {t["id"]: t for t in reloaded["topics"]}
    for tid in targets:
        assert by_id[tid]["status"] == "used", (
            f"Topic {tid} was not marked used — concurrent write lost. "
            f"Full queue: {[(t['id'], t['status']) for t in reloaded['topics']]}"
        )


def test_mark_post_topic_status_simple_smoke_roundtrip(tmp_topics):
    """load -> mutate-and-save -> load -> mutation present (simpler RMW smoke)."""
    common, topics = _fresh_modules(tmp_topics)
    _write_queue(common, [{"id": "alpha", "text": "a", "status": "pending"}])

    snapshot = topics.load_post_topics()
    assert snapshot["topics"][0]["status"] == "pending"

    topics.mark_post_topic_status("alpha", "skipped")

    after = topics.load_post_topics()
    assert after["topics"][0]["status"] == "skipped"
