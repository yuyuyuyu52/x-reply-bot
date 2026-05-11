"""Unit tests for `src.llm` — provider detection, JSON parsing, cost estimation.

These pin down the recent regressions:

- `provider_mode` now uses `urlparse` and only treats the URL as Anthropic
  when the trailing path segment is literally ``anthropic``. The previous
  substring match misclassified URLs like ``.../anthropic-compat-shim/openai``.
- `estimate_cost` now uses ``str.startswith`` against versioned model names
  so server-returned ids like ``MiniMax-M2.7-20250930`` aren't silently
  zeroed. Highspeed-prefix matching must precede bare-prefix matching.

Qwen tier dispatch already has coverage in `test_estimate_cost.py` — this
file focuses on versioned-name dispatch and JSON parsing edge cases.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("X_REPLY_MODEL", "test-default")

import src.llm as llm  # noqa: E402


# ---------------------------------------------------------------------------
# provider_mode
# ---------------------------------------------------------------------------


class ProviderModeTests(unittest.TestCase):
    def _set_base(self, url: str, monkey: pytest.MonkeyPatch | None = None) -> None:
        os.environ["X_REPLY_BASE_URL"] = url

    def setUp(self):
        self._prev = os.environ.get("X_REPLY_BASE_URL")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("X_REPLY_BASE_URL", None)
        else:
            os.environ["X_REPLY_BASE_URL"] = self._prev

    def test_trailing_anthropic_segment(self):
        os.environ["X_REPLY_BASE_URL"] = "https://example.com/anthropic"
        self.assertEqual(llm.provider_mode(), "anthropic")

    def test_trailing_anthropic_with_slash(self):
        os.environ["X_REPLY_BASE_URL"] = "https://example.com/anthropic/"
        self.assertEqual(llm.provider_mode(), "anthropic")

    def test_anthropic_substring_not_misclassified(self):
        # Regression: previous substring match would (incorrectly) return
        # "anthropic" here because the string contains the word.
        os.environ["X_REPLY_BASE_URL"] = (
            "https://example.com/anthropic-compat-shim/openai"
        )
        self.assertEqual(llm.provider_mode(), "openai")

    def test_minimax_base_is_openai_compat(self):
        os.environ["X_REPLY_BASE_URL"] = "https://api.minimaxi.com/v1"
        self.assertEqual(llm.provider_mode(), "openai")

    def test_case_insensitive(self):
        os.environ["X_REPLY_BASE_URL"] = "https://EXAMPLE.com/ANTHROPIC"
        self.assertEqual(llm.provider_mode(), "anthropic")


# ---------------------------------------------------------------------------
# estimate_cost — versioned model name regressions
# ---------------------------------------------------------------------------


class EstimateCostVersionedTests(unittest.TestCase):
    def test_qwen_versioned_name_matches_bare(self):
        bare = llm.estimate_cost(
            {"prompt_tokens": 1000, "completion_tokens": 100},
            model="qwen3.5-flash",
        )
        versioned = llm.estimate_cost(
            {"prompt_tokens": 1000, "completion_tokens": 100},
            model="qwen3.5-flash-20250413",
        )
        self.assertGreater(bare["total_cost"], 0.0)
        self.assertEqual(bare["total_cost"], versioned["total_cost"])
        self.assertEqual(versioned["model"], "qwen3.5-flash-20250413")

    def test_minimax_versioned_standard(self):
        result = llm.estimate_cost(
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            model="MiniMax-M2.7-20250930",
        )
        # Standard MiniMax rates: 2.1 + 8.4 per 1M = 10.5 CNY.
        self.assertAlmostEqual(result["total_cost"], 10.5, places=6)

    def test_minimax_versioned_highspeed(self):
        result = llm.estimate_cost(
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            model="MiniMax-M2.5-highspeed-20250930",
        )
        # Highspeed rates: 4.2 + 16.8 per 1M = 21.0 CNY.
        self.assertAlmostEqual(result["total_cost"], 21.0, places=6)

    def test_highspeed_priority_over_bare(self):
        """Highspeed prefix must beat bare MiniMax prefix — order matters.

        If the dispatch ever lists the bare prefix first, M2.5-highspeed
        would silently fall into the cheaper tier.
        """
        bare = llm.estimate_cost(
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            model="MiniMax-M2.5",
        )
        highspeed = llm.estimate_cost(
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            model="MiniMax-M2.5-highspeed-20250930",
        )
        self.assertGreater(bare["total_cost"], 0.0)
        self.assertGreater(highspeed["total_cost"], 0.0)
        self.assertNotEqual(bare["total_cost"], highspeed["total_cost"])
        # Sanity: highspeed is exactly 2x bare.
        self.assertAlmostEqual(highspeed["total_cost"], bare["total_cost"] * 2, places=6)

    def test_claude_unknown_model_is_free(self):
        # Current behavior: any model not in the qwen/minimax prefix sets
        # silently costs zero. Locking this in so a future "we added Claude
        # pricing" change has to update this assertion deliberately.
        result = llm.estimate_cost(
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            model="claude-sonnet-4-5-20250930",
        )
        self.assertEqual(result["total_cost"], 0.0)
        self.assertEqual(result["model"], "claude-sonnet-4-5-20250930")
        self.assertEqual(result["input_per_million"], 0.0)
        self.assertEqual(result["output_per_million"], 0.0)


# ---------------------------------------------------------------------------
# parse_json_object
# ---------------------------------------------------------------------------


class ParseJsonObjectTests(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(llm.parse_json_object('{"a": 1, "b": "x"}'), {"a": 1, "b": "x"})

    def test_fenced_json_block(self):
        text = "```json\n{\"a\": 1}\n```"
        self.assertEqual(llm.parse_json_object(text), {"a": 1})

    def test_fenced_block_no_language(self):
        text = "```\n{\"a\": 1}\n```"
        self.assertEqual(llm.parse_json_object(text), {"a": 1})

    def test_leading_prose(self):
        # extract_first_json_object should find the {…} even when surrounded
        # by prose. The parse_json_object code path falls through to it when
        # the raw json.loads fails.
        text = "Sure, here is the result: {\"a\": 1, \"b\": 2} -- enjoy!"
        self.assertEqual(llm.parse_json_object(text), {"a": 1, "b": 2})

    def test_empty_string_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            llm.parse_json_object("")
        self.assertIn("Empty JSON response", str(ctx.exception))

    def test_whitespace_only_raises(self):
        with self.assertRaises(RuntimeError):
            llm.parse_json_object("   \n  \t  ")

    def test_only_fences_no_body_raises(self):
        # ```json``` with no body should fail clearly, not silently return {}.
        with self.assertRaises((RuntimeError, ValueError)):
            llm.parse_json_object("```json\n\n```")

    def test_brace_inside_string(self):
        # The string-aware scanner in extract_first_json_object must not
        # treat the "}" inside the quoted value as a closing brace.
        text = 'prose {"a": "}"} more prose'
        self.assertEqual(llm.parse_json_object(text), {"a": "}"})

    def test_escape_sequences_in_strings(self):
        # Literal source: {"a": "\""}  — i.e. value is a single quote char.
        text = '{"a": "\\""}'
        self.assertEqual(llm.parse_json_object(text), {"a": '"'})

    def test_array_top_level_rejected(self):
        # Top-level array is not a JSON object — parser should reject it.
        with self.assertRaises(RuntimeError):
            llm.parse_json_object("[1, 2, 3]")


# ---------------------------------------------------------------------------
# extract_first_json_object
# ---------------------------------------------------------------------------


class ExtractFirstJsonObjectTests(unittest.TestCase):
    def test_noisy_input_returns_just_the_object(self):
        # Note: the slice is character-balanced, not JSON-parsed, so an
        # unquoted key like `{a:1, ...}` is still returned verbatim.
        result = llm.extract_first_json_object("foo {a:1, b:[1,2,3]} bar")
        self.assertEqual(result, "{a:1, b:[1,2,3]}")

    def test_no_brace_returns_empty(self):
        self.assertEqual(llm.extract_first_json_object("no braces here"), "")

    def test_handles_nested_object(self):
        text = 'prose {"outer": {"inner": 1}} trailing'
        self.assertEqual(
            llm.extract_first_json_object(text),
            '{"outer": {"inner": 1}}',
        )

    def test_string_containing_braces_not_counted(self):
        text = 'noise {"a": "{nested}"} tail'
        self.assertEqual(
            llm.extract_first_json_object(text),
            '{"a": "{nested}"}',
        )

    def test_unbalanced_returns_empty(self):
        # An opening brace without a matching close — scanner walks off the
        # end and returns "".
        self.assertEqual(llm.extract_first_json_object("prose {\"a\": 1"), "")


# ---------------------------------------------------------------------------
# qwen35_flash_rates — quick sanity (deeper coverage lives in test_estimate_cost)
# ---------------------------------------------------------------------------


class QwenRatesSanityTests(unittest.TestCase):
    def test_returns_dict_with_expected_keys(self):
        rates = llm.qwen35_flash_rates(10_000)
        self.assertIn("input_per_million", rates)
        self.assertIn("output_per_million", rates)
        # Output rate is always 10x input rate for qwen3.5-flash.
        self.assertAlmostEqual(
            rates["output_per_million"], rates["input_per_million"] * 10, places=6
        )


if __name__ == "__main__":
    unittest.main()
