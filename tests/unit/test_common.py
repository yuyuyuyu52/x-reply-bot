"""Unit tests for `src.common` helpers and constants.

Pins down lock-file behavior (no-truncate, non-blocking contention,
blocking retries), language-filter regressions, .env loading, status-URL
canonicalization, state-dir bootstrapping, history path sanitization,
and the 200-entry run-log cap. Cost-estimation already lives in
`test_estimate_cost.py` — don't duplicate it here.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("X_REPLY_MODEL", "test-default")

import src.common as common  # noqa: E402


# ---------------------------------------------------------------------------
# exclusive_lock / blocking_lock
# ---------------------------------------------------------------------------


def test_exclusive_lock_does_not_truncate(tmp_state):
    """Regression: the lock file's pre-existing bytes must survive lock acquisition.

    The fix moved from `path.open("w")` (which truncates) to
    `os.O_RDWR | os.O_CREAT` (no truncate). If a future refactor reverts
    that, this test fails.
    """
    lock_path = tmp_state / "test.lock"
    lock_path.write_bytes(b"important-holder-metadata")

    with common.exclusive_lock(lock_path):
        # Inside the critical section the bytes should still be there.
        assert lock_path.read_bytes() == b"important-holder-metadata"

    # And after release too.
    assert lock_path.read_bytes() == b"important-holder-metadata"


def test_exclusive_lock_non_blocking_contention(tmp_state):
    """Second acquirer should raise BlockingIOError immediately (LOCK_NB)."""
    lock_path = tmp_state / "test.lock"

    with common.exclusive_lock(lock_path):
        with pytest.raises(BlockingIOError):
            with common.exclusive_lock(lock_path):
                pass  # pragma: no cover -- not reachable


def test_exclusive_lock_release_allows_reacquisition(tmp_state):
    """After the with-block exits the lock must be free for the next caller."""
    lock_path = tmp_state / "test.lock"

    with common.exclusive_lock(lock_path):
        pass
    # Should not raise.
    with common.exclusive_lock(lock_path):
        pass


def test_blocking_lock_retries_until_holder_releases(tmp_state):
    """Worker thread must block on flock until the main thread releases."""
    lock_path = tmp_state / "test.lock"
    worker_acquired = threading.Event()
    holder_released = threading.Event()
    timing: dict[str, float] = {}

    def worker() -> None:
        with common.blocking_lock(lock_path):
            timing["acquired_at"] = time.monotonic()
            worker_acquired.set()

    with common.blocking_lock(lock_path):
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        # Give the worker time to start and block on flock.
        time.sleep(0.2)
        assert not worker_acquired.is_set(), "worker should still be blocked"
        timing["release_at"] = time.monotonic()
        holder_released.set()

    # Worker should acquire promptly after release.
    assert worker_acquired.wait(timeout=2.0), "worker never acquired the lock"
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    # Ordering check: worker acquired after the holder released.
    assert timing["acquired_at"] >= timing["release_at"]


# ---------------------------------------------------------------------------
# looks_supported_language
# ---------------------------------------------------------------------------


class LooksSupportedLanguageTests(unittest.TestCase):
    def test_numeric_finance_text_is_supported(self):
        # Regression: this used to return False because digits weren't counted
        # as meaningful content. The fix added digits to the numerator.
        self.assertTrue(common.looks_supported_language("$500 → $1200"))

    def test_pure_chinese(self):
        self.assertTrue(common.looks_supported_language("今天天气很好，我要去散步。"))

    def test_pure_english(self):
        self.assertTrue(common.looks_supported_language("Hello world, this is a test."))

    def test_mixed_zh_en(self):
        self.assertTrue(common.looks_supported_language("今天 deploy 了新 feature 到 prod"))

    def test_emoji_only_unsupported(self):
        self.assertFalse(common.looks_supported_language("🔥🚀💯😀"))

    def test_all_spaces_unsupported(self):
        self.assertFalse(common.looks_supported_language("     "))

    def test_empty_unsupported(self):
        self.assertFalse(common.looks_supported_language(""))

    def test_gdp_style_text(self):
        # Also from the regression docstring — numbers + Latin should pass.
        self.assertTrue(common.looks_supported_language("GDP -2.3% YoY"))


# ---------------------------------------------------------------------------
# load_env_file
# ---------------------------------------------------------------------------


def test_load_env_file_parses_lines_and_quotes(tmp_state, monkeypatch):
    env_path = tmp_state / ".env"
    env_path.write_text(
        "\n".join(
            [
                "# leading comment",
                "",
                "KEY=value",
                "QUOTED_DOUBLE=\"hello world\"",
                "QUOTED_SINGLE='single quoted'",
                "EMPTY_VALUE=",
                "WITH_EQUALS=a=b=c",
                "# trailing comment",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(common, "ENV_PATH", env_path, raising=False)

    # Make sure we start from a known state.
    for k in ("KEY", "QUOTED_DOUBLE", "QUOTED_SINGLE", "EMPTY_VALUE", "WITH_EQUALS"):
        monkeypatch.delenv(k, raising=False)

    common.load_env_file()

    assert os.environ.get("KEY") == "value"
    assert os.environ.get("QUOTED_DOUBLE") == "hello world"
    assert os.environ.get("QUOTED_SINGLE") == "single quoted"
    assert os.environ.get("EMPTY_VALUE") == ""
    # Only the first '=' splits, the rest is value verbatim.
    assert os.environ.get("WITH_EQUALS") == "a=b=c"


def test_load_env_file_overwrite_policy(tmp_state, monkeypatch):
    """Lock down the shell-vs-.env precedence policy.

    CLAUDE.md documents: "values don't override existing env vars set by
    the shell". The current implementation in `common.load_env_file`
    (line ~106: `os.environ[key] = value`) actually DOES overwrite
    unconditionally — there is a docs-vs-code mismatch.

    We pin down the *current* implementation behavior here so a future
    change to either side is forced to update this test deliberately.
    When the implementation is fixed to honor the docs, flip the
    assertion to `== "from_shell"`.
    """
    env_path = tmp_state / ".env"
    env_path.write_text("PRESET_KEY=from_env_file\n", encoding="utf-8")
    monkeypatch.setattr(common, "ENV_PATH", env_path, raising=False)

    monkeypatch.setenv("PRESET_KEY", "from_shell")
    common.load_env_file()

    # CURRENT BEHAVIOR: .env wins because the implementation overwrites.
    # DOCUMENTED BEHAVIOR (CLAUDE.md): shell wins. Flip this when fixed.
    assert os.environ.get("PRESET_KEY") == "from_env_file"


def test_load_env_file_missing_file_is_noop(tmp_state, monkeypatch):
    env_path = tmp_state / "does-not-exist.env"
    monkeypatch.setattr(common, "ENV_PATH", env_path, raising=False)
    # Should not raise.
    common.load_env_file()


# ---------------------------------------------------------------------------
# normalize_status_url
# ---------------------------------------------------------------------------


class NormalizeStatusUrlTests(unittest.TestCase):
    def test_plain_status_url(self):
        self.assertEqual(
            common.normalize_status_url("https://x.com/elonmusk/status/123456"),
            "https://x.com/elonmusk/status/123456",
        )

    def test_strips_photo_suffix(self):
        self.assertEqual(
            common.normalize_status_url(
                "https://x.com/elonmusk/status/123456/photo/1"
            ),
            "https://x.com/elonmusk/status/123456",
        )

    def test_strips_query_string(self):
        self.assertEqual(
            common.normalize_status_url(
                "https://x.com/elonmusk/status/123456?s=20"
            ),
            "https://x.com/elonmusk/status/123456",
        )

    def test_twitter_dot_com_not_supported(self):
        # Document current behavior: only x.com URLs match — twitter.com returns "".
        self.assertEqual(
            common.normalize_status_url(
                "https://twitter.com/elonmusk/status/123456"
            ),
            "",
        )

    def test_empty_input(self):
        self.assertEqual(common.normalize_status_url(""), "")
        self.assertEqual(common.normalize_status_url(None), "")  # type: ignore[arg-type]

    def test_not_a_status_url(self):
        self.assertEqual(
            common.normalize_status_url("https://x.com/elonmusk"),
            "",
        )


# ---------------------------------------------------------------------------
# ensure_state_dirs
# ---------------------------------------------------------------------------


def test_ensure_state_dirs_creates_all_expected(tmp_path, monkeypatch):
    """ensure_state_dirs must (re-)create STATE_DIR + 4 known subdirs."""
    state_root = tmp_path / "fresh-state"
    monkeypatch.setattr(common, "STATE_DIR", state_root, raising=False)
    monkeypatch.setattr(common, "SCREENSHOT_DIR", state_root / "screenshots", raising=False)
    monkeypatch.setattr(common, "HISTORY_DIR", state_root / "history", raising=False)
    monkeypatch.setattr(common, "POST_HISTORY_DIR", state_root / "post_history", raising=False)
    monkeypatch.setattr(common, "REVISIT_HISTORY_DIR", state_root / "revisit_history", raising=False)

    assert not state_root.exists()
    common.ensure_state_dirs()

    assert state_root.is_dir()
    assert (state_root / "screenshots").is_dir()
    assert (state_root / "history").is_dir()
    assert (state_root / "post_history").is_dir()
    assert (state_root / "revisit_history").is_dir()


def test_ensure_state_dirs_idempotent(tmp_path, monkeypatch):
    state_root = tmp_path / "fresh-state"
    monkeypatch.setattr(common, "STATE_DIR", state_root, raising=False)
    monkeypatch.setattr(common, "SCREENSHOT_DIR", state_root / "screenshots", raising=False)
    monkeypatch.setattr(common, "HISTORY_DIR", state_root / "history", raising=False)
    monkeypatch.setattr(common, "POST_HISTORY_DIR", state_root / "post_history", raising=False)
    monkeypatch.setattr(common, "REVISIT_HISTORY_DIR", state_root / "revisit_history", raising=False)

    common.ensure_state_dirs()
    # Second call must not raise.
    common.ensure_state_dirs()
    assert state_root.is_dir()


# ---------------------------------------------------------------------------
# history_path_for / post_history_path_for
# ---------------------------------------------------------------------------


def test_history_path_for_sanitizes(tmp_state):
    """Invalid filename chars get collapsed to underscores."""
    p = common.history_path_for("2026-05-11 12:30:00 CST")
    assert p.parent == common.HISTORY_DIR
    # Colons and spaces are not in [0-9A-Za-z_.-], so they collapse to one '_'.
    assert ":" not in p.name
    assert " " not in p.name
    assert p.name.endswith(".json")
    # Dots and hyphens preserved.
    assert "2026-05-11" in p.name


def test_post_history_path_for_sanitizes(tmp_state):
    p = common.post_history_path_for("hello/world\\stamp")
    assert p.parent == common.POST_HISTORY_DIR
    assert "/" not in p.name
    assert "\\" not in p.name
    assert p.name.endswith(".json")


def test_history_path_for_preserves_safe_chars(tmp_state):
    p = common.history_path_for("20260511_123000")
    assert p.name == "20260511_123000.json"


# ---------------------------------------------------------------------------
# append_log
# ---------------------------------------------------------------------------


def test_append_log_caps_at_200_entries(tmp_state):
    # tmp_state fixture re-points RUN_LOG_PATH to tmp/state/run_log.json.
    for i in range(250):
        common.append_log({"i": i})

    logs = json.loads(common.RUN_LOG_PATH.read_text(encoding="utf-8"))
    assert isinstance(logs, list)
    assert len(logs) == 200
    # The last entry should be the most recent.
    assert logs[-1] == {"i": 249}
    # Oldest retained is i=50 (250 written, keep last 200 → 50..249).
    assert logs[0] == {"i": 50}


def test_append_log_starts_from_empty(tmp_state):
    common.append_log({"hello": "world"})
    logs = json.loads(common.RUN_LOG_PATH.read_text(encoding="utf-8"))
    assert logs == [{"hello": "world"}]


if __name__ == "__main__":
    unittest.main()
