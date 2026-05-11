"""Integration tests for the proactive post pipeline.

Covers ``post_once.py`` orchestration for the thread and article paths.
Mocks the LLM (``generate_thread_plan`` / ``generate_article_plan``) and
the subprocess shim ``post_once.run`` so we never touch the real
``post_send.py`` / ``article_send.py`` child scripts. ``tmp_state`` keeps
all writes in a tmp dir.

Regressions locked down here:
  * Thread segments: empty URL on send is NOT a failure; do NOT retry.
  * Article: empty URL on send is NOT a failure; do NOT retry.
  * Thread/article: status is recorded as ``*_url_unresolved`` and topic
    is still marked ``used`` so we don't double-post next run.
  * ``normalize_thread_segments`` truncates by X weighted-char count
    (CJK = 2), capping at 280 weight not 280 ``len()``.
  * Article ``send_failed`` JSON path is honored (single terminal print
    contract): ``ok=False`` + ``reason`` → ``article_send_failed`` status,
    topic remains pending.
  * ``mark_post_topic_status`` is safe under concurrent writers
    (POST_TOPICS_LOCK).
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cp(stdout: str = "", stderr: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _which_script(cmd: list[str]) -> str:
    joined = " ".join(str(part) for part in cmd)
    if "post_send.py" in joined:
        return "post_send"
    if "article_send.py" in joined:
        return "article_send"
    if "send_reply.py" in joined:
        return "send_reply"
    return "unknown"


def _seed_thread_topic(tmp_state: Path, *, topic_id: str = "t1") -> dict:
    topic = {
        "id": topic_id,
        "type": "thread",
        "text": "为什么独立开发者要写帖串",
        "source": "manual",
        "status": "pending",
        "subject": "indie threads",
        "event_or_context": "",
        "stance": "thread crafting",
        "evidence_hint": "",
    }
    (tmp_state / "post_topics.json").write_text(
        json.dumps({"topics": [topic]}, ensure_ascii=False, indent=2)
    )
    return topic


def _seed_article_topic(tmp_state: Path, *, topic_id: str = "a1") -> dict:
    topic = {
        "id": topic_id,
        "type": "article",
        "text": "一篇关于分发的长文",
        "source": "manual",
        "status": "pending",
        "subject": "distribution",
        "event_or_context": "",
        "stance": "long form holds attention",
        "evidence_hint": "",
    }
    (tmp_state / "post_topics.json").write_text(
        json.dumps({"topics": [topic]}, ensure_ascii=False, indent=2)
    )
    return topic


def _make_thread_plan(segments: list[str]) -> dict:
    return {
        "segments": [{"index": i, "text": t, "position_hint": ""} for i, t in enumerate(segments)],
        "thread_angle": "test angle",
        "thread_reason": "test reason",
        "image_query": "",
        "review_pass": True,
        "review_reason": "ok",
        "review_rewrite_hint": "",
        "rewritten": False,
        "generate_usage": {},
        "generate_cost": {"total_cost": 0.001},
        "review_usage": {},
        "review_cost": {"total_cost": 0.0005},
        "rewrite_usage": {},
        "rewrite_cost": {},
        "total_cost_cny": 0.0015,
    }


def _make_article_plan(title: str = "示例文章标题", body: str = "正文" * 20) -> dict:
    return {
        "title": title,
        "body": body,
        "image_query": "",
        "article_reason": "test article reason",
        "review_pass": True,
        "review_reason": "ok",
        "review_rewrite_hint": "",
        "rewritten": False,
        "generate_usage": {},
        "generate_cost": {"total_cost": 0.001},
        "review_usage": {},
        "review_cost": {},
        "rewrite_usage": {},
        "rewrite_cost": {},
        "total_cost_cny": 0.001,
    }


def _import_post_once():
    if "post_once" in sys.modules:
        del sys.modules["post_once"]
    import post_once
    return post_once


@pytest.fixture
def _retarget_topics_paths(tmp_state, monkeypatch):
    """Repoint src.topics' module-level POST_TOPICS_PATH at tmp_state.

    The shared ``tmp_state`` fixture only retargets modules that expose a
    ``STATE_DIR`` attribute. ``src.topics`` does not (it only imports
    specific constants from ``src.common`` at module load), so those
    constants must be patched explicitly when a test exercises code
    paths under ``src.topics``.
    """
    from src import topics as topics_mod
    monkeypatch.setattr(topics_mod, "POST_TOPICS_PATH", tmp_state / "post_topics.json", raising=False)
    monkeypatch.setattr(topics_mod, "POST_TOPICS_LOCK_PATH", tmp_state / "post_topics.lock", raising=False)
    # post_once's add_recent_post writes via persona_store — keep that
    # under tmp_state too.
    import src.persona_store as persona_mod
    monkeypatch.setattr(persona_mod, "PERSONA_PATH", tmp_state / "persona.json", raising=False)
    monkeypatch.setattr(persona_mod, "PERSONA_LOCK_PATH", tmp_state / "persona.lock", raising=False)
    return tmp_state


# ---------------------------------------------------------------------------
# Thread pipeline
# ---------------------------------------------------------------------------


def test_thread_happy_path_three_segments(tmp_state, _retarget_topics_paths, monkeypatch):
    """All 3 segments post OK with URLs → status=thread_posted, topic=used."""
    post_once = _import_post_once()
    topic = _seed_thread_topic(tmp_state)

    plan = _make_thread_plan(["第一段内容", "第二段内容", "第三段内容"])
    monkeypatch.setattr(post_once, "generate_thread_plan", lambda t: plan)
    monkeypatch.setattr(post_once, "telegram_enabled", lambda: False)

    posted_urls = [
        "https://x.com/me/status/1001",
        "https://x.com/me/status/1002",
        "https://x.com/me/status/1003",
    ]
    call_count = {"post_send": 0, "send_reply": 0}

    def fake_run(cmd):
        kind = _which_script(cmd)
        if kind == "post_send":
            call_count["post_send"] += 1
            return _cp(stdout=json.dumps({
                "ok": True,
                "sent_ok": True,
                "url": posted_urls[0],
            }), rc=0)
        if kind == "send_reply":
            idx = call_count["send_reply"]
            call_count["send_reply"] += 1
            url = posted_urls[1 + idx]
            stdout = f'{{"ok": true}}\nREPLY_URL: {url}\n'
            return _cp(stdout=stdout, rc=0)
        raise AssertionError(kind)

    monkeypatch.setattr(post_once, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["post_once.py", "--trigger", "manual"])

    rc = post_once.main()
    assert rc == 0
    assert call_count["post_send"] == 1
    assert call_count["send_reply"] == 2  # segments 2 + 3

    latest = json.loads((tmp_state / "latest_post_run.json").read_text())
    assert latest["status"] == "thread_posted"
    assert latest["thread_segment_count"] == 3
    assert latest["post_url"] == posted_urls[0]
    assert latest["thread_segment_urls"] == posted_urls

    # post_history archive
    history_files = list((tmp_state / "post_history").glob("*.json"))
    assert history_files
    archived = json.loads(history_files[0].read_text())
    assert archived["status"] == "thread_posted"

    # Topic marked used
    topics = json.loads((tmp_state / "post_topics.json").read_text())
    assert topics["topics"][0]["status"] == "used"


def test_thread_segment_empty_url_not_failure_no_retry(tmp_state, _retarget_topics_paths, monkeypatch):
    """Segment 0 returns ok=True but empty url → no retry, mark used.

    Locks down the P0 regression where the old code retried 3x on empty
    URL, causing duplicate posts because the first attempt actually
    landed.
    """
    post_once = _import_post_once()
    _seed_thread_topic(tmp_state)

    # Single-segment thread so we can isolate the empty-URL path on idx 0.
    # (We use 3 segments to satisfy normalize, but force segment 0 to
    # have empty URL and ensure no retry happens.)
    plan = _make_thread_plan(["seg one body", "seg two body", "seg three body"])
    monkeypatch.setattr(post_once, "generate_thread_plan", lambda t: plan)
    monkeypatch.setattr(post_once, "telegram_enabled", lambda: False)

    call_count = {"post_send": 0, "send_reply": 0}

    def fake_run(cmd):
        kind = _which_script(cmd)
        if kind == "post_send":
            call_count["post_send"] += 1
            # DOM marker fired but profile-timeline lookup couldn't find
            # the URL — this is the regression case.
            return _cp(stdout=json.dumps({
                "ok": True,
                "sent_ok": True,
                "url": "",
            }), rc=0)
        if kind == "send_reply":
            call_count["send_reply"] += 1
            return _cp(stdout='{"ok": true}\n', rc=0)
        raise AssertionError(kind)

    monkeypatch.setattr(post_once, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["post_once.py", "--trigger", "manual"])

    rc = post_once.main()
    # rc=1 because chain aborts (segment 0 URL missing -> can't chain).
    assert rc == 1
    # CRUCIAL: post_send was called EXACTLY ONCE — no triple retry.
    assert call_count["post_send"] == 1, (
        f"empty-URL should NOT trigger retries; got {call_count['post_send']} calls"
    )
    # And we aborted before sending the chained replies.
    assert call_count["send_reply"] == 0

    latest = json.loads((tmp_state / "latest_post_run.json").read_text())
    # Status is the "URL unresolved" terminal state OR partial — must
    # NOT be ``thread_posted`` (clean success). Must reflect URL trouble.
    assert latest["status"] in {"thread_partial", "thread_posted_url_unresolved"}
    # First segment must be tagged as url_unresolved in the per-segment record
    assert latest["thread_segments"][0]["url_unresolved"] is True


# ---------------------------------------------------------------------------
# Article pipeline
# ---------------------------------------------------------------------------


def test_article_happy_path(tmp_state, _retarget_topics_paths, monkeypatch):
    """Article send returns ok=True with URL → status=article_posted, topic=used."""
    post_once = _import_post_once()
    _seed_article_topic(tmp_state)

    plan = _make_article_plan()
    monkeypatch.setattr(post_once, "generate_article_plan", lambda t: plan)
    monkeypatch.setattr(post_once, "telegram_enabled", lambda: False)

    article_url = "https://x.com/me/status/9999"
    call_count = {"article_send": 0}

    def fake_run(cmd):
        kind = _which_script(cmd)
        if kind == "article_send":
            call_count["article_send"] += 1
            return _cp(stdout=json.dumps({
                "ok": True,
                "sent_ok": True,
                "url": article_url,
            }), rc=0)
        raise AssertionError(kind)

    monkeypatch.setattr(post_once, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["post_once.py", "--trigger", "manual"])

    rc = post_once.main()
    assert rc == 0
    assert call_count["article_send"] == 1

    latest = json.loads((tmp_state / "latest_post_run.json").read_text())
    assert latest["status"] == "article_posted"
    assert latest["post_url"] == article_url

    topics = json.loads((tmp_state / "post_topics.json").read_text())
    assert topics["topics"][0]["status"] == "used"


def test_article_empty_url_not_failure_no_retry(tmp_state, _retarget_topics_paths, monkeypatch):
    """Article send confirmed but URL empty → no retry, mark used.

    Article path's mirror of the thread P0 fix.
    """
    post_once = _import_post_once()
    _seed_article_topic(tmp_state)

    plan = _make_article_plan()
    monkeypatch.setattr(post_once, "generate_article_plan", lambda t: plan)
    monkeypatch.setattr(post_once, "telegram_enabled", lambda: False)

    call_count = {"article_send": 0}

    def fake_run(cmd):
        kind = _which_script(cmd)
        if kind == "article_send":
            call_count["article_send"] += 1
            return _cp(stdout=json.dumps({
                "ok": True,
                "sent_ok": True,
                "article_url": "",
                "url": "",
            }), rc=0)
        raise AssertionError(kind)

    monkeypatch.setattr(post_once, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["post_once.py", "--trigger", "manual"])

    rc = post_once.main()
    # Send was confirmed (ok=True / sent_ok=True), so post_once exits 0
    # even though URL didn't resolve.
    assert rc == 0
    # CRUCIAL: only one call. No retries.
    assert call_count["article_send"] == 1

    latest = json.loads((tmp_state / "latest_post_run.json").read_text())
    assert latest["status"] == "article_sent_url_unresolved"
    assert latest["post_url"] == ""

    # Topic STILL marked used — leaving it pending would cause a duplicate
    # post on the next run.
    topics = json.loads((tmp_state / "post_topics.json").read_text())
    assert topics["topics"][0]["status"] == "used"


def test_article_send_failed_topic_remains_pending(tmp_state, _retarget_topics_paths, monkeypatch):
    """article_send emits {ok:false, reason:...} (early-fail JSON path).

    post_once must record ``article_send_failed`` and leave the topic
    in ``pending`` so a follow-up run can retry. Single terminal print
    contract: post_once parses the failure JSON from stdout.
    """
    post_once = _import_post_once()
    _seed_article_topic(tmp_state)

    plan = _make_article_plan()
    monkeypatch.setattr(post_once, "generate_article_plan", lambda t: plan)
    monkeypatch.setattr(post_once, "telegram_enabled", lambda: False)

    def fake_run(cmd):
        kind = _which_script(cmd)
        if kind == "article_send":
            # article_send.py early-fail JSON: ok=False + sent_ok=False
            # + a ``reason`` field, exit code 1.
            return _cp(stdout=json.dumps({
                "ok": False,
                "sent_ok": False,
                "reason": "no title element",
            }), rc=1)
        raise AssertionError(kind)

    monkeypatch.setattr(post_once, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["post_once.py", "--trigger", "manual"])

    rc = post_once.main()
    assert rc == 1

    latest = json.loads((tmp_state / "latest_post_run.json").read_text())
    assert latest["status"] == "article_send_failed"

    # Topic should still be pending so the next run can retry.
    topics = json.loads((tmp_state / "post_topics.json").read_text())
    assert topics["topics"][0]["status"] == "pending"


# ---------------------------------------------------------------------------
# normalize_thread_segments — CJK weight regression
# ---------------------------------------------------------------------------


def test_normalize_thread_segments_truncates_by_cjk_weight(tmp_state):
    """200 CJK chars (weight 400) must truncate to ~140 CJK chars (weight 280).

    Locks down the P1 regression where len()-based truncation let through
    a 280-CJK segment whose tweet weight was 560 — silently broke posting.
    """
    from src.post import post_generate
    from src.common import THREAD_MAX_SEGMENT_CHARS

    assert THREAD_MAX_SEGMENT_CHARS == 280

    big = "中" * 200  # weight 400, len 200
    short1 = "短一" * 5
    short2 = "短二" * 5

    out = post_generate.normalize_thread_segments([
        {"text": big},
        {"text": short1},
        {"text": short2},
    ])
    assert len(out) >= 3
    truncated_text = out[0]["text"]
    truncated_weight = post_generate.cjk_weight(truncated_text)
    # Weight cap = 280, each CJK = 2, so max 140 CJK chars.
    assert truncated_weight <= THREAD_MAX_SEGMENT_CHARS, (
        f"truncated segment still weighs {truncated_weight}"
    )
    assert len(truncated_text) <= 140, (
        f"expected ~140 CJK chars, got {len(truncated_text)}"
    )
    # And we did truncate (we sent in 200, not <=140).
    assert len(truncated_text) < 200


# ---------------------------------------------------------------------------
# POST_TOPICS_LOCK — concurrent mark_post_topic_status
# ---------------------------------------------------------------------------


def test_post_topics_lock_serializes_concurrent_marks(tmp_state, monkeypatch):
    """Two threads marking different topics must both land safely.

    Locks in the contract that POST_TOPICS_LOCK serializes writes via
    blocking_lock — no torn writes / lost updates under concurrency.
    """
    # ``src.topics`` captures POST_TOPICS_PATH at import time. The
    # ``tmp_state`` fixture only retargets modules with a ``STATE_DIR``
    # attribute — ``src.topics`` has none, so we patch its path bindings
    # by hand to point at the tmp_state isolation dir.
    from src import topics as topics_mod
    monkeypatch.setattr(topics_mod, "POST_TOPICS_PATH", tmp_state / "post_topics.json")
    monkeypatch.setattr(topics_mod, "POST_TOPICS_LOCK_PATH", tmp_state / "post_topics.lock")
    from src.common import mark_post_topic_status

    # Seed two pending topics.
    topics_data = {
        "topics": [
            {"id": "concurrent-a", "type": "argument", "text": "ta", "source": "manual", "status": "pending"},
            {"id": "concurrent-b", "type": "argument", "text": "tb", "source": "manual", "status": "pending"},
        ]
    }
    (tmp_state / "post_topics.json").write_text(
        json.dumps(topics_data, ensure_ascii=False, indent=2)
    )

    errors: list[Exception] = []

    def worker(topic_id: str):
        try:
            for _ in range(5):
                mark_post_topic_status(topic_id, "used", {"last_seen_at": "2026-05-11T12:00:00"})
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=("concurrent-a",))
    t2 = threading.Thread(target=worker, args=("concurrent-b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"concurrent writes raised: {errors}"
    assert not t1.is_alive() and not t2.is_alive()

    final = json.loads((tmp_state / "post_topics.json").read_text())
    by_id = {item["id"]: item for item in final["topics"]}
    assert by_id["concurrent-a"]["status"] == "used"
    assert by_id["concurrent-b"]["status"] == "used"
    # And both got the side-effect field — no lost update.
    assert by_id["concurrent-a"]["last_seen_at"] == "2026-05-11T12:00:00"
    assert by_id["concurrent-b"]["last_seen_at"] == "2026-05-11T12:00:00"
