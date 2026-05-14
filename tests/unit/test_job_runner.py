from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src import job_runner, job_store

CST = timezone(timedelta(hours=8))


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 5, 14, hour, minute, second, tzinfo=CST)


class JobRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.logs = self.root / "state" / "logs"
        self.logs.mkdir(parents=True)
        self.db_path = self.root / "state" / "jobs.db"
        self.db_patch = patch.object(job_store, "JOBS_DB_PATH", self.db_path)
        self.db_patch.start()
        job_store.init_job_store()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_tick_starts_queued_job_and_writes_to_file_not_pipe(self):
        job_store.enqueue_job("reply", "run_once.py", [sys.executable, "-c", "print('ok')"], "telegram", created_at=at(9))
        proc = MagicMock()
        proc.pid = 123
        proc.poll.return_value = None

        with patch("src.job_runner.subprocess.Popen", return_value=proc) as popen:
            runner = job_runner.JobRunner(root=self.root, log_dir=self.logs)
            runner.tick(at(9, 1))

        active = job_store.active_job()
        self.assertEqual(active["pid"], 123)
        self.assertIn("job-1.log", active["output_path"])
        self.assertIsNot(popen.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertIs(popen.call_args.kwargs["stderr"], subprocess.STDOUT)

    def test_tick_marks_completed_job_succeeded(self):
        job_store.enqueue_job("reply", "run_once.py", [sys.executable, "-c", "print('ok')"], "telegram", created_at=at(9))
        runner = job_runner.JobRunner(root=self.root, log_dir=self.logs)
        runner.tick(at(9, 1))
        runner.tick(at(9, 2))

        recent = job_store.recent_jobs(["succeeded"], limit=1)
        self.assertEqual(recent[0]["status"], "succeeded")
        self.assertEqual(recent[0]["exit_code"], 0)

    def test_tick_marks_nonzero_exit_failed(self):
        job_store.enqueue_job("reply", "run_once.py", [sys.executable, "-c", "raise SystemExit(7)"], "telegram", created_at=at(9))
        runner = job_runner.JobRunner(root=self.root, log_dir=self.logs)
        runner.tick(at(9, 1))
        runner.tick(at(9, 2))

        recent = job_store.recent_jobs(["failed"], limit=1)
        self.assertEqual(recent[0]["exit_code"], 7)

    def test_timeout_terminates_running_process(self):
        job_store.enqueue_job(
            "reply",
            "run_once.py",
            [sys.executable, "-c", "import time; time.sleep(60)"],
            "telegram",
            timeout_seconds=1,
            created_at=at(9),
        )
        proc = MagicMock()
        proc.pid = 456
        proc.poll.return_value = None

        with patch("src.job_runner.subprocess.Popen", return_value=proc):
            runner = job_runner.JobRunner(root=self.root, log_dir=self.logs)
            runner.tick(at(9, 0))
            runner.tick(at(9, 0, 2))

        proc.terminate.assert_called_once()
        recent = job_store.recent_jobs(["timed_out"], limit=1)
        self.assertEqual(recent[0]["status"], "timed_out")


if __name__ == "__main__":
    unittest.main()
