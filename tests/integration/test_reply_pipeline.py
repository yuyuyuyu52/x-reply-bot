"""Integration tests for the reply pipeline.

Covers ``run_once.py`` orchestration plus per-step modules. Mocks
``chat_json_result`` (selection + reply generation) and ``run_harness``
(prepare scrape + send) so the test runs fully offline.

Also locks down several P0/P1 regression fixes:
  * Consistency guard between ``selected_post.json`` and the generated reply.
  * ``like_block`` JS template must NOT leak ``{{``/``}}`` (f-string bug).
  * ``like_block`` indentation must land for action=reply/quote/repost.

Children scripts (``prepare_post.py`` / ``generate_reply.py`` /
``send_reply.py``) are spawned via ``subprocess.run`` in production. We
patch ``run_once.run`` (the thin subprocess wrapper) with a router that
returns canned ``CompletedProcess`` objects keyed off the script being
launched. Each branch also writes whatever state file the real child
would have written (``state/selected_post.json``).
"""
from __future__ import annotations

import json
import subprocess
import sys
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
    """Return a short tag for which child script ``cmd`` invokes."""
    joined = " ".join(str(part) for part in cmd)
    if "prepare_post.py" in joined:
        return "prepare"
    if "generate_reply.py" in joined:
        return "generate"
    if "send_reply.py" in joined:
        return "send"
    return "unknown"


def _write_selected(state_dir: Path, *, url: str, selection_id: str, text: str = "demo post text"):
    selected = {
        "ok": True,
        "url": url,
        "selection_id": selection_id,
        "main_post_text": text,
        "selector_reason": "looks interesting",
        "selection_model": "qwen3.5-flash",
        "selection_usage": {"prompt_tokens": 200, "completion_tokens": 80},
        "selection_cost": {"total_cost": 0.0012, "model": "qwen3.5-flash"},
    }
    (state_dir / "selected_post.json").write_text(
        json.dumps(selected, ensure_ascii=False, indent=2)
    )
    return selected


# ---------------------------------------------------------------------------
# run_once.main() — full pipeline
# ---------------------------------------------------------------------------


def _import_run_once():
    if "run_once" in sys.modules:
        del sys.modules["run_once"]
    import run_once
    return run_once


