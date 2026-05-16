"""Unit tests for daemon scheduling helpers and the schedule-enqueue path."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Pin env vars to deterministic values before importing the daemon.
os.environ["X_REPLY_JITTER_SECONDS"] = "0"
os.environ["X_POST_JITTER_SECONDS"] = "0"
os.environ["X_LEARN_INTERVAL_SECONDS"] = "900"
os.environ.pop("X_POST_SCHEDULE_HOURS", None)

import src.scheduling as bd  # noqa: E402  -- scheduling helpers were extracted from bot_daemon
import bot_daemon  # noqa: E402

CST = timezone(timedelta(hours=8))


def at(h, m=0, day=5):
    return datetime(2026, 5, day, h, m, tzinfo=CST)


class RevisitWindowTests(unittest.TestCase):
    def test_inside_midnight_hour(self):
        self.assertTrue(bd.in_revisit_window(at(0, 0)))
        self.assertTrue(bd.in_revisit_window(at(0, 59)))

    def test_outside(self):
        self.assertFalse(bd.in_revisit_window(at(1, 0)))
        self.assertFalse(bd.in_revisit_window(at(12, 0)))
        self.assertFalse(bd.in_revisit_window(at(23, 59)))


class NextRevisitAfterTests(unittest.TestCase):
    def test_inside_rolls_to_next_midnight(self):
        self.assertEqual(bd.next_revisit_after(at(0, 0)), at(0, 0, day=6))
        self.assertEqual(bd.next_revisit_after(at(0, 30)), at(0, 0, day=6))

    def test_outside_returns_next_midnight(self):
        self.assertEqual(bd.next_revisit_after(at(15, 0)), at(0, 0, day=6))
        self.assertEqual(bd.next_revisit_after(at(23, 59)), at(0, 0, day=6))

    def test_after_midnight_returns_tomorrow_midnight(self):
        self.assertEqual(bd.next_revisit_after(at(1, 0)), at(0, 0, day=6))


class ProactiveScheduleTests(unittest.TestCase):
    def test_next_proactive_after_defaults_to_four_daily_slots(self):
        self.assertEqual(bd.proactive_schedule_hours(), [9, 13, 17, 21])

    def test_next_proactive_after_picks_today_9(self):
        slot = bd.next_proactive_after(at(8, 0))
        self.assertEqual(slot, at(9, 0))

    def test_next_proactive_after_skips_to_13(self):
        slot = bd.next_proactive_after(at(9, 0, 5))
        self.assertEqual(slot, at(13, 0))

    def test_next_proactive_after_rolls_to_next_day(self):
        slot = bd.next_proactive_after(at(22, 0))
        self.assertEqual(slot, at(9, 0, day=6))


class HotspotScheduleTests(unittest.TestCase):
    def test_next_hotspot_after_defaults_to_today_0730(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("X_HOTSPOT_SCHEDULE_TIME", None)
            slot = bd.next_hotspot_after(at(2, 0))
        self.assertEqual(slot, at(7, 30))

    def test_next_hotspot_after_rolls_to_tomorrow_after_daily_slot(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("X_HOTSPOT_SCHEDULE_TIME", None)
            slot = bd.next_hotspot_after(at(8, 0))
        self.assertEqual(slot, at(7, 30, day=6))

    def test_next_hotspot_after_uses_configured_daily_time(self):
        with patch.dict(os.environ, {"X_HOTSPOT_SCHEDULE_TIME": "06:15"}):
            slot = bd.next_hotspot_after(at(2, 0))
        self.assertEqual(slot, at(6, 15))


class ReplyScheduleTests(unittest.TestCase):
    def test_reply_skips_midnight_revisit_hour(self):
        slot = bd.next_scheduled_after(at(2, 0))
        self.assertEqual(slot, at(3, 0))

    def test_reply_runs_after_2300(self):
        slot = bd.next_scheduled_after(at(23, 30))
        self.assertEqual(slot, at(1, 0, day=6))

    def test_reply_does_not_schedule_midnight_hour(self):
        slot = bd.next_scheduled_after(at(23, 59))
        self.assertEqual(slot, at(1, 0, day=6))


class ScheduleEnqueueTests(unittest.TestCase):
    def test_enqueue_scheduled_job_skips_when_same_kind_pending(self):
        with (
            patch("bot_daemon.job_store.has_pending_or_running", return_value=True) as pending,
            patch("bot_daemon.job_store.enqueue_job") as enqueue,
        ):
            job = bot_daemon.enqueue_scheduled_job("reply", at(9, 0))
        self.assertIsNone(job)
        pending.assert_called_once_with("reply", trigger="schedule")
        enqueue.assert_not_called()

    def test_enqueue_scheduled_job_creates_slot_key(self):
        with (
            patch("bot_daemon.job_store.has_pending_or_running", return_value=False),
            patch("bot_daemon.job_store.enqueue_job", return_value={"id": 1}) as enqueue,
        ):
            job = bot_daemon.enqueue_scheduled_job("reply", at(9, 0))
        self.assertEqual(job["id"], 1)
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(kwargs["slot_key"], "schedule:reply:2026-05-05T09:00")
        self.assertEqual(kwargs["trigger"], "schedule")
        self.assertEqual(kwargs["kind"], "reply")
        self.assertEqual(kwargs["command"][1], str(bot_daemon.ROOT / "run_once.py"))
        self.assertIn("--trigger", kwargs["command"])
        self.assertIn("schedule", kwargs["command"])


if __name__ == "__main__":
    unittest.main()
