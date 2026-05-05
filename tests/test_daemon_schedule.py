"""Unit tests for daemon scheduling helpers.

Targets the carry-over and window logic AGENTS.md flagged as fragile.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Pin env vars to deterministic values before importing the daemon.
os.environ["X_REPLY_JITTER_SECONDS"] = "0"
os.environ["X_POST_JITTER_SECONDS"] = "0"
os.environ["X_LEARN_INTERVAL_SECONDS"] = "900"
os.environ["X_POST_SCHEDULE_HOURS"] = "11,19"

import bot_daemon as bd  # noqa: E402

CST = timezone(timedelta(hours=8))


def at(h, m=0, day=5):
    return datetime(2026, 5, day, h, m, tzinfo=CST)


class RevisitWindowTests(unittest.TestCase):
    def test_inside_late_evening(self):
        self.assertTrue(bd.in_revisit_window(at(23, 0)))
        self.assertTrue(bd.in_revisit_window(at(23, 59)))

    def test_inside_early_morning(self):
        self.assertTrue(bd.in_revisit_window(at(0, 0)))
        self.assertTrue(bd.in_revisit_window(at(6, 59)))

    def test_outside(self):
        self.assertFalse(bd.in_revisit_window(at(7, 0)))
        self.assertFalse(bd.in_revisit_window(at(12, 0)))
        self.assertFalse(bd.in_revisit_window(at(22, 59)))


class NextRevisitAfterTests(unittest.TestCase):
    def test_inside_advances_30_min(self):
        self.assertEqual(bd.next_revisit_after(at(23, 0)), at(23, 30))
        self.assertEqual(bd.next_revisit_after(at(0, 30)), at(1, 0))

    def test_outside_returns_today_2300(self):
        self.assertEqual(bd.next_revisit_after(at(15, 0)), at(23, 0))
        self.assertEqual(bd.next_revisit_after(at(22, 59)), at(23, 0))

    def test_outside_after_window_returns_today_2300(self):
        # 07:00 today is outside; today's 23:00 is still in the future.
        self.assertEqual(bd.next_revisit_after(at(7, 0)), at(23, 0))


class ProactiveScheduleTests(unittest.TestCase):
    def test_next_proactive_after_picks_today_11(self):
        slot = bd.next_proactive_after(at(8, 0))
        self.assertEqual(slot, at(11, 0))

    def test_next_proactive_after_skips_to_19(self):
        slot = bd.next_proactive_after(at(11, 0, 5))
        self.assertEqual(slot, at(19, 0))

    def test_next_proactive_after_rolls_to_next_day(self):
        slot = bd.next_proactive_after(at(20, 0))
        self.assertEqual(slot, at(11, 0, day=6))


class ReplyScheduleTests(unittest.TestCase):
    def test_reply_skips_quiet_hours(self):
        # 02:00 is outside 07-23, so the next slot must be today's 07:00.
        slot = bd.next_scheduled_after(at(2, 0))
        self.assertEqual(slot, at(7, 0))

    def test_reply_rolls_past_2300(self):
        slot = bd.next_scheduled_after(at(23, 30))
        self.assertEqual(slot, at(7, 0, day=6))


if __name__ == "__main__":
    unittest.main()
