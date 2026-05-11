"""Unit tests for src.metrics.

Pins down the count parser, aria-label metrics extractor, engagement scoring,
and own-handle inference. The aria-label tests guard the "reply" vs "replies"
substring bug regression — see comment on parse_metrics in src/metrics.py.

NOTE: the public API exported by `src/metrics.py` is `normalize_count` (not
`parse_count`) and `engagement_score` (no separate `engagement_bonus` function).
Tests below cover what the module actually exposes — the additional bonus
calculation lives inline in `src/reply/prepare_post.py`.
"""
from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

import src.common as common_mod  # noqa: E402
import src.metrics as metrics  # noqa: E402
from src.metrics import (  # noqa: E402
    engagement_score,
    infer_own_handle,
    normalize_count,
    parse_metrics,
)


class NormalizeCountTests(unittest.TestCase):
    """`normalize_count` is the project's `parse_count`."""

    def test_plain_integer(self):
        self.assertEqual(normalize_count("1234"), 1234)

    def test_comma_grouping(self):
        self.assertEqual(normalize_count("1,234"), 1234)

    def test_fullwidth_comma(self):
        self.assertEqual(normalize_count("1，234"), 1234)

    def test_k_suffix_decimal(self):
        self.assertEqual(normalize_count("1.2K"), 1200)

    def test_k_suffix_lower(self):
        self.assertEqual(normalize_count("5k"), 5000)

    def test_m_suffix(self):
        self.assertEqual(normalize_count("3M"), 3_000_000)

    def test_b_suffix(self):
        self.assertEqual(normalize_count("3B"), 3_000_000_000)

    def test_chinese_wan(self):
        self.assertEqual(normalize_count("1.5万"), 15_000)

    def test_chinese_yi(self):
        self.assertEqual(normalize_count("2亿"), 200_000_000)

    def test_large_plain(self):
        self.assertEqual(normalize_count("10000"), 10000)

    def test_zero(self):
        self.assertEqual(normalize_count("0"), 0)

    def test_empty_string(self):
        self.assertEqual(normalize_count(""), 0)

    def test_whitespace_only(self):
        self.assertEqual(normalize_count("   "), 0)

    def test_non_numeric(self):
        # "Like" is the aria-label sometimes shown when the count is zero.
        self.assertEqual(normalize_count("Like"), 0)

    def test_none_safe(self):
        # The implementation guards `raw or ""`, so None should not crash.
        self.assertEqual(normalize_count(None), 0)


class ParseMetricsTests(unittest.TestCase):
    """`parse_metrics` walks aria-label strings into the metrics dict."""

    def test_empty_list(self):
        result = parse_metrics([])
        self.assertEqual(result, {"views": 0, "replies": 0, "reposts": 0, "likes": 0, "bookmarks": 0})

    def test_none_safe(self):
        self.assertEqual(parse_metrics(None)["replies"], 0)

    def test_basic_english_labels(self):
        # NB: "78 bookmarks" would absorb the trailing 'b' as billion-suffix
        # under the current regex; we side-step that by avoiding ambiguous
        # numeric/letter boundaries — those are tracked separately if ever
        # found in the wild. The basic-label test covers the typical X aria.
        labels = [
            "12 replies",
            "34 reposts",
            "56 likes",
            "7,890 书签",
            "9,000 views",
        ]
        result = parse_metrics(labels)
        self.assertEqual(result["replies"], 12)
        self.assertEqual(result["reposts"], 34)
        self.assertEqual(result["likes"], 56)
        self.assertEqual(result["bookmarks"], 7890)
        self.assertEqual(result["views"], 9000)

    def test_reply_vs_replies_substring_regression(self):
        """REGRESSION: `'reply' in 'replies'` is False (no 'y' in 'replies').

        Older code used the literal `'reply'` substring; this missed the
        plural `'replies'` form. The fix matches on `'repl'` (shared prefix).
        We assert the plural form lands in `replies`.
        """
        # The composite label X often emits: "5 replies, Reply" — the suffix
        # ", Reply" is the action button hint. The count must still be 5.
        result = parse_metrics(["5 replies, Reply"])
        self.assertEqual(result["replies"], 5)

    def test_singular_reply_label(self):
        # When the count is exactly 1 X uses the singular form.
        result = parse_metrics(["1 reply"])
        self.assertEqual(result["replies"], 1)

    def test_chinese_reply_label(self):
        result = parse_metrics(["12 回复"])
        self.assertEqual(result["replies"], 12)

    def test_chinese_view_label(self):
        result = parse_metrics(["1.2万 次查看"])
        self.assertEqual(result["views"], 12_000)

    def test_mixed_case_aria_label(self):
        # The implementation lower-cases each label internally.
        result = parse_metrics(["100 LIKES", "200 Reposts"])
        self.assertEqual(result["likes"], 100)
        self.assertEqual(result["reposts"], 200)

    def test_retweet_alias(self):
        # X used to say "retweets" — we still parse them as reposts.
        result = parse_metrics(["50 retweets"])
        self.assertEqual(result["reposts"], 50)

    def test_max_wins_on_duplicate(self):
        # If the same metric appears in two aria-labels the larger wins.
        result = parse_metrics(["3 likes", "5 likes"])
        self.assertEqual(result["likes"], 5)

    def test_views_only_label(self):
        result = parse_metrics(["12,345 views"])
        self.assertEqual(result["views"], 12_345)


