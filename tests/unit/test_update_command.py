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

    def test_update_command_starts_detached_update_process(self):
        fake_proc = object()
        with (
            patch.object(tg_commands, "_safe_notify") as notify,
            patch.object(tg_commands, "_start_update_process", return_value=fake_proc) as start_update,
            patch.object(tg_commands.job_store, "active_job", return_value=None),
            patch.object(tg_commands, "enqueue_command_job") as enqueue,
        ):
            result = tg_commands.handle_command("/update", *self._args())

        self.assertIs(result[0], fake_proc)
        start_update.assert_called_once()
        enqueue.assert_not_called()
        self.assertIn("开始更新", notify.call_args.args[0])

    def test_update_command_refuses_while_job_is_running(self):
        class RunningProc:
            def poll(self):
                return None

        running = RunningProc()
        with (
            patch.object(tg_commands, "_safe_notify") as notify,
            patch.object(tg_commands, "_start_update_process") as start_update,
            patch.object(tg_commands.job_store, "active_job", return_value=None),
            patch.object(tg_commands, "enqueue_command_job") as enqueue,
        ):
            result = tg_commands.handle_command("/update", *self._args(run_proc=running))

        self.assertIs(result[0], running)
        start_update.assert_not_called()
        enqueue.assert_not_called()
        self.assertIn("当前已有任务在执行", notify.call_args.args[0])

    def test_update_command_refuses_while_queued_runner_job_is_active(self):
        with (
            patch.object(tg_commands, "_safe_notify") as notify,
            patch.object(tg_commands, "_start_update_process") as start_update,
            patch.object(tg_commands.job_store, "active_job", return_value={"id": 7, "label": "run_once.py"}),
            patch.object(tg_commands, "enqueue_command_job") as enqueue,
        ):
            result = tg_commands.handle_command("/update", *self._args())

        self.assertIsNone(result[0])
        start_update.assert_not_called()
        enqueue.assert_not_called()
        self.assertIn("当前已有任务在执行", notify.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
