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


def _simulate_carry_over(
    *,
    active_label: str,
    finished_at: datetime,
    next_run_at: datetime,
    next_post_run_at: datetime,
    next_revisit_at: datetime,
    next_hotspot_at: datetime,
    hotspot_enabled: bool = True,
) -> dict:
    """Replicates the carry-over arithmetic from bot_daemon.main().

    Kept in lock-step with the post-job block. If the daemon's logic
    changes, this helper must change too — that's the point: the helper
    documents the invariant.
    """
    carry_over_post_slot = (
        not active_label.startswith("post_once.py")
        and next_post_run_at <= finished_at
    )
    carry_over_reply_slot = (
        active_label != "run_once.py"
        and next_run_at <= finished_at
    )
    carry_over_revisit_slot = (
        active_label != "src/revisit.py"
        and bd.in_revisit_window(finished_at)
        and next_revisit_at <= finished_at
    )
    carry_over_hotspot_slot = (
        active_label != "discover_hotspots.py"
        and hotspot_enabled
        and next_hotspot_at <= finished_at
    )
    return {
        "next_run_at": finished_at if carry_over_reply_slot else bd.next_scheduled_after(finished_at),
        "next_post_run_at": finished_at if carry_over_post_slot else bd.next_proactive_after(finished_at),
        "next_revisit_at": finished_at if carry_over_revisit_slot else bd.next_revisit_after(finished_at),
        "next_hotspot_at": finished_at if carry_over_hotspot_slot else bd.next_hotspot_after(finished_at),
    }


class CarryOverTests(unittest.TestCase):
    """Cover the slot-carry-over invariant called out in bot_daemon.main().

    Scenario: a long-running job is active while another slot's due-time
    elapses. When the job finishes, the elapsed slot must be carried over
    (next-fire = finished_at) rather than skipped to its next normal
    window — otherwise the daemon silently drops slots.
    """

    def test_long_running_reply_carries_over_proactive_slot(self):
        # Reply job starts at 10:55, finishes at 11:05. Proactive 11:00
        # slot fired during the run. It must be carried over.
        finished = at(11, 5)
        sim = _simulate_carry_over(
            active_label="run_once.py",
            finished_at=finished,
            next_run_at=at(12, 0),       # next reply slot still in future
            next_post_run_at=at(11, 0),  # post slot fired during the run
            next_revisit_at=at(23, 0),
            next_hotspot_at=at(13, 0),
        )
        # Post slot carried over to finished_at.
        self.assertEqual(sim["next_post_run_at"], finished)
        # Reply was the active job → never carries itself over.
        self.assertEqual(sim["next_run_at"], bd.next_scheduled_after(finished))

    def test_long_running_post_carries_over_reply_slot(self):
        # Post job runs across the top of an hour; reply slot fired
        # during the run.
        finished = at(11, 30)
        sim = _simulate_carry_over(
            active_label="post_once.py",
            finished_at=finished,
            next_run_at=at(11, 0),       # reply slot fired during the run
            next_post_run_at=at(19, 0),
            next_revisit_at=at(23, 0),
            next_hotspot_at=at(13, 0),
        )
        self.assertEqual(sim["next_run_at"], finished)
        # Post job was active → its own slot is recomputed normally.
        self.assertEqual(sim["next_post_run_at"], bd.next_proactive_after(finished))

    def test_carry_over_skipped_when_slot_still_future(self):
        # No slot elapsed during the run → nothing to carry over.
        finished = at(10, 30)
        sim = _simulate_carry_over(
            active_label="run_once.py",
            finished_at=finished,
            next_run_at=at(12, 0),
            next_post_run_at=at(11, 0),
            next_revisit_at=at(23, 0),
            next_hotspot_at=at(13, 0),
        )
        # Both still in the future → standard recomputation, NOT finished_at.
        self.assertEqual(sim["next_post_run_at"], bd.next_proactive_after(finished))
        self.assertEqual(sim["next_run_at"], bd.next_scheduled_after(finished))

    def test_revisit_carry_over_only_inside_window(self):
        # Revisit slot only carries over if finished_at is still inside the
        # nightly window. At 08:00 the window has closed, so even if the
        # stored next_revisit_at is in the past, we must NOT pin to
        # finished_at — we should fall through to next_revisit_after().
        finished = at(8, 0)
        sim = _simulate_carry_over(
            active_label="run_once.py",
            finished_at=finished,
            next_run_at=at(9, 0),
            next_post_run_at=at(11, 0),
            next_revisit_at=at(7, 30),  # in the past, but window is closed
            next_hotspot_at=at(13, 0),
        )
        self.assertEqual(sim["next_revisit_at"], bd.next_revisit_after(finished))

    def test_hotspot_disabled_disables_carry_over(self):
        finished = at(12, 0)
        sim = _simulate_carry_over(
            active_label="run_once.py",
            finished_at=finished,
            next_run_at=at(13, 0),
            next_post_run_at=at(19, 0),
            next_revisit_at=at(23, 0),
            next_hotspot_at=at(11, 0),  # elapsed
            hotspot_enabled=False,
        )
        # Disabled → never pin to finished_at, recompute normally.
        self.assertEqual(sim["next_hotspot_at"], bd.next_hotspot_after(finished))


if __name__ == "__main__":
    unittest.main()