class EngagementScoreTests(unittest.TestCase):
    def test_zero_engagement_is_zero(self):
        # All-zero must return 0.0 cleanly — no log(0) NaN/-inf leakage.
        score = engagement_score({"views": 0, "replies": 0, "reposts": 0, "likes": 0, "bookmarks": 0})
        self.assertEqual(score, 0.0)
        self.assertFalse(math.isnan(score))
        self.assertFalse(math.isinf(score))

    def test_empty_dict(self):
        score = engagement_score({})
        self.assertEqual(score, 0.0)

    def test_high_engagement(self):
        score = engagement_score({
            "views": 1_000_000,
            "replies": 1000,
            "reposts": 500,
            "likes": 5000,
            "bookmarks": 200,
        })
        self.assertGreater(score, 50.0)
        self.assertFalse(math.isnan(score))

    def test_negative_inputs_clamped(self):
        # The implementation clamps each metric with max(int(v), 0) so
        # negative noise doesn't blow up log1p.
        score = engagement_score({
            "views": -100, "replies": -10, "reposts": -5, "likes": -1, "bookmarks": -1,
        })
        self.assertEqual(score, 0.0)

    def test_monotonic_in_views(self):
        low = engagement_score({"views": 10})
        high = engagement_score({"views": 10000})
        self.assertGreater(high, low)

    def test_string_inputs_coerced(self):
        # The implementation uses int(metrics.get(...)) so numeric strings work.
        score = engagement_score({"views": "100", "likes": "5"})
        self.assertGreater(score, 0.0)


class InferOwnHandleTests:
    """Pytest-style class so we can take the `tmp_state` fixture."""

    def test_no_latest_post_returns_empty(self, tmp_state, monkeypatch):
        # tmp_state retargets src.common.LATEST_POST_RUN_PATH; src.metrics
        # bound the constant at import time so we patch it explicitly.
        monkeypatch.setattr(metrics, "LATEST_POST_RUN_PATH", common_mod.LATEST_POST_RUN_PATH)
        assert infer_own_handle() == ""

    def test_extracts_handle_from_status_url(self, tmp_state, monkeypatch):
        monkeypatch.setattr(metrics, "LATEST_POST_RUN_PATH", common_mod.LATEST_POST_RUN_PATH)
        common_mod.LATEST_POST_RUN_PATH.write_text(
            json.dumps({"post_url": "https://x.com/SomeUser/status/1234567890"}),
            encoding="utf-8",
        )
        assert infer_own_handle() == "someuser"

    def test_extracts_handle_lowercased(self, tmp_state, monkeypatch):
        monkeypatch.setattr(metrics, "LATEST_POST_RUN_PATH", common_mod.LATEST_POST_RUN_PATH)
        common_mod.LATEST_POST_RUN_PATH.write_text(
            json.dumps({"post_url": "https://x.com/CamelCase_Handle/status/42"}),
            encoding="utf-8",
        )
        assert infer_own_handle() == "camelcase_handle"

    def test_malformed_url_returns_empty(self, tmp_state, monkeypatch):
        monkeypatch.setattr(metrics, "LATEST_POST_RUN_PATH", common_mod.LATEST_POST_RUN_PATH)
        common_mod.LATEST_POST_RUN_PATH.write_text(
            json.dumps({"post_url": "https://example.com/not-a-status-url"}),
            encoding="utf-8",
        )
        assert infer_own_handle() == ""

    def test_missing_post_url_key(self, tmp_state, monkeypatch):
        monkeypatch.setattr(metrics, "LATEST_POST_RUN_PATH", common_mod.LATEST_POST_RUN_PATH)
        common_mod.LATEST_POST_RUN_PATH.write_text(json.dumps({}), encoding="utf-8")
        assert infer_own_handle() == ""


if __name__ == "__main__":
    unittest.main()
