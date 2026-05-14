from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from src import reporters


CST = timezone(timedelta(hours=8))


def at(hour: int, minute: int = 0, day: int = 14) -> datetime:
    return datetime(2026, 5, day, hour, minute, tzinfo=CST)


class StatusTextTests(unittest.TestCase):
    def test_marks_past_slots_as_pending_recalculation(self):
        now = at(9, 46)
        active = {"id": 1, "label": "run_once.py", "trigger": "schedule"}

        with (
            patch("src.reporters._beijing_now", return_value=now),
            patch("src.reporters.job_store.active_job", return_value=active),
            patch("src.reporters.job_store.queued_jobs", return_value=[]),
            patch("src.reporters.job_store.recent_jobs", return_value=[]),
            patch("src.reporters.latest_summary", return_value="最近: none"),
            patch("src.reporters.learning_enabled", return_value=True),
            patch("src.reporters.hotspot_enabled", return_value=True),
        ):
            text = reporters.status_text(
                None,
                next_run_at=at(23, 18, day=13),
                next_post_run_at=at(11, 23),
                next_learn_at=at(23, 17, day=13),
                next_revisit_at=at(23, 32, day=13),
                next_hotspot_at=at(7, 30),
                active_label="run_once.py",
            )

        self.assertIn("正在执行 #1 run_once.py", text)
        self.assertIn("💬 下次回复: 当前任务完成后重算（原定 2026-05-13 23:18:00 UTC+08:00）", text)
        self.assertIn("👀 下次观察学习: 当前任务完成后重算（原定 2026-05-13 23:17:00 UTC+08:00）", text)
        self.assertIn("🔥 下次热点发现: 当前任务完成后重算（原定 2026-05-14 07:30:00 UTC+08:00）", text)
        self.assertIn("📝 下次主动发帖: 2026-05-14 11:23:00 UTC+08:00", text)


if __name__ == "__main__":
    unittest.main()
