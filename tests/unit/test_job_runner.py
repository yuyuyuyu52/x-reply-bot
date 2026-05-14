from __future__ import annotations

import signal
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
        self.assertIs(popen.call_args.kwargs["start_new_session"], True)

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
        proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="job", timeout=0.1), 0]

        with (
            patch("src.job_runner.subprocess.Popen", return_value=proc),
            patch("src.job_runner.os.getpgid", return_value=456) as getpgid,
            patch("src.job_runner.os.killpg") as killpg,
        ):
            runner = job_runner.JobRunner(root=self.root, log_dir=self.logs)
            runner.tick(at(9, 0))
            runner.tick(at(9, 0, 2))

        getpgid.assert_called_with(456)
        killpg.assert_called_once_with(456, signal.SIGTERM)
        proc.terminate.assert_not_called()
        recent = job_store.recent_jobs(["timed_out"], limit=1)
        self.assertEqual(recent[0]["status"], "timed_out")

    def test_mark_started_failure_terminates_spawned_process_and_clears_state(self):
        job_store.enqueue_job(
            "reply",
            "run_once.py",
            [sys.executable, "-c", "import time; time.sleep(60)"],
            "telegram",
            created_at=at(9),
        )
        job_store.enqueue_job(
            "post",
            "post_once.py",
            [sys.executable, "-c", "print('second')"],
            "telegram",
            created_at=at(9, 1),
        )
        proc = MagicMock()
        proc.pid = 789
        proc.poll.return_value = None
        proc.wait.return_value = 0
        runner = job_runner.JobRunner(root=self.root, log_dir=self.logs)

        with (
            patch("src.job_runner.subprocess.Popen", return_value=proc),
            patch("src.job_runner.job_store.mark_started", side_effect=RuntimeError("guard failed")),
            patch("src.job_runner.os.getpgid", return_value=789),
            patch("src.job_runner.os.killpg") as killpg,
        ):
            with self.assertRaises(RuntimeError):
                runner.tick(at(9, 1))

        killpg.assert_called_once_with(789, signal.SIGTERM)
        self.assertIsNone(runner.proc)
        self.assertIsNone(runner.job)
        self.assertIsNone(runner._output_fh)
        self.assertIsNone(job_store.active_job())
        recent = job_store.recent_jobs(["interrupted", "failed"], limit=1)
        self.assertEqual(recent[0]["label"], "run_once.py")
        self.assertIn("mark_started failed: guard failed", recent[0]["error_summary"])
        queued = job_store.queued_jobs(limit=10)
        self.assertEqual([job["label"] for job in queued], ["post_once.py"])

    def test_setup_failure_opening_log_file_marks_job_failed_and_clears_active_state(self):
        job_store.enqueue_job(
            "reply",
            "run_once.py",
            [sys.executable, "-c", "print('ok')"],
            "telegram",
            created_at=at(9),
        )
        runner = job_runner.JobRunner(root=self.root, log_dir=self.logs)

        with patch("pathlib.Path.open", side_effect=OSError("disk full")):
            runner.tick(at(9, 1))

        self.assertIsNone(runner.proc)
        self.assertIsNone(runner.job)
        self.assertIsNone(runner._output_fh)
        self.assertIsNone(job_store.active_job())
        recent = job_store.recent_jobs(["failed", "interrupted"], limit=1)
        self.assertEqual(recent[0]["label"], "run_once.py")
        self.assertIn("setup failed", recent[0]["error_summary"])
        self.assertIn("disk full", recent[0]["error_summary"])

    def test_failed_job_sends_failure_notification(self):
        job_store.enqueue_job(
            "reply",
            "run_once.py",
            [sys.executable, "-c", "print('bad'); raise SystemExit(2)"],
            "schedule",
            created_at=at(9),
        )
        with (
            patch("src.job_runner.telegram_enabled", return_value=True),
            patch("src.job_runner.telegram_notify") as notify,
        ):
            runner = job_runner.JobRunner(root=self.root, log_dir=self.logs)
            runner.tick(at(9, 1))
            runner.tick(at(9, 2))
        notify.assert_called_once()
        text = notify.call_args.args[0]
        self.assertIn("任务失败", text)
        self.assertIn("exit_code", text)
        self.assertIn("bad", text)

    def test_timed_out_job_sends_failure_notification(self):
        job_store.enqueue_job(
            "reply",
            "run_once.py",
            [sys.executable, "-c", "import time; time.sleep(60)"],
            "schedule",
            timeout_seconds=1,
            created_at=at(9),
        )
        proc = MagicMock()
        proc.pid = 456
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="job", timeout=0.1), 0]

        with (
            patch("src.job_runner.subprocess.Popen", return_value=proc),
            patch("src.job_runner.os.getpgid", return_value=456),
            patch("src.job_runner.os.killpg"),
            patch("src.job_runner.telegram_enabled", return_value=True),
            patch("src.job_runner.telegram_notify") as notify,
        ):
            runner = job_runner.JobRunner(root=self.root, log_dir=self.logs)
            runner.tick(at(9, 0))
            runner.tick(at(9, 0, 2))
        notify.assert_called_once()
        self.assertIn("任务失败", notify.call_args.args[0])
        self.assertIn("timed_out", notify.call_args.args[0])

    def test_succeeded_job_does_not_send_notification(self):
        job_store.enqueue_job(
            "reply",
            "run_once.py",
            [sys.executable, "-c", "print('ok')"],
            "schedule",
            created_at=at(9),
        )
        with (
            patch("src.job_runner.telegram_enabled", return_value=True),
            patch("src.job_runner.telegram_notify") as notify,
        ):
            runner = job_runner.JobRunner(root=self.root, log_dir=self.logs)
            runner.tick(at(9, 1))
            runner.tick(at(9, 2))
        notify.assert_not_called()

    def test_shutdown_terminates_group_marks_interrupted_and_clears_state(self):
        job_store.enqueue_job(
            "reply",
            "run_once.py",
            [sys.executable, "-c", "import time; time.sleep(60)"],
            "telegram",
            created_at=at(9),
        )
        proc = MagicMock()
        proc.pid = 321
        proc.poll.return_value = None
        proc.wait.return_value = 0

        with (
            patch("src.job_runner.subprocess.Popen", return_value=proc),
            patch("src.job_runner.os.getpgid", return_value=321),
            patch("src.job_runner.os.killpg") as killpg,
        ):
            runner = job_runner.JobRunner(root=self.root, log_dir=self.logs)
            runner.tick(at(9, 0))
            runner.shutdown()

        killpg.assert_called_once_with(321, signal.SIGTERM)
        recent = job_store.recent_jobs(["interrupted"], limit=1)
        self.assertEqual(recent[0]["status"], "interrupted")
        self.assertIsNone(runner.proc)
        self.assertIsNone(runner.job)
        self.assertIsNone(runner._output_fh)


if __name__ == "__main__":
    unittest.main()
