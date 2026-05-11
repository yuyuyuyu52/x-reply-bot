#!/usr/bin/env python3
"""Shared helpers for post handlers (single / thread / article).

Lives outside post_once.py so the per-mode handler modules
(``src.post.thread``, ``src.post.article``) can import them without
re-creating an import cycle through ``post_once``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from src.common import (
    LATEST_POST_RUN_PATH,
    post_history_path_for,
    telegram_enabled,
    telegram_notify,
    write_json,
)


# Repo root resolved from this file's location (src/post/handlers_common.py).
ROOT = Path(__file__).resolve().parent.parent.parent


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Subprocess wrapper that injects PYTHONPATH=ROOT and cwd=ROOT."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(cmd, text=True, capture_output=True, cwd=str(ROOT), env=env)


def notify_telegram(record: dict, stamp: str, text: str) -> None:
    """Send a pre-formatted notify text and persist the result back into ``record``.

    ``text`` is built by the caller (mode-specific notify formatter) so this
    helper stays free of mode-specific imports — handlers_common cannot
    safely import the per-mode formatters without re-creating a cycle.
    """
    if not telegram_enabled():
        return
    try:
        tg_resp = telegram_notify(text)
        record["telegram_notify"] = {"ok": True, "response": tg_resp}
    except Exception as exc:
        record["telegram_notify"] = {"ok": False, "error": str(exc)}
        print(f"TELEGRAM_NOTIFY_ERROR: {exc}")
    write_json(LATEST_POST_RUN_PATH, record)
    write_json(post_history_path_for(stamp), record)


def topic_extra_update(status: str, stamp: str, dry_run: bool) -> dict:
    data = {
        "last_seen_at": stamp,
        "last_status": status,
    }
    if not dry_run and status == "used":
        data["used_at"] = stamp
    return data
