from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import src.telegram_commands as tg_commands  # noqa: E402


class TelegramConfigCommandTests(unittest.TestCase):
    def setUp(self):
        fake_daemon = types.ModuleType("bot_daemon")
        fake_daemon.ROOT = ROOT
        fake_daemon._child_env = lambda: {"PYTHONPATH": str(ROOT)}
        fake_daemon.start_job = MagicMock()
        self.daemon_patcher = patch.dict(sys.modules, {"bot_daemon": fake_daemon})
        self.daemon_patcher.start()

    def tearDown(self):
        self.daemon_patcher.stop()

    def _args(self):
        now = datetime(2026, 5, 12, tzinfo=timezone.utc)
        return (None, now, now, now, now, now, "", "")

    def test_config_command_delegates_and_notifies_response(self):
        with patch.object(tg_commands, "_safe_notify") as notify, patch(
            "src.config_manager.handle_config_command", return_value="配置回复"
        ) as handler:
            result = tg_commands.handle_command("/config get X_POST_DAILY_LIMIT", *self._args())

        self.assertIsNone(result[0])
        handler.assert_called_once_with("get X_POST_DAILY_LIMIT")
        notify.assert_called_once_with("配置回复")


if __name__ == "__main__":
    unittest.main()
