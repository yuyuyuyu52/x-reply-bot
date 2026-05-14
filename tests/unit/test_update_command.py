from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import src.telegram_commands as tg_commands  # noqa: E402


class TelegramUpdateCommandTests(unittest.TestCase):
    def _args(self, run_proc=None):
        now = datetime(2026, 5, 12, tzinfo=timezone.utc)
        return (run_proc, now, now, now, now, now, "", "")

    def test_update_command_enqueues_update_job(self):
        with (
            patch.object(tg_commands, "_safe_notify") as notify,
            patch.object(tg_commands, "enqueue_command_job", return_value={"id": 99}) as enqueue,
            patch.object(tg_commands.job_store, "queue_position", return_value=2),
        ):
            result = tg_commands.handle_command("/update", *self._args())

        self.assertIsNone(result[0])
        enqueue.assert_called_once_with("update")
        self.assertIn("已加入队列", notify.call_args.args[0])
        self.assertIn("#99", notify.call_args.args[0])
        self.assertIn("队列位置: 2", notify.call_args.args[0])

    def test_update_command_enqueues_while_job_is_running(self):
        class RunningProc:
            def poll(self):
                return None

        running = RunningProc()
        with (
            patch.object(tg_commands, "_safe_notify") as notify,
            patch.object(tg_commands, "enqueue_command_job", return_value={"id": 100}) as enqueue,
            patch.object(tg_commands.job_store, "queue_position", return_value=1),
        ):
            result = tg_commands.handle_command("/update", *self._args(run_proc=running))

        self.assertIs(result[0], running)
        enqueue.assert_called_once_with("update")
        self.assertIn("已加入队列", notify.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
