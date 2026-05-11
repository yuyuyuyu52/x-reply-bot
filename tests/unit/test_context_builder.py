"""Unit tests for src.context_builder.

Covers:
- The `time_beijing` parser regression inside `scan_reviewable_entries`
  (handles CST suffix, +0800, +08:00, ISO no-space, and naive Beijing fallback).
- The cutoff `break` regression — verifies older files are NOT opened once
  the iteration crosses the cutoff (a `continue` regression would scan all
  files even past the cutoff).
- `human_feedback` exclusion.
- `build_feedback_context` double-bucket cap + early-break once both buckets
  are full (only the newest files needed should be parsed).
- `build_feedback_context` returns Chinese-prefixed prompt text.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _write_history(state_dir: Path, stamp: str, payload: dict) -> Path:
    """Drop a JSON file into HISTORY_DIR with the given stamp + payload."""
    p = state_dir / "history" / f"{stamp}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture
def cb_paths(tmp_state, monkeypatch):
    """Rebind context_builder's HISTORY_DIR / POST_HISTORY_DIR to tmp_state.

    The `tmp_state` fixture only retargets modules that expose `STATE_DIR`;
    context_builder uses local `from src.common import HISTORY_DIR`, so we
    rebind those names directly here.
    """
    import src.context_builder as cb
    monkeypatch.setattr(cb, "HISTORY_DIR", tmp_state / "history")
    monkeypatch.setattr(cb, "POST_HISTORY_DIR", tmp_state / "post_history")
    return tmp_state


# ---------------------------------------------------------------------------
# time_beijing parser regression
# ---------------------------------------------------------------------------

TIME_FORMAT_CASES = [
    # (label, time_beijing_string)
    ("cst_suffix", "2026-05-10 12:00:00 CST"),
    ("offset_no_colon", "2026-05-10 12:00:00 +0800"),
    ("offset_with_colon", "2026-05-10 12:00:00 +08:00"),
    ("iso_no_space", "2026-05-10T12:00:00+08:00"),
    ("naive_no_tz", "2026-05-10 12:00:00"),
]


@pytest.mark.parametrize("label,time_str", TIME_FORMAT_CASES)
def test_scan_reviewable_entries_accepts_time_format(cb_paths, label, time_str, monkeypatch):
    """Each supported `time_beijing` format must parse without being silently dropped.

    We pin "now" to 2026-05-11 12:00 CST so all five 2026-05-10 records fall
    within the default 3-day window.
    """
    from datetime import datetime, timedelta, timezone
    import src.context_builder as cb

    fixed = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    monkeypatch.setattr(cb, "_beijing_now", lambda: fixed)

    _write_history(
        cb_paths,
        f"20260510_{label}",
        {
            "stamp": f"20260510_{label}",
            "time_beijing": time_str,
            "reply_text": f"hello {label}",
        },
    )

    entries = cb.scan_reviewable_entries(days=3)
    stamps = [e["stamp"] for e in entries]
    assert any(label in s for s in stamps), (
        f"format {label!r} ({time_str!r}) should have been accepted; got stamps={stamps}"
    )


# ---------------------------------------------------------------------------
# Cutoff `break` regression
# ---------------------------------------------------------------------------

class CutoffBreakRegressionTests(unittest.TestCase):
    """Make sure the cutoff terminates iteration (break) rather than skipping (continue)."""

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, cb_paths, monkeypatch):
        self.tmp_state = cb_paths
        self.monkeypatch = monkeypatch

    def test_old_files_not_opened_after_cutoff(self):
        from datetime import datetime, timedelta, timezone
        import src.context_builder as cb

        fixed = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        self.monkeypatch.setattr(cb, "_beijing_now", lambda: fixed)

        # 5 files, descending stamps. Files sorted reverse=True iterate
        # newest first. With days=2 cutoff = 2026-05-09 12:00 CST:
        #   2026-05-11 -> recent
        #   2026-05-10 -> recent
        #   2026-05-09 11:00 -> PAST cutoff (just barely)
        #   2026-05-08 -> past cutoff
        #   2026-05-07 -> past cutoff
        records = [
            ("20260511_120000", "2026-05-11 12:00:00 CST"),
            ("20260510_120000", "2026-05-10 12:00:00 CST"),
            ("20260509_110000", "2026-05-09 11:00:00 CST"),
            ("20260508_120000", "2026-05-08 12:00:00 CST"),
            ("20260507_120000", "2026-05-07 12:00:00 CST"),
        ]
        for stamp, t in records:
            _write_history(self.tmp_state, stamp, {
                "stamp": stamp,
                "time_beijing": t,
                "reply_text": f"r-{stamp}",
            })

        # Count Path.read_text calls inside HISTORY_DIR only (POST_HISTORY_DIR is empty).
        original_read_text = Path.read_text
        seen: list[Path] = []

        def counting_read_text(self_, *a, **kw):
            seen.append(self_)
            return original_read_text(self_, *a, **kw)

        self.monkeypatch.setattr(Path, "read_text", counting_read_text)

        entries = cb.scan_reviewable_entries(days=2)
        stamps = {e["stamp"] for e in entries}

        # Only the two recent ones should be returned.
        self.assertEqual(stamps, {"20260511_120000", "20260510_120000"})

        # Regression: we must have STOPPED reading after hitting the first
        # past-cutoff file. The 3rd file is read (so its time is parsed and
        # checked), then break fires. So we should see exactly 3 file reads,
        # NOT 5. If the implementation regressed to `continue`, all 5 would
        # be opened.
        history_reads = [p for p in seen if p.parent.name == "history"]
        self.assertEqual(
            len(history_reads),
            3,
            f"Expected 3 reads (break after first past-cutoff file); got {len(history_reads)}: "
            f"{[p.name for p in history_reads]}",
        )

    def test_human_feedback_excluded(self):
        from datetime import datetime, timedelta, timezone
        import src.context_builder as cb

        fixed = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        self.monkeypatch.setattr(cb, "_beijing_now", lambda: fixed)

        _write_history(self.tmp_state, "20260511_100000", {
            "stamp": "20260511_100000",
            "time_beijing": "2026-05-11 10:00:00 CST",
            "reply_text": "reviewable",
        })
        _write_history(self.tmp_state, "20260511_090000", {
            "stamp": "20260511_090000",
            "time_beijing": "2026-05-11 09:00:00 CST",
            "reply_text": "already rated",
            "human_feedback": {"score": 5, "comment": "", "rated_at": "x"},
        })

        entries = cb.scan_reviewable_entries(days=3)
        stamps = {e["stamp"] for e in entries}
        self.assertIn("20260511_100000", stamps)
        self.assertNotIn(
            "20260511_090000",
            stamps,
            "records with human_feedback should be filtered out of the reviewable list",
        )


# ---------------------------------------------------------------------------
# build_feedback_context: double-bucket cap + break-early regression
# ---------------------------------------------------------------------------

class BuildFeedbackContextTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, cb_paths, monkeypatch):
        self.tmp_state = cb_paths
        self.monkeypatch = monkeypatch

    def _write_rated(self, stamp: str, score: int, text: str, comment: str = "") -> Path:
        return _write_history(self.tmp_state, stamp, {
            "stamp": stamp,
            "time_beijing": "2026-05-11 12:00:00 CST",
            "reply_text": text,
            "human_feedback": {"score": score, "comment": comment, "rated_at": "x"},
        })

    def test_returns_empty_when_no_feedback(self):
        from src.context_builder import build_feedback_context
        self.assertEqual(build_feedback_context(), "")

    def test_double_bucket_cap_and_chinese_prefix(self):
        """Final formatted output must cap at 2 good + 2 bad, with Chinese headers."""
        from src.context_builder import build_feedback_context

        # 10 good (score 5) and 10 bad (score 1). Stamps alternate so the
        # newest 10 files (sorted reverse) contain 5 good + 5 bad — both
        # buckets fill simultaneously, then the loop breaks early.
        # We pad the rank with leading zeros so lexicographic sort matches
        # numeric order.
        for i in range(20):
            rank = 99 - i  # newest = highest rank
            score = 5 if i % 2 == 0 else 1
            kind = "good" if score == 5 else "bad"
            self._write_rated(
                stamp=f"20260511_{rank:06d}",
                score=score,
                text=f"sample {kind} #{i}",
                comment=f"c{i}",
            )

        out = build_feedback_context()
        self.assertIsInstance(out, str)
        self.assertTrue(out, "feedback context should be non-empty given rated records")

        # Chinese-prefixed section headers (regression on the prompt format).
        self.assertIn("好评示例", out)
        self.assertIn("差评示例", out)

        # Final output is capped at 2 of each. Count the "[5分]" / "[1分]" lines.
        good_lines = [ln for ln in out.splitlines() if ln.startswith("- [5分]")]
        bad_lines = [ln for ln in out.splitlines() if ln.startswith("- [1分]")]
        self.assertEqual(len(good_lines), 2, f"good cap should be 2; got {good_lines}")
        self.assertEqual(len(bad_lines), 2, f"bad cap should be 2; got {bad_lines}")

    def test_breaks_once_both_buckets_full(self):
        """Regression: once both buckets reach the internal target, stop scanning.

        We write 20 alternating good/bad files. The newest 10 (which fill
        both buckets to 5 each) should be opened; the older 10 should be
        skipped. Patch json.loads to count parses.
        """
        from src.context_builder import build_feedback_context
        import src.context_builder as cb

        for i in range(20):
            rank = 99 - i
            score = 5 if i % 2 == 0 else 1
            self._write_rated(
                stamp=f"20260511_{rank:06d}",
                score=score,
                text=f"x {i}",
            )

        original_loads = json.loads
        parsed: list[str] = []

        def counting_loads(s, *a, **kw):
            parsed.append(s if isinstance(s, str) else s.decode("utf-8", "ignore"))
            return original_loads(s, *a, **kw)

        # The function uses json.loads via its own module-level binding.
        self.monkeypatch.setattr(cb.json, "loads", counting_loads)

        build_feedback_context()

        # Newest 10 fill both buckets (5 good + 5 bad alternating). Once the
        # 10th file is parsed and assigned, the next-iteration top-of-loop
        # check breaks. So we expect to have parsed exactly 10 files,
        # not all 20. A regression that forgot the break would parse 20.
        self.assertEqual(
            len(parsed),
            10,
            f"Expected to break after both buckets fill at 5+5 (10 reads); "
            f"got {len(parsed)} parses. (A `continue`-only loop would scan all 20.)",
        )

    def test_only_good_returns_only_good_section(self):
        from src.context_builder import build_feedback_context

        for i in range(3):
            self._write_rated(
                stamp=f"20260511_{99 - i:06d}",
                score=5,
                text=f"good {i}",
            )

        out = build_feedback_context()
        self.assertIn("好评示例", out)
        self.assertNotIn("差评示例", out)


if __name__ == "__main__":
    unittest.main()
