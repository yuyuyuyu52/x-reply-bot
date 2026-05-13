from __future__ import annotations

import sys
import subprocess
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import src.telegram_commands as tg_commands  # noqa: E402


class TelegramUpdateCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT
        fake_daemon = types.ModuleType("bot_daemon")
        fake_daemon.ROOT = self.root
        fake_daemon._child_env = lambda: {"PYTHONPATH": str(self.root)}
        fake_daemon.start_job = MagicMock()
        self.daemon_patcher = patch.dict(sys.modules, {"bot_daemon": fake_daemon})
        self.daemon_patcher.start()

    def tearDown(self):
        self.daemon_patcher.stop()

    def _args(self, run_proc=None):
        now = datetime(2026, 5, 12, tzinfo=timezone.utc)
        return (
            run_proc,
            now,
            now,
            now,
            now,
            now,
            "",
            "",
        )

    def test_update_command_starts_detached_update_script(self):
        proc = MagicMock()
        with patch.object(tg_commands, "_safe_notify") as notify, patch.object(
            tg_commands.subprocess, "Popen", return_value=proc
        ) as popen:
            result = tg_commands.handle_command("/update", *self._args())

        self.assertIs(result[0], proc)
        self.assertEqual(result[7], "scripts/update_bot.sh")
        notify.assert_called_once()
        self.assertIn("开始更新", notify.call_args.args[0])
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["/usr/bin/env", "bash", str(self.root / "scripts/update_bot.sh")])
        self.assertEqual(kwargs["cwd"], str(self.root))
        self.assertTrue(kwargs["start_new_session"])
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)

    def test_update_command_refuses_while_job_is_running(self):
        running_proc = MagicMock()
        running_proc.poll.return_value = None
        with patch.object(tg_commands, "_safe_notify") as notify, patch.object(
            tg_commands.subprocess, "Popen"
        ) as popen:
            result = tg_commands.handle_command("/update", *self._args(run_proc=running_proc))

        self.assertIs(result[0], running_proc)
        popen.assert_not_called()
        notify.assert_called_once()
        self.assertIn("当前已有任务", notify.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
