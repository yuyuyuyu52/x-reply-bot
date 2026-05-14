from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import src.telegram_commands as tg_commands


class TelegramJobQueueTests(unittest.TestCase):
    def _args(self, run_proc=None):
        now = datetime(2026, 5, 14, tzinfo=timezone.utc)
        return (run_proc, now, now, now, now, now, "", "")

    def test_run_enqueues_reply_instead_of_starting_process(self):
        fake_job = {"id": 42}
        with (
            patch.object(tg_commands, "_safe_notify") as notify,
            patch.object(tg_commands, "enqueue_command_job", return_value=fake_job) as enqueue,
            patch.object(tg_commands.job_store, "queue_position", return_value=3),
        ):
            result = tg_commands.handle_command("/run", *self._args())
        self.assertIsNone(result[0])
        enqueue.assert_called_once_with("reply")
        self.assertIn("已加入队列", notify.call_args.args[0])
        self.assertIn("#42", notify.call_args.args[0])
        self.assertIn("队列位置: 3", notify.call_args.args[0])

    def test_run_enqueues_even_when_job_is_running(self):
        class RunningProc:
            def poll(self):
                return None

        running = RunningProc()
        with (
            patch.object(tg_commands, "_safe_notify") as notify,
            patch.object(tg_commands, "enqueue_command_job", return_value={"id": 43}) as enqueue,
            patch.object(tg_commands.job_store, "queue_position", return_value=1),
        ):
            result = tg_commands.handle_command("/run", *self._args(run_proc=running))
        self.assertIs(result[0], running)
        enqueue.assert_called_once_with("reply")
        self.assertIn("已加入队列", notify.call_args.args[0])

    def test_post_dry_run_enqueues_post_dry(self):
        with (
            patch.object(tg_commands, "_safe_notify"),
            patch.object(tg_commands, "enqueue_command_job", return_value={"id": 7}) as enqueue,
            patch.object(tg_commands.job_store, "queue_position", return_value=0),
        ):
            tg_commands.handle_command("/post_dry_run", *self._args())
        enqueue.assert_called_once_with("post_dry")

    def test_all_start_commands_enqueue_expected_kind(self):
        cases = {
            "/post_once": "post",
            "/learn_once": "learn",
            "/revisit_once": "revisit",
            "/hotspot_discover": "hotspot",
        }
        for command, kind in cases.items():
            with self.subTest(command=command):
                with (
                    patch.object(tg_commands, "_safe_notify"),
                    patch.object(tg_commands, "enqueue_command_job", return_value={"id": 9}) as enqueue,
                    patch.object(tg_commands.job_store, "queue_position", return_value=0),
                ):
                    tg_commands.handle_command(command, *self._args())
            enqueue.assert_called_once_with(kind)


if __name__ == "__main__":
    unittest.main()
