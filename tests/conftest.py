"""Shared pytest fixtures.

These fixtures isolate tests from the real `state/` directory, the network,
the browser harness, and the LLM. Use them whenever a unit/integration test
needs to write state files, call `chat_*` / `run_harness`, or assert on
time-of-day behavior.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BEIJING_TZ = timezone(timedelta(hours=8))


def _retarget_state(module, target: Path, original_root: Path | None = None) -> None:
    """Re-point any module-level *_PATH / *_DIR constant that lives under STATE_DIR.

    If ``original_root`` is provided it overrides the module's own STATE_DIR
    (which lets us retarget modules that imported individual *_PATH constants
    without bringing STATE_DIR with them).
    """
    if original_root is None:
        if not hasattr(module, "STATE_DIR"):
            return
        original_root = Path(module.STATE_DIR)
        module.STATE_DIR = target
    for name in dir(module):
        if not (name.endswith("_PATH") or name.endswith("_DIR")):
            continue
        val = getattr(module, name, None)
        if not isinstance(val, Path):
            continue
        try:
            rel = val.relative_to(original_root)
        except ValueError:
            continue
        setattr(module, name, target / rel)


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Isolate state I/O to a tmp_path so tests can't pollute real state/.

    Re-points STATE_DIR (and every *_PATH/*_DIR derived from it) in src.common
    to a tmp directory. Re-imports modules that cached path constants at import
    time. Yields the tmp state dir.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "logs").mkdir(exist_ok=True)
    (state_dir / "history").mkdir(exist_ok=True)
    (state_dir / "post_history").mkdir(exist_ok=True)
    (state_dir / "revisit_history").mkdir(exist_ok=True)
    (state_dir / "hotspot_history").mkdir(exist_ok=True)
    (state_dir / "screenshots").mkdir(exist_ok=True)

    from src import common as common_mod  # noqa: WPS433 -- runtime import is the point

    # Capture the common.STATE_DIR currently in effect before mutating it,
    # so we can also retarget downstream modules that imported individual
    # *_PATH constants (i.e. modules without their own STATE_DIR attribute).
    pre_common_state_dir = Path(common_mod.STATE_DIR)
    _retarget_state(common_mod, state_dir)

    # Touch downstream modules that import STATE-derived constants at module load.
    for mod_name in (
        "src.persona_store",
        "src.learning.store",
        "src.context_builder",
        "src.topics",
        "src.learning.observe",
        "src.learning.revisit",
        "src.hotspot.store",
        "src.image_search",
        "src.post.handlers_common",
        "src.post.thread",
        "src.post.article",
    ):
        if mod_name in sys.modules:
            _retarget_state(sys.modules[mod_name], state_dir, original_root=pre_common_state_dir)

    yield state_dir


@pytest.fixture
def fake_now(monkeypatch):
    """Pin `datetime.now(tz=Asia/Shanghai)` reads to a fixed instant.

    Usage: `fake_now(year=2026, month=5, day=11, hour=12)` returns a setter;
    or call without args to default to 2026-05-11 12:00:00 CST.
    """
    import src.common as common_mod

    def _set(*, year=2026, month=5, day=11, hour=12, minute=0, second=0):
        fixed = datetime(year, month, day, hour, minute, second, tzinfo=BEIJING_TZ)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return fixed.astimezone().replace(tzinfo=None)
                return fixed.astimezone(tz)

        monkeypatch.setattr("src.common.datetime", _FrozenDatetime, raising=False)
        return fixed

    return _set


@pytest.fixture
def mock_chat(monkeypatch):
    """Replace src.common.chat_json_result / chat_text_result with stubs.

    Returns a dict of MagicMock objects you can configure:
        mock_chat["json"].return_value = {"payload": {...}, "usage": {...}, "cost": {...}}
        mock_chat["text"].return_value = {"text": "...", "usage": {...}, "cost": {...}}
    Default returns are sane empty payloads so tests that don't care won't crash.
    """
    chat_json = MagicMock(return_value={
        "payload": {},
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        "cost": {"total_cost": 0.001},
    })
    chat_text = MagicMock(return_value={
        "text": "",
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        "cost": {"total_cost": 0.001},
    })

    # Patch on src.common (canonical home) and src.llm (extracted module).
    for target in ("src.common.chat_json_result", "src.llm.chat_json_result"):
        try:
            monkeypatch.setattr(target, chat_json, raising=False)
        except Exception:
            pass
    for target in ("src.common.chat_text_result", "src.llm.chat_text_result"):
        try:
            monkeypatch.setattr(target, chat_text, raising=False)
        except Exception:
            pass

    return {"json": chat_json, "text": chat_text}


@pytest.fixture
def mock_run_harness(monkeypatch):
    """Replace src.harness.run_harness with a MagicMock.

    Default returns `'{}'` so JSON-parsing callers get an empty payload.
    Configure per-test: `mock_run_harness.return_value = json.dumps({"ok": True, ...})`.
    """
    harness = MagicMock(return_value=json.dumps({}))
    for target in ("src.common.run_harness", "src.harness.run_harness"):
        try:
            monkeypatch.setattr(target, harness, raising=False)
        except Exception:
            pass
    return harness


@pytest.fixture
def beijing_record_factory():
    """Build a minimal Beijing-time history record for tests."""
    def _make(stamp="20260511_120000", **overrides):
        dt = datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=BEIJING_TZ)
        base = {
            "stamp": stamp,
            "time_beijing": dt.strftime("%Y-%m-%d %H:%M:%S CST"),
            "date_beijing": dt.strftime("%Y-%m-%d"),
            "trigger": "schedule",
            "status": "ok",
            "total_cost_cny": 0.0,
        }
        base.update(overrides)
        return base

    return _make
