#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from src.common import STATE_DIR, ensure_state_dirs, load_json, write_json

LEARNING_DB_PATH = STATE_DIR / "learning.db"
LEARNING_HISTORY_DIR = STATE_DIR / "learning_history"
LATEST_LEARNING_RUN_PATH = STATE_DIR / "latest_learning_run.json"

QUALITY_RANK = {
    "skip": 0,
    "seen": 1,
    "worth_watching": 2,
    "high_quality": 3,
}

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS learned_posts (
        status_url TEXT PRIMARY KEY,
        observed_at TEXT NOT NULL,
        trigger TEXT NOT NULL,
        author_handle TEXT,
        author_name TEXT,
        relative_time TEXT,
        post_text TEXT NOT NULL,
        language TEXT,
        views INTEGER NOT NULL DEFAULT 0,
        replies INTEGER NOT NULL DEFAULT 0,
        reposts INTEGER NOT NULL DEFAULT 0,
        likes INTEGER NOT NULL DEFAULT 0,
        bookmarks INTEGER NOT NULL DEFAULT 0,
        engagement_score REAL NOT NULL DEFAULT 0,
        quality_label TEXT NOT NULL DEFAULT 'seen',
        quality_score REAL NOT NULL DEFAULT 0,
        format_guess TEXT,
        hook_type TEXT,
        style_summary TEXT,
        structure_pattern TEXT,
        why_it_works TEXT,
        imitation_takeaway TEXT,
        innovation_direction TEXT,
        quality_reason TEXT,
        raw_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS learning_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        observed_at TEXT NOT NULL,
        trigger TEXT NOT NULL,
        status TEXT NOT NULL,
        scanned_count INTEGER NOT NULL DEFAULT 0,
        analyzed_count INTEGER NOT NULL DEFAULT 0,
        saved_count INTEGER NOT NULL DEFAULT 0,
        high_quality_count INTEGER NOT NULL DEFAULT 0,
        worth_watching_count INTEGER NOT NULL DEFAULT 0,
        total_cost_cny REAL NOT NULL DEFAULT 0,
        summary TEXT,
        raw_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_learned_posts_observed_at ON learned_posts(observed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_learned_posts_quality ON learned_posts(quality_label, quality_score DESC, engagement_score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_learning_runs_observed_at ON learning_runs(observed_at DESC)",
]


# Expected columns per table, used for forward-only migrations. Each entry maps
# column_name -> column definition (without the column name prefix).
# Keep this in sync with the CREATE TABLE statements in SCHEMA.
EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "learned_posts": {
        "status_url": "TEXT PRIMARY KEY",
        "observed_at": "TEXT NOT NULL DEFAULT ''",
        "trigger": "TEXT NOT NULL DEFAULT 'schedule'",
        "author_handle": "TEXT",
        "author_name": "TEXT",
        "relative_time": "TEXT",
        "post_text": "TEXT NOT NULL DEFAULT ''",
        "language": "TEXT",
        "views": "INTEGER NOT NULL DEFAULT 0",
        "replies": "INTEGER NOT NULL DEFAULT 0",
        "reposts": "INTEGER NOT NULL DEFAULT 0",
        "likes": "INTEGER NOT NULL DEFAULT 0",
        "bookmarks": "INTEGER NOT NULL DEFAULT 0",
        "engagement_score": "REAL NOT NULL DEFAULT 0",
        "quality_label": "TEXT NOT NULL DEFAULT 'seen'",
        "quality_score": "REAL NOT NULL DEFAULT 0",
        "format_guess": "TEXT",
        "hook_type": "TEXT",
        "style_summary": "TEXT",
        "structure_pattern": "TEXT",
        "why_it_works": "TEXT",
        "imitation_takeaway": "TEXT",
        "innovation_direction": "TEXT",
        "quality_reason": "TEXT",
        "raw_json": "TEXT NOT NULL DEFAULT ''",
    },
    "learning_runs": {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "observed_at": "TEXT NOT NULL DEFAULT ''",
        "trigger": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT ''",
        "scanned_count": "INTEGER NOT NULL DEFAULT 0",
        "analyzed_count": "INTEGER NOT NULL DEFAULT 0",
        "saved_count": "INTEGER NOT NULL DEFAULT 0",
        "high_quality_count": "INTEGER NOT NULL DEFAULT 0",
        "worth_watching_count": "INTEGER NOT NULL DEFAULT 0",
        "total_cost_cny": "REAL NOT NULL DEFAULT 0",
        "summary": "TEXT",
        "raw_json": "TEXT NOT NULL DEFAULT ''",
    },
}


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Add any columns present in EXPECTED_COLUMNS but missing from the live table.

    SQLite's ``CREATE TABLE IF NOT EXISTS`` does not add new columns to a
    pre-existing table, so any column added to SCHEMA in a later release will
    never be applied to production databases without an explicit ALTER TABLE.
    """
    for table, columns in EXPECTED_COLUMNS.items():
        try:
            existing_rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.OperationalError:
            # Table doesn't exist yet (SCHEMA creation may have failed earlier);
            # skip silently — the CREATE TABLE step will handle it on next run.
            continue
        existing = {row[1] for row in existing_rows}
        for col_name, col_def in columns.items():
            if col_name in existing:
                continue
            # PRIMARY KEY / AUTOINCREMENT columns cannot be added via
            # ALTER TABLE; they must exist from initial CREATE. Skip them.
            upper_def = col_def.upper()
            if "PRIMARY KEY" in upper_def or "AUTOINCREMENT" in upper_def:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
            except sqlite3.OperationalError:
                # Race or unsupported default expression; ignore — next
                # invocation will retry if needed.
                pass


def ensure_learning_storage() -> None:
    ensure_state_dirs()
    LEARNING_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(LEARNING_DB_PATH, timeout=10)) as conn:
        with conn:
            for statement in SCHEMA:
                conn.execute(statement)
            _migrate_schema(conn)


def db_connect() -> sqlite3.Connection:
    ensure_learning_storage()
    conn = sqlite3.connect(LEARNING_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def learning_history_path_for(timestamp_label: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", timestamp_label)
    return LEARNING_HISTORY_DIR / f"{safe}.json"


def _best_label(old_label: str, new_label: str) -> str:
    return old_label if QUALITY_RANK.get(old_label, 0) >= QUALITY_RANK.get(new_label, 0) else new_label


def upsert_learning_post(post: dict) -> None:
    ensure_learning_storage()
    url = str(post.get("status_url") or "").strip()
    if not url:
        return

    with closing(db_connect()) as conn:
        with conn:
            row = conn.execute("SELECT * FROM learned_posts WHERE status_url = ?", (url,)).fetchone()
            current = dict(row) if row else {}

            merged = {
                "status_url": url,
                "observed_at": str(post.get("observed_at") or current.get("observed_at") or ""),
                "trigger": str(post.get("trigger") or current.get("trigger") or "schedule"),
                "author_handle": str(post.get("author_handle") or current.get("author_handle") or ""),
                "author_name": str(post.get("author_name") or current.get("author_name") or ""),
                "relative_time": str(post.get("relative_time") or current.get("relative_time") or ""),
                "post_text": str(post.get("post_text") or current.get("post_text") or ""),
                "language": str(post.get("language") or current.get("language") or ""),
                "views": max(int(post.get("views") or 0), int(current.get("views") or 0)),
                "replies": max(int(post.get("replies") or 0), int(current.get("replies") or 0)),
                "reposts": max(int(post.get("reposts") or 0), int(current.get("reposts") or 0)),
                "likes": max(int(post.get("likes") or 0), int(current.get("likes") or 0)),
                "bookmarks": max(int(post.get("bookmarks") or 0), int(current.get("bookmarks") or 0)),
                "engagement_score": max(float(post.get("engagement_score") or 0.0), float(current.get("engagement_score") or 0.0)),
                "quality_label": _best_label(str(current.get("quality_label") or "seen"), str(post.get("quality_label") or "seen")),
                "quality_score": max(float(post.get("quality_score") or 0.0), float(current.get("quality_score") or 0.0)),
                "format_guess": str(post.get("format_guess") or current.get("format_guess") or ""),
                "hook_type": str(post.get("hook_type") or current.get("hook_type") or ""),
                "style_summary": str(post.get("style_summary") or current.get("style_summary") or ""),
                "structure_pattern": str(post.get("structure_pattern") or current.get("structure_pattern") or ""),
                "why_it_works": str(post.get("why_it_works") or current.get("why_it_works") or ""),
                "imitation_takeaway": str(post.get("imitation_takeaway") or current.get("imitation_takeaway") or ""),
                "innovation_direction": str(post.get("innovation_direction") or current.get("innovation_direction") or ""),
                "quality_reason": str(post.get("quality_reason") or current.get("quality_reason") or ""),
                "raw_json": json.dumps(post.get("raw") or current.get("raw_json") or {}, ensure_ascii=False),
            }

            conn.execute(
                """
                INSERT INTO learned_posts (
                    status_url, observed_at, trigger, author_handle, author_name, relative_time,
                    post_text, language, views, replies, reposts, likes, bookmarks,
                    engagement_score, quality_label, quality_score, format_guess, hook_type,
                    style_summary, structure_pattern, why_it_works, imitation_takeaway,
                    innovation_direction, quality_reason, raw_json
                ) VALUES (
                    :status_url, :observed_at, :trigger, :author_handle, :author_name, :relative_time,
                    :post_text, :language, :views, :replies, :reposts, :likes, :bookmarks,
                    :engagement_score, :quality_label, :quality_score, :format_guess, :hook_type,
                    :style_summary, :structure_pattern, :why_it_works, :imitation_takeaway,
                    :innovation_direction, :quality_reason, :raw_json
                )
                ON CONFLICT(status_url) DO UPDATE SET
                    observed_at=excluded.observed_at,
                    trigger=excluded.trigger,
                    author_handle=excluded.author_handle,
                    author_name=excluded.author_name,
                    relative_time=excluded.relative_time,
                    post_text=excluded.post_text,
                    language=excluded.language,
                    views=excluded.views,
                    replies=excluded.replies,
                    reposts=excluded.reposts,
                    likes=excluded.likes,
                    bookmarks=excluded.bookmarks,
                    engagement_score=excluded.engagement_score,
                    quality_label=excluded.quality_label,
                    quality_score=excluded.quality_score,
                    format_guess=excluded.format_guess,
                    hook_type=excluded.hook_type,
                    style_summary=excluded.style_summary,
                    structure_pattern=excluded.structure_pattern,
                    why_it_works=excluded.why_it_works,
                    imitation_takeaway=excluded.imitation_takeaway,
                    innovation_direction=excluded.innovation_direction,
                    quality_reason=excluded.quality_reason,
                    raw_json=excluded.raw_json
                """,
                merged,
            )


def record_learning_run(record: dict) -> None:
    ensure_learning_storage()
    write_json(LATEST_LEARNING_RUN_PATH, record)
    stamp = str(record.get("stamp") or datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"))
    write_json(learning_history_path_for(stamp), record)
    with closing(db_connect()) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO learning_runs (
                    observed_at, trigger, status, scanned_count, analyzed_count,
                    saved_count, high_quality_count, worth_watching_count, total_cost_cny,
                    summary, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.get("time_beijing", ""),
                    record.get("trigger", ""),
                    record.get("status", ""),
                    int(record.get("scanned_count") or 0),
                    int(record.get("analyzed_count") or 0),
                    int(record.get("saved_count") or 0),
                    int(record.get("high_quality_count") or 0),
                    int(record.get("worth_watching_count") or 0),
                    float(record.get("total_cost_cny") or 0.0),
                    str(record.get("summary") or ""),
                    json.dumps(record, ensure_ascii=False),
                ),
            )


def learning_counts() -> dict:
    ensure_learning_storage()
    with closing(db_connect()) as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN quality_label = 'high_quality' THEN 1 ELSE 0 END) AS high_quality,
                SUM(CASE WHEN quality_label = 'worth_watching' THEN 1 ELSE 0 END) AS worth_watching
            FROM learned_posts
            """
        ).fetchone()
    latest = load_json(LATEST_LEARNING_RUN_PATH, {})
    return {
        "total": int((row["total"] if row else 0) or 0),
        "high_quality": int((row["high_quality"] if row else 0) or 0),
        "worth_watching": int((row["worth_watching"] if row else 0) or 0),
        "latest_time": str(latest.get("time_beijing") or ""),
        "latest_status": str(latest.get("status") or ""),
    }


def recent_learning_references(limit: int = 5) -> list[dict]:
    ensure_learning_storage()
    with closing(db_connect()) as conn:
        rows = conn.execute(
            """
            SELECT
                status_url, author_handle, author_name, post_text, views, replies, reposts, likes,
                engagement_score, quality_label, quality_score, format_guess, hook_type,
                style_summary, structure_pattern, why_it_works, imitation_takeaway,
                innovation_direction, quality_reason
            FROM learned_posts
            WHERE quality_label IN ('high_quality', 'worth_watching')
            ORDER BY quality_score DESC, engagement_score DESC, observed_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(row) for row in rows]


def top_learning_posts(limit: int = 3) -> list[dict]:
    ensure_learning_storage()
    with closing(db_connect()) as conn:
        rows = conn.execute(
            """
            SELECT status_url, author_handle, post_text, views, replies, reposts, likes,
                   quality_label, quality_score, why_it_works
            FROM learned_posts
            WHERE quality_label IN ('high_quality', 'worth_watching')
            ORDER BY quality_score DESC, engagement_score DESC, observed_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(row) for row in rows]