def test_happy_path_writes_history_and_log(tmp_state, mock_chat, monkeypatch):
    """Happy path: prepare→generate→send all succeed; history + log written."""
    run_once = _import_run_once()

    selected_url = "https://x.com/alice/status/1234567890"
    selection_id = "20260511_120000_000001"

    def fake_run(cmd):
        kind = _which_script(cmd)
        if kind == "prepare":
            _write_selected(tmp_state, url=selected_url, selection_id=selection_id)
            return _cp(stdout="prepared\n", rc=0)
        if kind == "generate":
            payload = {
                "reply": "this is a thoughtful reply",
                "action": "reply",
                "reason": "agrees with the framing",
                "like": False,
                "source_post_url": selected_url,
                "selection_id": selection_id,
                "usage": {"prompt_tokens": 300, "completion_tokens": 100},
                "cost": {"total_cost": 0.0025, "model": "qwen3.5-flash"},
            }
            return _cp(stdout=json.dumps(payload), rc=0)
        if kind == "send":
            # The real send_reply.py would update state/replied_posts.json
            # after a confirmed send — mimic that side-effect here so the
            # post-run assertions can validate it.
            replied_path = tmp_state / "replied_posts.json"
            current = {"posts": []}
            if replied_path.exists():
                current = json.loads(replied_path.read_text())
            current.setdefault("posts", []).append(selected_url)
            replied_path.write_text(json.dumps(current, ensure_ascii=False, indent=2))
            stdout = json.dumps({
                "ok": True,
                "url": selected_url,
                "action": "reply",
                "reply": "this is a thoughtful reply",
            })
            return _cp(stdout=stdout, rc=0)
        raise AssertionError(f"unexpected child invocation: {cmd}")

    monkeypatch.setattr(run_once, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_once.py", "--trigger", "schedule"])
    monkeypatch.setattr(run_once, "telegram_enabled", lambda: False)

    rc = run_once.main()
    assert rc == 0

    # latest_run.json + history archive both written
    latest = json.loads((tmp_state / "latest_run.json").read_text())
    assert latest["action"] == "reply"
    assert latest["post_url"] == selected_url
    assert latest["reply_text"] == "this is a thoughtful reply"
    assert latest["send_returncode"] == 0
    assert "time_beijing" in latest
    assert "date_beijing" in latest
    # cost was summed from selection + reply
    assert latest["total_cost_cny"] == pytest.approx(0.0012 + 0.0025, abs=1e-9)

    history_files = list((tmp_state / "history").glob("*.json"))
    assert history_files, "history archive should be written"
    archived = json.loads(history_files[0].read_text())
    assert archived["post_url"] == selected_url
    assert "time_beijing" in archived
    assert "date_beijing" in archived

    # run_log.json got an ``ok`` status entry
    log_entries = json.loads((tmp_state / "run_log.json").read_text())
    assert any(e.get("status") == "success" for e in log_entries)

    # send_reply side-effect: replied_posts.json contains the URL
    replied = json.loads((tmp_state / "replied_posts.json").read_text())
    assert selected_url in replied.get("posts", [])


def test_skip_path_ai_rejected_all_candidates(tmp_state, monkeypatch):
    """prepare returns ``ai_rejected_all_candidates`` → log skipped, rc=0."""
    run_once = _import_run_once()

    def fake_run(cmd):
        kind = _which_script(cmd)
        if kind == "prepare":
            payload = {
                "ok": False,
                "reason": "ai_rejected_all_candidates",
                "selector_reason": "nothing worth replying to",
                "selection_model": "qwen3.5-flash",
                "selection_usage": {"prompt_tokens": 100, "completion_tokens": 30},
                "selection_cost": {"total_cost": 0.0006},
            }
            (tmp_state / "selected_post.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2)
            )
            return _cp(stdout=json.dumps(payload), rc=1)
        raise AssertionError(f"should not invoke {kind} on the skip path")

    monkeypatch.setattr(run_once, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_once.py", "--trigger", "schedule"])
    monkeypatch.setattr(run_once, "telegram_enabled", lambda: False)

    rc = run_once.main()
    assert rc == 0  # skipped is NOT failure

    # run_log.json has an entry with status="skipped"
    log_entries = json.loads((tmp_state / "run_log.json").read_text())
    assert any(
        e.get("status") == "skipped" and e.get("reason") == "ai_rejected_all_candidates"
        for e in log_entries
    ), log_entries
    # No history file should have been written (we never got to send)
    assert not list((tmp_state / "history").glob("*.json"))


def test_consistency_guard_blocks_send_on_mismatch(tmp_state, monkeypatch):
    """Stale selected_post vs. reply → refuse to send + audit log + rc=1."""
    run_once = _import_run_once()

    selected_url_A = "https://x.com/alice/status/AAA"
    selected_id_A = "X1"

    # send must never be invoked
    send_was_called = {"hit": False}

    def fake_run(cmd):
        kind = _which_script(cmd)
        if kind == "prepare":
            _write_selected(tmp_state, url=selected_url_A, selection_id=selected_id_A)
            return _cp(rc=0)
        if kind == "generate":
            payload = {
                "reply": "wrong-state reply",
                "action": "reply",
                "reason": "hijack",
                "like": False,
                "source_post_url": "https://x.com/bob/status/BBB",
                "selection_id": "X2",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "cost": {"total_cost": 0.0},
            }
            return _cp(stdout=json.dumps(payload), rc=0)
        if kind == "send":
            send_was_called["hit"] = True
            return _cp(rc=0)
        raise AssertionError(kind)

    monkeypatch.setattr(run_once, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_once.py", "--trigger", "manual"])
    monkeypatch.setattr(run_once, "telegram_enabled", lambda: False)

    rc = run_once.main()
    assert rc == 1
    assert send_was_called["hit"] is False, "send_reply should NOT run on mismatch"

    log_entries = json.loads((tmp_state / "run_log.json").read_text())
    stale = [e for e in log_entries if e.get("status") == "stale_state"]
    assert stale, log_entries
    audit = stale[-1]
    assert audit["reason"] == "reply_selection_mismatch"
    assert audit["selected_url"] == selected_url_A
    assert audit["reply_source_url"] == "https://x.com/bob/status/BBB"
    assert audit["selected_selection_id"] == selected_id_A
    assert audit["reply_selection_id"] == "X2"


def test_consistency_guard_blocks_empty_selection_id(tmp_state, monkeypatch):
    """Both selection_ids empty must still fail the guard (post-fix behavior)."""
    run_once = _import_run_once()
    send_was_called = {"hit": False}

    def fake_run(cmd):
        kind = _which_script(cmd)
        if kind == "prepare":
            # write selected with empty selection_id
            _write_selected(tmp_state, url="https://x.com/a/status/1", selection_id="")
            return _cp(rc=0)
        if kind == "generate":
            payload = {
                "reply": "ok",
                "action": "reply",
                "reason": "",
                "source_post_url": "https://x.com/a/status/1",
                "selection_id": "",
                "usage": {},
                "cost": {"total_cost": 0.0},
            }
            return _cp(stdout=json.dumps(payload), rc=0)
        if kind == "send":
            send_was_called["hit"] = True
            return _cp(rc=0)
        raise AssertionError(kind)

    monkeypatch.setattr(run_once, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_once.py", "--trigger", "manual"])
    monkeypatch.setattr(run_once, "telegram_enabled", lambda: False)

    rc = run_once.main()
    assert rc == 1
    assert send_was_called["hit"] is False

    log_entries = json.loads((tmp_state / "run_log.json").read_text())
    assert any(e.get("status") == "stale_state" for e in log_entries)


# ---------------------------------------------------------------------------
# send_reply.py — like_block / action interactions
# ---------------------------------------------------------------------------


def _run_send_reply(monkeypatch, *, action: str, like: bool, harness_mock):
    """Drive ``src.reply.send_reply.main`` once with the given args + return
    the rendered harness code from ``harness_mock.call_args[0][0]``.
    """
    import src.reply.send_reply as send_mod

    # Patch the module-level binding directly — the module did
    # ``from src.common import run_harness`` so it has its own reference
    # that the mock_run_harness fixture's monkeypatch on src.common /
    # src.harness can't reach.
    monkeypatch.setattr(send_mod, "run_harness", harness_mock)

    argv = [
        "send_reply.py",
        "--url", "https://x.com/me/status/1",
        "--reply", "hi there",
        "--action", action,
    ]
    if like:
        argv.append("--like")
    monkeypatch.setattr(sys, "argv", argv)
    rc = send_mod.main()
    return rc


def test_like_block_no_double_braces_regression(tmp_state, monkeypatch):
    """Rendered harness code must not contain ``{{`` or ``}}`` substrings.

    The historical regression: the ``like_block`` was originally embedded
    inside a parent f-string but accidentally used doubled braces (``{{``
    / ``}}``), so JS bracketing leaked literal ``{{`` into the harness
    payload and failed to parse. Locks down the fix.
    """
    harness_mock = mock.MagicMock(return_value=json.dumps({"ok": True}))
    rc = _run_send_reply(monkeypatch, action="reply", like=True, harness_mock=harness_mock)
    # rc may be 0 or 1 depending on whether ``ok: true`` is in stdout —
    # we don't care about the exit code here, only the rendered code.
    assert harness_mock.call_count >= 1
    rendered_code = harness_mock.call_args[0][0]

    # Locate the like block in the rendered output.
    assert "like_result = js(" in rendered_code, "like block should be present"
    # Find the like region: start at "like_result = js(" up through the closing JS marker.
    start = rendered_code.index("like_result = js(")
    # End at a reasonable downstream marker (the like_ok check).
    end_marker = "like_ok = like_result.get"
    assert end_marker in rendered_code, "like check should be present"
    end = rendered_code.index(end_marker)
    like_region = rendered_code[start:end]

    # Crucial regression assertion: no doubled-brace leakage inside the
    # like region. Single ``{`` / ``}`` from real JS are fine.
    assert "{{" not in like_region, (
        f"{{{{ leaked into rendered like block:\n{like_region}"
    )
    assert "}}" not in like_region, (
        f"}}}} leaked into rendered like block:\n{like_region}"
    )


@pytest.mark.parametrize("action", ["reply", "quote", "repost"])
def test_like_block_executes_for_all_actions(tmp_state, monkeypatch, action):
    """``--like`` must render the like block for reply/quote/repost.

    Indentation regression: the like block previously sat under one of
    the action branches, so e.g. action=repost skipped the like. After
    the fix it lives at the outer level and fires for all three actions.
    """
    harness_mock = mock.MagicMock(return_value=json.dumps({"ok": True}))
    _run_send_reply(monkeypatch, action=action, like=True, harness_mock=harness_mock)

    rendered = harness_mock.call_args[0][0]
    assert "like_result = js(" in rendered, (
        f"--like did not produce a like_result block for action={action}"
    )
    # And confirm the like check at the tail also got included.
    assert "like_ok = like_result.get" in rendered
