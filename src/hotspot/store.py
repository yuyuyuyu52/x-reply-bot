#!/usr/bin/env python3
"""Hotspot discovery storage – SQLite-backed dedup and query."""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.common import HOTSPOT_STORE_PATH


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def _now_beijing() -> datetime:
    return datetime.now(tz=BEIJING_TZ)


SCHEMA = [
    """\
CREATE TABLE IF NOT EXISTS hotspots (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    hn_score INTEGER NOT NULL DEFAULT 0,
    hn_descendants INTEGER NOT NULL DEFAULT 0,
    relevance_score INTEGER NOT NULL DEFAULT 0,
    relevance_reason TEXT NOT NULL DEFAULT '',
    angle TEXT NOT NULL DEFAULT '',
    cn_summary TEXT NOT NULL DEFAULT '',
    discovered_at TEXT NOT NULL DEFAULT '',
    added_to_queue INTEGER NOT NULL DEFAULT 0
);
""",
    "CREATE INDEX IF NOT EXISTS idx_hotspots_source ON hotspots(source);",
    "CREATE INDEX IF NOT EXISTS idx_hotspots_discovered ON hotspots(discovered_at);",
]


def _ensure_schema(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA:
        conn.execute(stmt)
    conn.commit()


@contextmanager
def _get_conn():
    db = HOTSPOT_STORE_PATH
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(db), timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        yield conn


def is_seen(source: str, hotspot_id: str) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("SELECT 1 FROM hotspots WHERE id = ?", (f"{source}:{hotspot_id}",))
        return cur.fetchone() is not None


def insert_hotspot(
    source: str,
    hotspot_id: str,
    title: str,
    url: str,
    hn_score: int = 0,
    hn_descendants: int = 0,
    relevance_score: int = 0,
    relevance_reason: str = "",
    angle: str = "",
    cn_summary: str = "",
) -> None:
    with _get_conn() as conn:
        now = _now_beijing().strftime("%Y-%m-%d %H:%M:%S %Z")
        conn.execute(
            """\
INSERT OR IGNORE INTO hotspots
    (id, source, title, url, hn_score, hn_descendants,
     relevance_score, relevance_reason, angle, cn_summary, discovered_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""",
            (
                f"{source}:{hotspot_id}",
                source,
                title,
                url,
                hn_score,
                hn_descendants,
                relevance_score,
                relevance_reason,
                angle,
                cn_summary,
                now,
            ),
        )
        conn.commit()


def mark_added_to_queue(source: str, hotspot_id: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE hotspots SET added_to_queue = 1 WHERE id = ?",
            (f"{source}:{hotspot_id}",),
        )
        conn.commit()


def recent_hotspots(days: int = 1, limit: int = 20) -> list[dict]:
    with _get_conn() as conn:
        cutoff = (_now_beijing() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            """\
SELECT id, source, title, url, hn_score, hn_descendants,
       relevance_score, relevance_reason, angle, cn_summary,
       discovered_at, added_to_queue
FROM hotspots
WHERE discovered_at >= ?
ORDER BY relevance_score DESC, hn_score DESC
LIMIT ?
""",
            (cutoff, limit),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "source": row["source"],
            "title": row["title"],
            "url": row["url"],
            "hn_score": row["hn_score"],
            "hn_descendants": row["hn_descendants"],
            "relevance_score": row["relevance_score"],
            "relevance_reason": row["relevance_reason"],
            "angle": row["angle"],
            "cn_summary": row["cn_summary"],
            "discovered_at": row["discovered_at"],
            "added_to_queue": row["added_to_queue"],
        }
        for row in rows
    ]


def hotspot_stats() -> dict:
    with _get_conn() as conn:
        today = _now_beijing().strftime("%Y-%m-%d")
        total = conn.execute("SELECT COUNT(*) FROM hotspots").fetchone()[0]
        added = conn.execute(
            "SELECT COUNT(*) FROM hotspots WHERE added_to_queue = 1"
        ).fetchone()[0]
        today_discovered = conn.execute(
            "SELECT COUNT(*) FROM hotspots WHERE discovered_at LIKE ?",
            (f"{today}%",),
        ).fetchone()[0]
        today_added = conn.execute(
            "SELECT COUNT(*) FROM hotspots WHERE discovered_at LIKE ? AND added_to_queue = 1",
            (f"{today}%",),
        ).fetchone()[0]
    return {
        "total_discovered": total,
        "total_added_to_queue": added,
        "today_discovered": today_discovered,
        "today_added_to_queue": today_added,
    }
