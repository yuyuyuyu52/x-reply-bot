"""Sanity checks for revisit.needs_revisit eligibility logic.

These pin down the kind=post vs kind=reply branches separately so changes to
the eligibility rules can't silently regress one side.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.revisit as revisit  # noqa: E402

CST = timezone(timedelta(hours=8))


def post_record(**overrides):
    base = {
        "status": "posted",
        "post_url": "https://x.com/me/status/1",
        "time_beijing": "2026-05-01 10:00:00 CST",
    }
    base.update(overrides)
    return base


def reply_record(**overrides):
    base = {
        "send_returncode": 0,
        "post_url": "https://x.com/them/status/9",
        "reply_text": "hello world",
        "time_beijing": "2026-05-01 10:00:00 CST",
    }
    base.update(overrides)
    return base


class NeedsRevisitPostTests(unittest.TestCase):
    now = datetime(2026, 5, 5, 23, 30, tzinfo=CST)

    def test_happy_path(self):
        self.assertTrue(revisit.needs_revisit(post_record(), "post", self.now))

    def test_dry_run_excluded(self):
        self.assertFalse(revisit.needs_revisit(post_record(dry_run=True), "post", self.now))

    def test_not_posted_excluded(self):
        self.assertFalse(revisit.needs_revisit(post_record(status="send_failed"), "post", self.now))

    def test_under_24h_excluded(self):
        recent = self.now - timedelta(hours=12)
        rec = post_record(time_beijing=recent.strftime("%Y-%m-%d %H:%M:%S CST"))
        self.assertFalse(revisit.needs_revisit(rec, "post", self.now))

    def test_already_filled_excluded(self):
        rec = post_record(engagement_24h={"metrics": {"views": 100}})
        self.assertFalse(revisit.needs_revisit(rec, "post", self.now))

    def test_failed_excluded(self):
        rec = post_record(engagement_24h={"failed": True, "attempts": 1})
        self.assertFalse(revisit.needs_revisit(rec, "post", self.now))

    def test_max_attempts_excluded(self):
        rec = post_record(engagement_24h={"attempts": revisit.MAX_ATTEMPTS})
        self.assertFalse(revisit.needs_revisit(rec, "post", self.now))


class NeedsRevisitReplyTests(unittest.TestCase):
    now = datetime(2026, 5, 5, 23, 30, tzinfo=CST)

    def test_happy_path(self):
        self.assertTrue(revisit.needs_revisit(reply_record(), "reply", self.now))

    def test_zero_returncode_recognized(self):
        # Regression: `int(rc or 1)` would treat 0 as failure. The fix uses
        # explicit `is None` + `int(rc) != 0`.
        rec = reply_record(send_returncode=0)
        self.assertTrue(revisit.needs_revisit(rec, "reply", self.now))

    def test_nonzero_returncode_excluded(self):
        rec = reply_record(send_returncode=1)
        self.assertFalse(revisit.needs_revisit(rec, "reply", self.now))

    def test_missing_returncode_excluded(self):
        rec = reply_record()
        rec.pop("send_returncode")
        self.assertFalse(revisit.needs_revisit(rec, "reply", self.now))

    def test_empty_reply_text_excluded(self):
        self.assertFalse(revisit.needs_revisit(reply_record(reply_text=""), "reply", self.now))

    def test_legacy_reply_field_accepted(self):
        # Older replies stored the text as `reply` rather than `reply_text`.
        rec = reply_record()
        rec.pop("reply_text")
        rec["reply"] = "legacy field"
        self.assertTrue(revisit.needs_revisit(rec, "reply", self.now))


class ParseRecordTimeTests(unittest.TestCase):
    def test_beijing_format(self):
        dt = revisit.parse_record_time({"time_beijing": "2026-05-04 02:36:54 CST"})
        self.assertEqual(dt, datetime(2026, 5, 4, 2, 36, 54, tzinfo=CST))

    def test_iso_utc(self):
        dt = revisit.parse_record_time({"time": "2026-05-03T17:17:27.376852+00:00"})
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_missing(self):
        self.assertIsNone(revisit.parse_record_time({}))


if __name__ == "__main__":
    unittest.main()
