from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ConfigManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env_path = self.root / ".env"
        self.state_dir = self.root / "state"
        self.log_dir = self.state_dir / "logs"
        self.state_dir.mkdir()
        self.log_dir.mkdir()
        self.env_path.write_text(
            "\n".join(
                [
                    'X_REPLY_MODEL="qwen3.5-flash"',
                    "# keep this comment",
                    'X_POST_DAILY_LIMIT="2"',
                    'UNKNOWN_KEEP="yes"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.cm = importlib.import_module("src.config_manager")
        self.patches = [
            patch.object(self.cm, "ENV_PATH", self.env_path),
            patch.object(self.cm, "STATE_DIR", self.state_dir),
            patch.object(self.cm, "LOG_DIR", self.log_dir),
            patch.object(self.cm, "PENDING_PATH", self.state_dir / "config_pending.json"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_mask_secret_values_in_get(self):
        self.env_path.write_text('X_REPLY_API_KEY="sk-1234567890abcdef"\n', encoding="utf-8")
        text = self.cm.config_get_text("X_REPLY_API_KEY")
        self.assertIn("X_REPLY_API_KEY", text)
        self.assertIn("sk-1", text)
        self.assertIn("cdef", text)
        self.assertNotIn("1234567890ab", text)

    def test_low_risk_set_writes_env_without_duplicate_and_preserves_comments(self):
        backup = self.cm.set_env_value("X_POST_DAILY_LIMIT", "3")
        content = self.env_path.read_text(encoding="utf-8")
        self.assertTrue(backup.exists())
        self.assertEqual(content.count("X_POST_DAILY_LIMIT="), 1)
        self.assertIn('X_POST_DAILY_LIMIT="3"', content)
        self.assertIn("# keep this comment", content)
        self.assertIn('UNKNOWN_KEEP="yes"', content)

    def test_sensitive_set_creates_pending_without_writing_env(self):
        result = self.cm.stage_or_apply_config("X_REPLY_MODEL", "other-model", apply_func=lambda *_: None)
        self.assertEqual(result["status"], "pending")
        self.assertIn("id", result)
        self.assertIn('X_REPLY_MODEL="qwen3.5-flash"', self.env_path.read_text(encoding="utf-8"))
        pending = json.loads((self.state_dir / "config_pending.json").read_text(encoding="utf-8"))
        self.assertEqual(pending["items"][0]["key"], "X_REPLY_MODEL")

    def test_confirm_applies_pending_sensitive_change(self):
        staged = self.cm.stage_or_apply_config("X_REPLY_MODEL", "other-model", apply_func=lambda *_: None)
        calls = []
        result = self.cm.confirm_pending_config(staged["id"], apply_func=lambda backup, key: calls.append((backup, key)))
        self.assertEqual(result["status"], "applied")
        self.assertIn('X_REPLY_MODEL="other-model"', self.env_path.read_text(encoding="utf-8"))
        self.assertEqual(calls[0][1], "X_REPLY_MODEL")
        pending = json.loads((self.state_dir / "config_pending.json").read_text(encoding="utf-8"))
        self.assertEqual(pending["items"], [])

    def test_cancel_removes_pending_sensitive_change(self):
        staged = self.cm.stage_or_apply_config("X_REPLY_MODEL", "other-model", apply_func=lambda *_: None)
        result = self.cm.cancel_pending_config(staged["id"])
        self.assertEqual(result["status"], "cancelled")
        pending = json.loads((self.state_dir / "config_pending.json").read_text(encoding="utf-8"))
        self.assertEqual(pending["items"], [])

    def test_invalid_numeric_value_does_not_write_env(self):
        with self.assertRaises(ValueError):
            self.cm.stage_or_apply_config("X_POST_DAILY_LIMIT", "zero", apply_func=lambda *_: None)
        self.assertIn('X_POST_DAILY_LIMIT="2"', self.env_path.read_text(encoding="utf-8"))

    def test_hours_list_validation_normalizes_values(self):
        self.cm.stage_or_apply_config("X_POST_SCHEDULE_HOURS", "19, 11,11", apply_func=lambda *_: None)
        self.assertIn('X_POST_SCHEDULE_HOURS="11,19"', self.env_path.read_text(encoding="utf-8"))

    def test_unset_removes_active_key_and_preserves_comment(self):
        self.cm.unset_config("X_POST_DAILY_LIMIT", apply_func=lambda *_: None)
        content = self.env_path.read_text(encoding="utf-8")
        self.assertNotIn("X_POST_DAILY_LIMIT=", content)
        self.assertIn("# keep this comment", content)

    def test_process_control_vars_are_not_telegram_configurable(self):
        for key in ["X_REPLY_PYTHON", "X_REPLY_TMUX_SESSION"]:
            with self.assertRaises(KeyError):
                self.cm.get_spec(key)


if __name__ == "__main__":
    unittest.main()
