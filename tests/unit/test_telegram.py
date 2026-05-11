"""Unit tests for src.telegram.

Covers:
- `_chunk_text` / `telegram_notify` chunking regression (4096-char cap with
  4000-char safety window, newline-preferred splits, hard cuts when needed,
  CJK code-point counting).
- 429 retry behavior — both the ok=false-with-retry_after path AND the
  HTTPError(code=429)-with-retry_after path. Both should sleep ~retry_after
  seconds and retry exactly once.
- `telegram_enabled` true/false depending on env.
- `telegram_notify` raises (does NOT silently no-op) when not configured —
  this is the actual contract in the code today.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# Make sure src.common is imported before src.telegram (avoids a circular import
# fingerprint on cold collection).
import src.common  # noqa: F401, E402
import src.telegram as tg  # noqa: E402


# ---------------------------------------------------------------------------
# _chunk_text regression
# ---------------------------------------------------------------------------

class ChunkTextTests(unittest.TestCase):
    def test_short_text_single_chunk(self):
        chunks = tg._chunk_text("hello")
        self.assertEqual(chunks, ["hello"])

    def test_exact_limit_single_chunk(self):
        text = "a" * 4000
        chunks = tg._chunk_text(text)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], text)

    def test_5000_no_newlines_two_chunks(self):
        text = "a" * 5000
        chunks = tg._chunk_text(text)
        self.assertEqual(len(chunks), 2)
        # First chunk is a hard cut at the limit.
        self.assertEqual(len(chunks[0]), 4000)
        self.assertEqual(len(chunks[1]), 1000)
        # Concatenation preserves content for the no-newline case (no leading
        # newline gets stripped because there isn't one).
        self.assertEqual("".join(chunks), text)

    def test_5000_cjk_two_chunks(self):
        # Python's len() counts code points; Telegram counts UTF-16 code units,
        # but for BMP CJK chars both are equivalent — so chunking is identical.
        text = "中" * 5000
        chunks = tg._chunk_text(text)
        self.assertEqual(len(chunks), 2)
        # Every chunk respects the limit measured in code points.
        for c in chunks:
            self.assertLessEqual(len(c), 4000)
        self.assertEqual(len(chunks[0]) + len(chunks[1]), 5000)

    def test_8000_with_newlines_splits_on_newline(self):
        # 80 lines of 99 'a' chars + '\n' = 80 * 100 = 8000 chars.
        line = "a" * 99 + "\n"
        text = line * 80
        chunks = tg._chunk_text(text)
        self.assertIn(len(chunks), (2, 3))
        for c in chunks:
            self.assertLessEqual(len(c), 4000)
        # The first chunk must end at a newline boundary (the regression
        # guard: rfind("\n") is preferred over a hard cut when available).
        self.assertTrue(
            chunks[0].endswith("a" * 99) or chunks[0].endswith("\n"),
            f"first chunk should split on newline boundary; got tail={chunks[0][-5:]!r}",
        )

    def test_9000_three_chunks(self):
        text = "a" * 9000
        chunks = tg._chunk_text(text)
        self.assertEqual(len(chunks), 3)
        self.assertEqual([len(c) for c in chunks], [4000, 4000, 1000])


# ---------------------------------------------------------------------------
# Mock helpers for urlopen
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Context-manager wrapper that mimics urllib.request.urlopen's return."""

    def __init__(self, body: dict | str):
        if isinstance(body, dict):
            self._body = json.dumps(body).encode("utf-8")
        else:
            self._body = body.encode("utf-8") if isinstance(body, str) else body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _make_http_error_with_body(body: dict, code: int = 429) -> urllib.error.HTTPError:
    body_bytes = json.dumps(body).encode("utf-8")
    err = urllib.error.HTTPError(
        url="https://api.telegram.org/botX/sendMessage",
        code=code,
        msg="Too Many Requests",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body_bytes),
    )
    # Override .read() so the production code's `e.read()` returns our body
    # even after fp has been touched.
    err.read = lambda: body_bytes  # type: ignore[method-assign]
    return err


# ---------------------------------------------------------------------------
# telegram_notify chunking via urlopen mock
# ---------------------------------------------------------------------------

