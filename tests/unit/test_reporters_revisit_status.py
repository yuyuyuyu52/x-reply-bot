from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


CST = timezone(timedelta(hours=8))


def test_revisit_counts_skip_old_reply_without_reply_url(tmp_state, monkeypatch):
    import src.reporters as reporters

    history_dir = tmp_state / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(reporters, "HISTORY_DIR", history_dir)
    monkeypatch.setattr(reporters, "POST_HISTORY_DIR", tmp_state / "post_history")
    monkeypatch.setattr(
        reporters,
        "_beijing_now",
        lambda: datetime(2026, 5, 16, 1, 0, tzinfo=CST),
    )

    (history_dir / "old-reply.json").write_text(
        json.dumps(
            {
                "send_returncode": 0,
                "post_url": "https://x.com/them/status/123",
                "reply_text": "legacy reply",
                "time_beijing": "2026-05-14 00:00:00 CST",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    counts = reporters.revisit_counts()

    assert counts["pending"] == 0
    assert counts["skipped"] == 1
