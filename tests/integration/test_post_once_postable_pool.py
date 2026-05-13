"""Integration: post_once.main() picks from postable_pool and marks correctly.

These tests exercise the end-to-end wiring between post_once,
postable_pool, the hotspot store, the manual topic queue, and the
selector LLM call. The browser-harness subprocess (post_send.py) and
all LLM calls are mocked — only the selection / persistence /
mark-posted logic is real.

Regressions locked down:
  * hotspot row → posted_at is set ONLY on successful send.
  * dry-run does NOT invoke post_send and does NOT mark posted.
  * send failure leaves posted_at empty so a future run can retry.
  * manual topics take priority over hotspot pool, and hotspot row is
    untouched when a manual topic is consumed.
  * When the LLM judges all hotspot candidates duplicate (best_index=-1),
    post_once falls back to auto_topic and does NOT mark the hotspot row.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def post_once_env(tmp_state, monkeypatch):
    """Retarget downstream modules that captured *_PATH constants at import.

    ``tmp_state`` retargets ``src.common`` and a handful of modules listed in
    conftest. The pool / topics layer captures path constants by name at
    import time, so we must explicitly rebind them onto the tmp dir or the
    post_once flow will read/write the real ``state/`` directory.

    We also force a fresh re-import of ``post_once`` so its module-level
    ``from src.common import ...`` and ``from src.post.handlers_common import ...``
    bindings pick up the retargeted paths.
    """
    # Repoint src.topics path bindings.
    from src import topics as topics_mod
    monkeypatch.setattr(topics_mod, "POST_TOPICS_PATH",
                        tmp_state / "post_topics.json", raising=False)
    monkeypatch.setattr(topics_mod, "POST_TOPICS_LOCK_PATH",
                        tmp_state / "post_topics.lock", raising=False)

    # Repoint src.postable_pool lock binding + reset the module-level
    # _migration_done flag (it would otherwise leak across tests in the
    # same process).
    import src.postable_pool as pool_mod
    importlib.reload(pool_mod)
    monkeypatch.setattr(pool_mod, "POST_TOPICS_LOCK_PATH",
                        tmp_state / "post_topics.lock", raising=False)
    pool_mod._migration_done = False

    # Repoint src.persona_store paths so add_recent_post writes don't
    # touch real state.
    import src.persona_store as persona_mod
    monkeypatch.setattr(persona_mod, "PERSONA_PATH",
                        tmp_state / "persona.json", raising=False)
    monkeypatch.setattr(persona_mod, "PERSONA_LOCK_PATH",
                        tmp_state / "persona.lock", raising=False)

    # Force fresh re-import of post_once so its top-level
    # ``from src.common import LATEST_POST_RUN_PATH, ...`` bindings
    # land on the retargeted paths.
    if "post_once" in sys.modules:
        del sys.modules["post_once"]
    import post_once
    return post_once


def _stub_run_success(monkeypatch, post_once_mod):
    """Stub the subprocess wrapper to a success result.

    Note: post_once.py does ``from src.post.handlers_common import ... run``,
    so the binding actually used is ``post_once.run`` — patching
    ``handlers_common.run`` alone would NOT intercept the call. We patch
    on the post_once module object.
    """
    fake_run = MagicMock(return_value=types.SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"url": "https://x.com/me/status/12345"}) + "\n",
        stderr="",
    ))
    monkeypatch.setattr(post_once_mod, "run", fake_run)
    return fake_run


def _stub_post_plan(monkeypatch, post_once_mod):
    """Stub generate_post_plan to a deterministic minimal plan.

    Same binding rationale as ``_stub_run_success``: post_once imports the
    symbol into its own namespace, so patch on the post_once module.
    """
    def _plan(_topic):
        return {
            "candidates": [{"text": "hello world", "image_query": ""}],
            "selected_index": 0,
            "selected_candidate": {"text": "hello world", "image_query": ""},
            "best_candidate": {"text": "hello world", "image_query": ""},
            "selected_reason": "stub",
            "review_pass": True,
            "review_reason": "ok",
            "review_rewrite_hint": "",
            "rewritten": False,
            "candidate_cost": {},
            "candidate_usage": {},
            "rerank_cost": {},
            "rerank_usage": {},
            "review_cost": {},
            "review_usage": {},
            "rewrite_cost": {},
            "rewrite_usage": {},
            "total_cost_cny": 0.0,
        }

    monkeypatch.setattr(post_once_mod, "generate_post_plan", _plan)


def _silence_side_effects(monkeypatch, post_once_mod):
    """Silence telegram + recent-post side effects on post_once."""
    monkeypatch.setattr(post_once_mod, "notify_telegram", lambda *a, **k: None)
    monkeypatch.setattr(post_once_mod, "add_recent_post", lambda *a, **k: None)


def _hotspot_row(**kw):
    """Seed a hotspot row directly into the SQLite store.

    Uses src.hotspot.store.insert_hotspot so we don't depend on the exact
    schema layout. tmp_state already retargets src.hotspot.store's
    HOTSPOT_STORE_PATH to the tmp dir.
    """
    from src.hotspot import store
    store.insert_hotspot(
        source=kw.get("source", "hn"),
        hotspot_id=kw.get("hotspot_id", "1"),
        title=kw.get("title", "Some hot story"),
        url=kw.get("url", "https://hn.x/1"),
        hn_score=kw.get("hn_score", 100),
        hn_descendants=kw.get("hn_descendants", 50),
        relevance_score=kw.get("relevance_score", 4),
        relevance_reason=kw.get("relevance_reason", "AI agent 重大更新"),
        angle=kw.get("angle", "工作流变化"),
        cn_summary=kw.get("cn_summary", "AI agent 框架更新"),
    )


def _read_posted_at(hotspot_id_key: str = "hn:1") -> str:
    """Return the posted_at value for the seeded hotspot row."""
    from src.hotspot import store
    with sqlite3.connect(str(store.HOTSPOT_STORE_PATH)) as conn:
        row = conn.execute(
            "SELECT posted_at FROM hotspots WHERE id = ?",
            (hotspot_id_key,),
        ).fetchone()
    assert row is not None, f"hotspot row {hotspot_id_key!r} missing"
    return row[0] or ""


def _stub_selector_pick(monkeypatch, *, best_index: int, reason: str = "ok"):
    """Patch the selector's LLM call to return a deterministic best_index."""
    from src.hotspot import selector
    monkeypatch.setattr(
        selector, "chat_json_result",
        lambda *a, **k: {
            "payload": {"best_index": best_index, "reason": reason},
            "cost": {"total_cost": 0.0},
            "usage": {},
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_hotspot_path_marks_posted_on_success(post_once_env, monkeypatch):
    """Happy path: hotspot row picked, send returns 0 → posted_at is set."""
    _hotspot_row()
    _stub_post_plan(monkeypatch, post_once_env)
    fake_run = _stub_run_success(monkeypatch, post_once_env)
    _stub_selector_pick(monkeypatch, best_index=0)
    _silence_side_effects(monkeypatch, post_once_env)

    monkeypatch.setattr(sys, "argv", ["post_once.py", "--trigger", "test"])
    rc = post_once_env.main()
    assert rc == 0

    # Subprocess was invoked once with the post_send script.
    assert fake_run.call_count == 1
    sent_cmd = fake_run.call_args.args[0]
    joined = " ".join(str(p) for p in sent_cmd)
    assert "post_send.py" in joined

    assert _read_posted_at() != ""


def test_dry_run_does_not_mark_posted(post_once_env, monkeypatch):
    """Dry-run path: send NOT invoked, posted_at stays empty."""
    _hotspot_row()
    _stub_post_plan(monkeypatch, post_once_env)
    _stub_selector_pick(monkeypatch, best_index=0)
    _silence_side_effects(monkeypatch, post_once_env)
    # Wire a tripwire so any subprocess call would crash the test.
    monkeypatch.setattr(
        post_once_env, "run",
        MagicMock(side_effect=AssertionError("dry-run must not call send")),
    )

    monkeypatch.setattr(sys, "argv",
                        ["post_once.py", "--dry-run", "--trigger", "test"])
    rc = post_once_env.main()
    assert rc == 0

    assert _read_posted_at() == ""


def test_send_failure_does_not_mark_posted(post_once_env, monkeypatch):
    """Transient send failure leaves posted_at empty so a future run can retry."""
    _hotspot_row()
    _stub_post_plan(monkeypatch, post_once_env)
    _stub_selector_pick(monkeypatch, best_index=0)
    _silence_side_effects(monkeypatch, post_once_env)
    monkeypatch.setattr(
        post_once_env, "run",
        MagicMock(return_value=types.SimpleNamespace(
            returncode=2, stdout="", stderr="boom",
        )),
    )

    monkeypatch.setattr(sys, "argv", ["post_once.py", "--trigger", "test"])
    rc = post_once_env.main()
    assert rc != 0

    assert _read_posted_at() == ""


def test_manual_takes_priority_over_hotspot(post_once_env, monkeypatch, tmp_state):
    """Manual queue entry beats hotspot pool: manual marked used, hotspot untouched."""
    _hotspot_row()
    # Seed a pending manual topic.
    from src.common import POST_TOPICS_PATH
    POST_TOPICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    POST_TOPICS_PATH.write_text(
        json.dumps({
            "topics": [{
                "id": "manual-1",
                "source": "manual",
                "status": "pending",
                "text": "manual override",
                "type": "argument",
            }],
        }),
        encoding="utf-8",
    )

    _stub_post_plan(monkeypatch, post_once_env)
    _stub_run_success(monkeypatch, post_once_env)
    _silence_side_effects(monkeypatch, post_once_env)

    # Tripwire: hotspot selector must not be reached when manual is pending.
    from src.hotspot import selector
    monkeypatch.setattr(
        selector, "pick_best",
        MagicMock(side_effect=AssertionError("manual must short-circuit hotspot")),
    )

    monkeypatch.setattr(sys, "argv", ["post_once.py", "--trigger", "test"])
    rc = post_once_env.main()
    assert rc == 0

    # Manual topic consumed.
    after = json.loads(POST_TOPICS_PATH.read_text(encoding="utf-8"))
    assert after["topics"][0]["status"] == "used"
    # Hotspot row left untouched.
    assert _read_posted_at() == ""


def test_all_duplicate_falls_through_to_auto(post_once_env, monkeypatch):
    """Selector says best_index=-1 → post_once falls back to auto_topic.

    Hotspot row stays unmarked because the selector returned None and
    postable_pool never produced a hotspot topic for post_once to consume.
    """
    _hotspot_row()
    _stub_selector_pick(monkeypatch, best_index=-1, reason="all dup")

    # Auto generator returns a deterministic topic.
    auto_called = MagicMock(return_value={
        "id": "auto-stub",
        "type": "argument",
        "text": "fallback content",
        "source": "auto",
        "status": "pending",
        "subject": "",
        "event_or_context": "",
        "stance": "fallback content",
        "evidence_hint": "",
    })
    monkeypatch.setattr(post_once_env, "generate_auto_topic", auto_called)

    _stub_post_plan(monkeypatch, post_once_env)
    _stub_run_success(monkeypatch, post_once_env)
    _silence_side_effects(monkeypatch, post_once_env)

    monkeypatch.setattr(sys, "argv", ["post_once.py", "--trigger", "test"])
    rc = post_once_env.main()
    assert rc == 0

    # Auto generator was used.
    auto_called.assert_called_once()
    # Hotspot row NOT marked posted (the auto topic was sent, not the hotspot).
    assert _read_posted_at() == ""
