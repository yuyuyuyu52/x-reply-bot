"""Unit tests for `common.estimate_cost` and tier dispatch.

These pin down the pricing tiers so future cost-table edits don't silently
change historical accounting math.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Make sure `model_name()` doesn't pull from .env unexpectedly.
os.environ.setdefault("X_REPLY_MODEL", "test-default")

from src.common import estimate_cost, qwen35_flash_rates  # noqa: E402


class QwenTierTests(unittest.TestCase):
    def test_low_tier(self):
        rates = qwen35_flash_rates(50_000)
        self.assertEqual(rates, {"input_per_million": 0.2, "output_per_million": 2.0})

    def test_low_tier_boundary(self):
        # 128_000 is inclusive in the low tier per current implementation.
        rates = qwen35_flash_rates(128_000)
        self.assertEqual(rates["input_per_million"], 0.2)

    def test_mid_tier(self):
        rates = qwen35_flash_rates(200_000)
        self.assertEqual(rates, {"input_per_million": 0.8, "output_per_million": 8.0})

    def test_high_tier(self):
        rates = qwen35_flash_rates(500_000)
        self.assertEqual(rates, {"input_per_million": 1.2, "output_per_million": 12.0})


class EstimateCostTests(unittest.TestCase):
    def test_qwen_low_tier_math(self):
        # 100k prompt tokens is the low tier (≤ 128k → 0.2 / 2.0).
        result = estimate_cost(
            {"prompt_tokens": 100_000, "completion_tokens": 50_000},
            model="qwen3.5-flash",
        )
        # 100k * 0.2/1M + 50k * 2.0/1M = 0.02 + 0.10 = 0.12
        self.assertAlmostEqual(result["total_cost"], 0.12, places=6)
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(result["model"], "qwen3.5-flash")

    def test_qwen_mid_tier_math(self):
        # 200k prompt tokens crosses into the mid tier (0.8 / 8.0).
        result = estimate_cost(
            {"prompt_tokens": 200_000, "completion_tokens": 50_000},
            model="qwen3.5-flash",
        )
        # 200k * 0.8/1M + 50k * 8.0/1M = 0.16 + 0.40 = 0.56
        self.assertAlmostEqual(result["total_cost"], 0.56, places=6)

    def test_minimax_standard(self):
        result = estimate_cost(
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            model="MiniMax-M2.7",
        )
        self.assertAlmostEqual(result["total_cost"], 10.5, places=6)

    def test_minimax_highspeed(self):
        result = estimate_cost(
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            model="MiniMax-M2.7-highspeed",
        )
        # Highspeed is exactly 2x standard.
        self.assertAlmostEqual(result["total_cost"], 21.0, places=6)

    def test_unknown_model_is_free(self):
        result = estimate_cost(
            {"prompt_tokens": 100_000, "completion_tokens": 50_000},
            model="never-shipped-model",
        )
        self.assertEqual(result["total_cost"], 0.0)

    def test_zero_tokens(self):
        result = estimate_cost({}, model="qwen3.5-flash")
        self.assertEqual(result["total_cost"], 0.0)

    def test_string_token_counts_coerced(self):
        # APIs sometimes return strings; estimate_cost coerces with int().
        result = estimate_cost(
            {"prompt_tokens": "1000", "completion_tokens": "500"},
            model="MiniMax-M2.7",
        )
        # 1000 * 2.1/1e6 + 500 * 8.4/1e6 = 0.0021 + 0.0042 = 0.0063
        self.assertAlmostEqual(result["total_cost"], 0.0063, places=8)


class DeepSeekEstimateCostTests(unittest.TestCase):
    def setUp(self):
        self._prev_rate = os.environ.get("X_REPLY_USD_CNY_RATE")
        os.environ["X_REPLY_USD_CNY_RATE"] = "7.2"

    def tearDown(self):
        if self._prev_rate is None:
            os.environ.pop("X_REPLY_USD_CNY_RATE", None)
        else:
            os.environ["X_REPLY_USD_CNY_RATE"] = self._prev_rate

    def test_deepseek_v4_flash_cache_miss_math(self):
        result = estimate_cost(
            {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            model="deepseek-v4-flash",
        )
        # ($0.14 input + $0.28 output) * 7.2 CNY/USD = 3.024 CNY.
        self.assertAlmostEqual(result["total_cost"], 3.024, places=6)
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(result["model"], "deepseek-v4-flash")
        self.assertAlmostEqual(result["input_per_million"], 1.008, places=6)
        self.assertAlmostEqual(result["output_per_million"], 2.016, places=6)

    def test_deepseek_v4_flash_uses_cache_hit_and_miss_tokens(self):
        result = estimate_cost(
            {
                "prompt_tokens": 1_000_000,
                "prompt_cache_hit_tokens": 250_000,
                "prompt_cache_miss_tokens": 750_000,
                "completion_tokens": 500_000,
            },
            model="deepseek-v4-flash",
        )
        # (0.25M*$0.0028 + 0.75M*$0.14 + 0.5M*$0.28) * 7.2 = 1.76904.
        self.assertAlmostEqual(result["total_cost"], 1.76904, places=6)


if __name__ == "__main__":
    unittest.main()
