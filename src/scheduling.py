#!/usr/bin/env python3
"""Pure scheduling helpers extracted from bot_daemon.py.

Decides next-fire times for the four recurring job types (reply,
proactive post, learning, revisit, hotspot) and the helpers that decide
whether a guard window applies. Free of subprocess / Telegram / state-IO
side effects so it can be imported by tests and other tools.
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def _beijing_now() -> datetime:
    """Current time anchored to Asia/Shanghai (Beijing).

    Use this instead of ``datetime.now()`` for any scheduling or
    today/hour-window decision — otherwise the daemon's behavior depends on
    the host's local timezone, which silently breaks cron windows when run
    in containers, on CI, or after a host TZ change.
    """
    return datetime.now(tz=BEIJING_TZ)


def next_scheduled_after(now: datetime) -> datetime:
    jitter_seconds = int(os.environ.get("X_REPLY_JITTER_SECONDS", "1800"))
    cursor = now.replace(minute=0, second=0, microsecond=0)
    while True:
        if cursor.hour != REVISIT_HOUR:
            random.seed(cursor.strftime("%Y%m%d%H"))
            candidate = cursor + timedelta(seconds=random.randint(0, jitter_seconds))
            if candidate > now:
                return candidate
        cursor += timedelta(hours=1)


def proactive_schedule_hours() -> list[int]:
    raw = os.environ.get("X_POST_SCHEDULE_HOURS", "09,13,17,21")
    hours: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hour = int(part)
        except ValueError:
            continue
        if 0 <= hour <= 23:
            hours.append(hour)
    return sorted(set(hours)) or [9, 13, 17, 21]


def next_proactive_after(now: datetime) -> datetime:
    jitter_seconds = int(os.environ.get("X_POST_JITTER_SECONDS", "1800"))
    hours = proactive_schedule_hours()
    base_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for day_offset in range(0, 14):
        day = base_day + timedelta(days=day_offset)
        for hour in hours:
            candidate_base = day.replace(hour=hour)
            random.seed("post-" + candidate_base.strftime("%Y%m%d%H"))
            candidate = candidate_base + timedelta(seconds=random.randint(0, jitter_seconds))
            if candidate > now:
                return candidate
    fallback = base_day + timedelta(days=1)
    return fallback.replace(hour=hours[0], minute=0, second=0, microsecond=0)


def learning_enabled() -> bool:
    return os.environ.get("X_LEARN_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def learning_interval_seconds() -> int:
    try:
        return max(300, int(os.environ.get("X_LEARN_INTERVAL_SECONDS", "900")))
    except ValueError:
        return 900


def learning_guard_seconds() -> int:
    try:
        return max(60, int(os.environ.get("X_LEARN_GUARD_SECONDS", "600")))
    except ValueError:
        return 600


def next_learning_after(now: datetime) -> datetime:
    return now + timedelta(seconds=learning_interval_seconds())


REVISIT_HOUR = 0
REVISIT_WINDOW_START_HOUR = REVISIT_HOUR  # compatibility for report window keys
REVISIT_WINDOW_END_HOUR = 1


def in_revisit_window(now: datetime) -> bool:
    """True iff `now` is inside the daily midnight revisit hour."""
    return now.hour == REVISIT_HOUR


def next_revisit_after(now: datetime) -> datetime:
    """Next daily revisit slot at Beijing 00:00 strictly after `now`."""
    midnight = now.replace(hour=REVISIT_HOUR, minute=0, second=0, microsecond=0)
    if midnight > now:
        return midnight
    return midnight + timedelta(days=1)


def revisit_guard_seconds() -> int:
    # Don't start revisit if the next reply or post slot is too close. Reply
    # skips the 00:00 hour, so this mainly prevents overlap with late jobs.
    return 600


def hotspot_enabled() -> bool:
    return os.environ.get("X_HOTSPOT_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def hotspot_schedule_time() -> tuple[int, int]:
    raw = os.environ.get("X_HOTSPOT_SCHEDULE_TIME", "07:30").strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        return 7, 30
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return 7, 30


def hotspot_guard_seconds() -> int:
    try:
        return max(60, int(os.environ.get("X_HOTSPOT_GUARD_SECONDS", "600")))
    except ValueError:
        return 600


def next_hotspot_after(now: datetime) -> datetime:
    hour, minute = hotspot_schedule_time()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate > now:
        return candidate
    return candidate + timedelta(days=1)
