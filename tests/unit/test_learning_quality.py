"""Sanity checks for `learning_store._best_label` quality ranking."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.learning_store import _best_label, QUALITY_RANK  # noqa: E402


class QualityRankTests(unittest.TestCase):
    def test_rank_order(self):
        self.assertLess(QUALITY_RANK["skip"], QUALITY_RANK["seen"])
        self.assertLess(QUALITY_RANK["seen"], QUALITY_RANK["worth_watching"])
        self.assertLess(QUALITY_RANK["worth_watching"], QUALITY_RANK["high_quality"])

    def test_keeps_higher_label(self):
        self.assertEqual(_best_label("high_quality", "seen"), "high_quality")
        self.assertEqual(_best_label("seen", "high_quality"), "high_quality")

    def test_equal_labels_unchanged(self):
        self.assertEqual(_best_label("seen", "seen"), "seen")

    def test_unknown_label_treated_as_zero(self):
        # Unknown new label must not downgrade an existing seen.
        self.assertEqual(_best_label("seen", "garbage"), "seen")
        # Unknown old label is upgraded by any known label.
        self.assertEqual(_best_label("garbage", "seen"), "seen")


if __name__ == "__main__":
    unittest.main()