class TelegramNotifyChunkingTests(unittest.TestCase):
    def setUp(self):
        # Configure env so telegram_notify proceeds.
        self.env_patcher = patch.dict(
            "os.environ",
            {"X_REPLY_TG_BOT_TOKEN": "fake-token", "X_REPLY_TG_CHAT_ID": "12345"},
            clear=False,
        )
        self.env_patcher.start()
        # Make sure stale envs don't override.
        import os
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            os.environ.pop(k, None)
        self.addCleanup(self.env_patcher.stop)

    def _run_with_mock(self, text: str):
        ok_response = _FakeResponse({"ok": True, "result": {"message_id": 1}})
        urlopen_mock = MagicMock(return_value=ok_response)
        # Patch the symbol used inside src.telegram.
        with patch.object(tg.urllib.request, "urlopen", urlopen_mock):
            tg.telegram_notify(text)
        return urlopen_mock

    def _payloads_from_calls(self, urlopen_mock: MagicMock) -> list[dict]:
        out = []
        for call in urlopen_mock.call_args_list:
            args, kwargs = call
            req = args[0]
            raw = req.data
            out.append(json.loads(raw.decode("utf-8")))
        return out

    def test_short_text_one_call(self):
        urlopen_mock = self._run_with_mock("hi")
        self.assertEqual(urlopen_mock.call_count, 1)

    def test_exact_4000_one_call(self):
        urlopen_mock = self._run_with_mock("a" * 4000)
        self.assertEqual(urlopen_mock.call_count, 1)

    def test_9000_chars_chunked_into_three(self):
        urlopen_mock = self._run_with_mock("a" * 9000)
        self.assertEqual(urlopen_mock.call_count, 3)
        payloads = self._payloads_from_calls(urlopen_mock)
        for p in payloads:
            self.assertLessEqual(len(p["text"]), 4000)
            self.assertEqual(p["chat_id"], "12345")

    def test_8000_with_newlines_chunked_and_splits_on_newline(self):
        text = ("a" * 99 + "\n") * 80
        urlopen_mock = self._run_with_mock(text)
        # 8000 chars → 2 or 3 chunks depending on where the newline lands.
        self.assertIn(urlopen_mock.call_count, (2, 3))
        payloads = self._payloads_from_calls(urlopen_mock)
        for p in payloads:
            self.assertLessEqual(len(p["text"]), 4000)

    def test_5000_no_newlines_two_calls(self):
        urlopen_mock = self._run_with_mock("a" * 5000)
        self.assertEqual(urlopen_mock.call_count, 2)

    def test_5000_cjk_two_calls(self):
        urlopen_mock = self._run_with_mock("中" * 5000)
        self.assertEqual(urlopen_mock.call_count, 2)
        payloads = self._payloads_from_calls(urlopen_mock)
        # Each chunk fits the 4000-codepoint window.
        for p in payloads:
            self.assertLessEqual(len(p["text"]), 4000)


# ---------------------------------------------------------------------------
# 429 retry regression
# ---------------------------------------------------------------------------

class TelegramRetryTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            "os.environ",
            {"X_REPLY_TG_BOT_TOKEN": "fake-token", "X_REPLY_TG_CHAT_ID": "12345"},
            clear=False,
        )
        self.env_patcher.start()
        import os
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            os.environ.pop(k, None)
        self.addCleanup(self.env_patcher.stop)

        # Always mock time.sleep so retry tests don't actually wait.
        self.sleep_mock = MagicMock()
        self.sleep_patcher = patch.object(tg.time, "sleep", self.sleep_mock)
        self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

    def test_429_via_ok_false_retries_once_then_succeeds(self):
        """200 response with ok=false + retry_after triggers one retry."""
        rate_limited = _FakeResponse({
            "ok": False,
            "parameters": {"retry_after": 1},
            "description": "Too Many Requests",
        })
        success = _FakeResponse({"ok": True, "result": {"message_id": 7}})

        urlopen_mock = MagicMock(side_effect=[rate_limited, success])

        with patch.object(tg.urllib.request, "urlopen", urlopen_mock):
            result = tg.telegram_notify("hello")

        self.assertEqual(urlopen_mock.call_count, 2, "should retry exactly once")
        self.assertTrue(result.get("ok"))

        # Slept approximately retry_after seconds (implementation adds 1s headroom).
        self.assertEqual(len(self.sleep_mock.call_args_list), 1)
        slept_for = self.sleep_mock.call_args_list[0][0][0]
        self.assertGreaterEqual(slept_for, 1)
        self.assertLessEqual(slept_for, 3)

    def test_429_via_ok_false_only_retries_once_total(self):
        """If two consecutive ok=false responses arrive, we don't retry forever."""
        rate_limited = _FakeResponse({
            "ok": False,
            "parameters": {"retry_after": 1},
            "description": "Too Many Requests",
        })

        urlopen_mock = MagicMock(side_effect=[rate_limited, rate_limited])

        with patch.object(tg.urllib.request, "urlopen", urlopen_mock):
            result = tg.telegram_notify("hello")

        # First attempt + one retry = exactly 2 calls. No third attempt.
        self.assertEqual(urlopen_mock.call_count, 2)
        # Final response is the second ok=false body; we return it (no exception).
        self.assertFalse(result.get("ok", False))

    def test_429_via_http_error_retries_once_then_succeeds(self):
        """An HTTPError(code=429) with retry_after in the body triggers one retry."""
        err = _make_http_error_with_body(
            {"ok": False, "parameters": {"retry_after": 2}, "description": "Too Many Requests"},
            code=429,
        )
        success = _FakeResponse({"ok": True, "result": {"message_id": 9}})

        urlopen_mock = MagicMock(side_effect=[err, success])

        with patch.object(tg.urllib.request, "urlopen", urlopen_mock):
            result = tg.telegram_notify("hello")

        self.assertEqual(urlopen_mock.call_count, 2, "HTTPError 429 must retry once")
        self.assertTrue(result.get("ok"))
        self.assertEqual(len(self.sleep_mock.call_args_list), 1)
        slept_for = self.sleep_mock.call_args_list[0][0][0]
        self.assertGreaterEqual(slept_for, 2)
        self.assertLessEqual(slept_for, 4)

    def test_http_error_without_retry_after_propagates(self):
        """HTTPError without retry_after should re-raise (no silent swallow)."""
        err = _make_http_error_with_body({"ok": False, "description": "Bad Request"}, code=400)

        urlopen_mock = MagicMock(side_effect=[err])

        with patch.object(tg.urllib.request, "urlopen", urlopen_mock):
            with self.assertRaises(urllib.error.HTTPError):
                tg.telegram_notify("hello")


# ---------------------------------------------------------------------------
# telegram_enabled + disabled-notify contract
# ---------------------------------------------------------------------------

class TelegramEnabledTests(unittest.TestCase):
    def test_disabled_when_no_env(self, ):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(tg.telegram_enabled())

    def test_disabled_when_only_token(self):
        with patch.dict("os.environ", {"X_REPLY_TG_BOT_TOKEN": "t"}, clear=True):
            self.assertFalse(tg.telegram_enabled())

    def test_disabled_when_only_chat_id(self):
        with patch.dict("os.environ", {"X_REPLY_TG_CHAT_ID": "1"}, clear=True):
            self.assertFalse(tg.telegram_enabled())

    def test_enabled_when_both_set(self):
        env = {"X_REPLY_TG_BOT_TOKEN": "t", "X_REPLY_TG_CHAT_ID": "1"}
        with patch.dict("os.environ", env, clear=True):
            self.assertTrue(tg.telegram_enabled())

    def test_enabled_via_legacy_env_names(self):
        # env_first also accepts TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
        with patch.dict("os.environ", env, clear=True):
            self.assertTrue(tg.telegram_enabled())

    def test_notify_when_disabled_raises_runtime_error(self):
        """Contract from src/telegram.py: telegram_notify raises when unset.

        Callers gate with `telegram_enabled()` to silently skip — the bare
        `telegram_notify()` is intentionally loud.
        """
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                tg.telegram_notify("hi")


if __name__ == "__main__":
    unittest.main()
