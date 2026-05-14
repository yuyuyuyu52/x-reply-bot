from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.job_specs import build_job_command, job_spec


class JobSpecsTests(unittest.TestCase):
    def test_reply_spec_builds_python_command_with_trigger(self):
        spec = job_spec("reply")
        cmd = build_job_command(spec, ROOT, "telegram")
        self.assertEqual(spec.label, "run_once.py")
        self.assertEqual(spec.priority, 10)
        self.assertEqual(cmd[1:], [str(ROOT / "run_once.py"), "--trigger", "telegram"])

    def test_scheduled_learning_uses_lower_priority(self):
        spec = job_spec("learn", trigger="schedule")
        self.assertEqual(spec.priority, 80)
        self.assertEqual(spec.label, "src/learning/observe.py")

    def test_post_dry_adds_dry_run_flag(self):
        spec = job_spec("post_dry")
        cmd = build_job_command(spec, ROOT, "telegram")
        self.assertEqual(cmd[1:], [str(ROOT / "post_once.py"), "--trigger", "telegram", "--dry-run"])

    def test_update_uses_shell_command(self):
        spec = job_spec("update")
        cmd = build_job_command(spec, ROOT, "telegram")
        self.assertEqual(cmd, ["/usr/bin/env", "bash", str(ROOT / "scripts/update_bot.sh")])

    def test_unknown_kind_raises(self):
        with self.assertRaises(KeyError):
            job_spec("missing")


if __name__ == "__main__":
    unittest.main()
