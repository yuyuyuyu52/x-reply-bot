from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src import job_store

CST = timezone(timedelta(hours=8))


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 5, 14, hour, minute, tzinfo=CST)


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "jobs.db"
        self.patch = patch.object(job_store, "JOBS_DB_PATH", self.db_path)
        self.patch.start()
        job_store.init_job_store()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_enqueue_and_claim_job(self):
        job = job_store.enqueue_job(
            kind="reply",
            label="run_once.py",
            command=["python", "run_once.py", "--trigger", "telegram"],
            trigger="telegram",
            created_at=at(9),
        )
        self.assertEqual(job["status"], "queued")
        self.assertEqual(json.loads(job["command_json"])[1], "run_once.py")

        claimed = job_store.claim_next_job(at(9, 1))

        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["attempts"], 1)

    def test_duplicate_slot_key_returns_existing_job(self):
        first = job_store.enqueue_job(
            kind="reply",
            label="run_once.py",
            command=["python", "run_once.py"],
            trigger="schedule",
            slot_key="schedule:reply:2026-05-14T09",
            created_at=at(9),
        )
        second = job_store.enqueue_job(
            kind="reply",
            label="run_once.py",
            command=["python", "run_once.py"],
            trigger="schedule",
            slot_key="schedule:reply:2026-05-14T09",
            created_at=at(9, 5),
        )
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(job_store.queued_jobs(limit=10)), 1)

    def test_priority_order(self):
        low = job_store.enqueue_job("learn", "observe.py", ["python", "observe.py"], "schedule", priority=80, created_at=at(9))
        high = job_store.enqueue_job("reply", "run_once.py", ["python", "run_once.py"], "telegram", priority=10, created_at=at(9, 1))

        claimed = job_store.claim_next_job(at(9, 2))

        self.assertEqual(claimed["id"], high["id"])
        self.assertNotEqual(claimed["id"], low["id"])

    def test_mark_started_and_finished(self):
        job = job_store.enqueue_job("reply", "run_once.py", ["python", "run_once.py"], "telegram", created_at=at(9))
        claimed = job_store.claim_next_job(at(9, 1))
        job_store.mark_started(claimed["id"], pid=123, output_path="state/logs/job-1.log", started_at=at(9, 1))
        job_store.mark_finished(claimed["id"], status="succeeded", exit_code=0, finished_at=at(9, 2))

        recent = job_store.recent_jobs(["succeeded"], limit=1)
        self.assertEqual(recent[0]["pid"], 123)
        self.assertEqual(recent[0]["exit_code"], 0)
        self.assertEqual(recent[0]["status"], "succeeded")

    def test_mark_finished_rejects_queued_job(self):
        job = job_store.enqueue_job("reply", "run_once.py", ["python", "run_once.py"], "telegram", created_at=at(9))

        with self.assertRaisesRegex(ValueError, "expected running"):
            job_store.mark_finished(job["id"], status="succeeded", exit_code=0, finished_at=at(9, 1))

        queued = job_store.queued_jobs(limit=10)
        self.assertEqual(queued[0]["id"], job["id"])
        self.assertEqual(queued[0]["status"], "queued")

    def test_late_success_cannot_overwrite_timed_out_job(self):
        job = job_store.enqueue_job("reply", "run_once.py", ["python", "run_once.py"], "telegram", created_at=at(9))
        claimed = job_store.claim_next_job(at(9, 1))
        job_store.mark_timed_out(claimed["id"], finished_at=at(9, 2), error_summary="timeout")

        with self.assertRaisesRegex(ValueError, "expected running"):
            job_store.mark_finished(claimed["id"], status="succeeded", exit_code=0, finished_at=at(9, 3))

        recent = job_store.recent_jobs(["timed_out", "succeeded"], limit=1)
        self.assertEqual(recent[0]["id"], job["id"])
        self.assertEqual(recent[0]["status"], "timed_out")
        self.assertIsNone(recent[0]["exit_code"])

    def test_mark_started_rejects_queued_job(self):
        job = job_store.enqueue_job("reply", "run_once.py", ["python", "run_once.py"], "telegram", created_at=at(9))

        with self.assertRaisesRegex(ValueError, "expected running"):
            job_store.mark_started(job["id"], pid=123, output_path="state/logs/job-1.log", started_at=at(9, 1))

        queued = job_store.queued_jobs(limit=10)
        self.assertEqual(queued[0]["id"], job["id"])
        self.assertEqual(queued[0]["pid"], 0)
        self.assertEqual(queued[0]["output_path"], "")

    def test_has_pending_or_running(self):
        self.assertFalse(job_store.has_pending_or_running("reply", trigger="schedule"))
        job_store.enqueue_job("reply", "run_once.py", ["python", "run_once.py"], "schedule", created_at=at(9))
        self.assertTrue(job_store.has_pending_or_running("reply", trigger="schedule"))


if __name__ == "__main__":
    unittest.main()
