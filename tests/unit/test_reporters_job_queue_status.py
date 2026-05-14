from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src import reporters

CST = timezone(timedelta(hours=8))


class JobQueueStatusTests(unittest.TestCase):
    def test_status_text_includes_running_and_queued_jobs(self):
        now = datetime(2026, 5, 14, 10, 0, tzinfo=CST)
        active = {"id": 1, "label": "run_once.py", "trigger": "schedule", "started_at": "2026-05-14 09:55:00 UTC+08:00"}
        queued = [{"id": 2, "label": "post_once.py", "trigger": "telegram"}]
        failed = [{"id": 3, "label": "discover_hotspots.py", "status": "failed"}]

        with (
            patch("src.reporters._beijing_now", return_value=now),
            patch("src.reporters.job_store.active_job", return_value=active),
            patch("src.reporters.job_store.queued_jobs", return_value=queued),
            patch("src.reporters.job_store.recent_jobs", return_value=failed),
            patch("src.reporters.latest_summary", return_value="最近: none"),
            patch("src.reporters.learning_enabled", return_value=True),
            patch("src.reporters.hotspot_enabled", return_value=True),
        ):
            text = reporters.status_text(None, now, now, now, now, now, "")

        self.assertIn("正在执行 #1 run_once.py", text)
        self.assertIn("队列: 1 个", text)
        self.assertIn("#2 post_once.py", text)
        self.assertIn("最近异常", text)
        self.assertIn("#3 discover_hotspots.py failed", text)


if __name__ == "__main__":
    unittest.main()
