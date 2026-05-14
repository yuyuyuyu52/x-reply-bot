#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Iterable

from src.common import JOBS_DB_PATH
from src.scheduling import BEIJING_TZ


FINAL_STATUSES = {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}
ACTIVE_STATUSES = {"queued", "running"}


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        label TEXT NOT NULL,
        command_json TEXT NOT NULL,
        trigger TEXT NOT NULL,
        status TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 100,
        slot_key TEXT NOT NULL DEFAULT '',
        scheduled_for TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL DEFAULT '',
        finished_at TEXT NOT NULL DEFAULT '',
        pid INTEGER NOT NULL DEFAULT 0,
        exit_code INTEGER,
        output_path TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 1,
        timeout_seconds INTEGER NOT NULL DEFAULT 3600,
        error_summary TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_slot_key ON jobs(slot_key) WHERE slot_key != ''",
    "CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, priority, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at)",
]


def _now_text(now: datetime | None = None) -> str:
    return (now or datetime.now(tz=BEIJING_TZ)).strftime("%Y-%m-%d %H:%M:%S %Z")


def _connect() -> sqlite3.Connection:
    JOBS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(JOBS_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def init_job_store() -> None:
    with closing(_connect()) as conn:
        for stmt in SCHEMA:
            conn.execute(stmt)
        conn.commit()


def _get_job(conn: sqlite3.Connection, job_id: int) -> dict:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(f"job {job_id} not found")
    return dict(row)


def _invalid_transition_error(conn: sqlite3.Connection, job_id: int, action: str, expected_status: str) -> ValueError:
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return ValueError(f"cannot {action} job {job_id}: job not found; expected {expected_status}")
    return ValueError(f"cannot {action} job {job_id}: status is {row['status']}; expected {expected_status}")


def enqueue_job(
    kind: str,
    label: str,
    command: list[str],
    trigger: str,
    *,
    slot_key: str = "",
    scheduled_for: str = "",
    priority: int = 100,
    timeout_seconds: int = 3600,
    max_attempts: int = 1,
    created_at: datetime | None = None,
) -> dict:
    init_job_store()
    now_text = _now_text(created_at)
    command_json = json.dumps(command, ensure_ascii=False)
    with closing(_connect()) as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO jobs (
                    kind, label, command_json, trigger, status, priority, slot_key,
                    scheduled_for, max_attempts, timeout_seconds, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    label,
                    command_json,
                    trigger,
                    priority,
                    slot_key,
                    scheduled_for,
                    max_attempts,
                    timeout_seconds,
                    now_text,
                    now_text,
                ),
            )
            conn.commit()
            return _get_job(conn, int(cur.lastrowid))
        except sqlite3.IntegrityError:
            if not slot_key:
                raise
            row = conn.execute("SELECT * FROM jobs WHERE slot_key = ?", (slot_key,)).fetchone()
            if row is None:
                raise
            return dict(row)


def claim_next_job(now: datetime | None = None) -> dict | None:
    init_job_store()
    now_text = _now_text(now)
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'queued'
            ORDER BY priority ASC, created_at ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        job_id = int(row["id"])
        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (now_text, job_id),
        )
        conn.commit()
        return _get_job(conn, job_id)


def mark_started(job_id: int, pid: int, output_path: str | Path, started_at: datetime | None = None) -> dict:
    now_text = _now_text(started_at)
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            UPDATE jobs
            SET pid = ?, output_path = ?, started_at = ?, updated_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (pid, str(output_path), now_text, now_text, job_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise _invalid_transition_error(conn, job_id, "mark started", "running")
        conn.commit()
        return _get_job(conn, job_id)


def mark_finished(
    job_id: int,
    status: str,
    exit_code: int | None,
    finished_at: datetime | None = None,
    error_summary: str = "",
) -> dict:
    if status not in FINAL_STATUSES:
        raise ValueError(f"invalid final status: {status}")
    now_text = _now_text(finished_at)
    with closing(_connect()) as conn:
        cur = conn.execute(
            """
            UPDATE jobs
            SET status = ?, exit_code = ?, finished_at = ?, error_summary = ?, updated_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (status, exit_code, now_text, error_summary, now_text, job_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise _invalid_transition_error(conn, job_id, f"mark finished as {status}", "running")
        conn.commit()
        return _get_job(conn, job_id)


def mark_timed_out(job_id: int, finished_at: datetime | None = None, error_summary: str = "") -> dict:
    return mark_finished(job_id, "timed_out", None, finished_at, error_summary)


def active_job() -> dict | None:
    init_job_store()
    with closing(_connect()) as conn:
        return _row_to_dict(conn.execute("SELECT * FROM jobs WHERE status = 'running' ORDER BY updated_at DESC LIMIT 1").fetchone())


def queued_jobs(limit: int = 10) -> list[dict]:
    init_job_store()
    with closing(_connect()) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY priority ASC, created_at ASC LIMIT ?",
                (limit,),
            )
        ]


def recent_jobs(statuses: Iterable[str], limit: int = 5) -> list[dict]:
    init_job_store()
    status_list = list(statuses)
    if not status_list:
        return []
    placeholders = ",".join("?" for _ in status_list)
    with closing(_connect()) as conn:
        return [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY updated_at DESC LIMIT ?",
                (*status_list, limit),
            )
        ]


def has_pending_or_running(kind: str, trigger: str | None = None) -> bool:
    init_job_store()
    sql = "SELECT 1 FROM jobs WHERE kind = ? AND status IN ('queued', 'running')"
    params: list[object] = [kind]
    if trigger is not None:
        sql += " AND trigger = ?"
        params.append(trigger)
    sql += " LIMIT 1"
    with closing(_connect()) as conn:
        return conn.execute(sql, params).fetchone() is not None


def queue_position(job_id: int) -> int:
    queued = queued_jobs(limit=1000)
    for index, job in enumerate(queued, start=1):
        if int(job["id"]) == int(job_id):
            return index
    return 0


def recover_running_jobs(now: datetime | None = None) -> list[dict]:
    init_job_store()
    now_text = _now_text(now)
    with closing(_connect()) as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM jobs WHERE status = 'running'")]
        for row in rows:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'interrupted', finished_at = ?, error_summary = ?, updated_at = ?
                WHERE id = ?
                """,
                (now_text, "daemon restarted while job was running", now_text, int(row["id"])),
            )
        conn.commit()
    return rows
