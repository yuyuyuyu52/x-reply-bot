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
        if 7 <= cursor.hour <= 23:
            random.seed(cursor.strftime("%Y%m%d%H"))
            candidate = cursor + timedelta(seconds=random.randint(0, jitter_seconds))
            if candidate > now:
                return candidate
        cursor += timedelta(hours=1)


def proactive_schedule_hours() -> list[int]:
    raw = os.environ.get("X_POST_SCHEDULE_HOURS", "11,19")
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
    return sorted(set(hours)) or [11, 19]


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


REVISIT_WINDOW_START_HOUR = 23  # inclusive
REVISIT_WINDOW_END_HOUR = 7     # exclusive
REVISIT_INTERVAL_SECONDS = 1800  # every 30 min while inside the window


def in_revisit_window(now: datetime) -> bool:
    """True iff `now` is inside the 23:00–07:00 nightly window."""
    hour = now.hour
    return hour >= REVISIT_WINDOW_START_HOUR or hour < REVISIT_WINDOW_END_HOUR


def next_revisit_after(now: datetime) -> datetime:
    """Next 30-minute slot inside the night window strictly after `now`.

    If `now` is inside the window, return `now + 30 min` (with a small floor
    to avoid immediate re-fire). If `now` is outside, return today's 23:00 if
    that's still in the future, else tomorrow's 23:00.
    """
    if in_revisit_window(now):
        return now + timedelta(seconds=REVISIT_INTERVAL_SECONDS)
    today_start = now.replace(hour=REVISIT_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    if today_start > now:
        return today_start
    return today_start + timedelta(days=1)


def revisit_guard_seconds() -> int:
    # Mirror the learning-job guard: don't start revisit if the next reply or
    # post slot is within this many seconds. Reply slots only fire 07-23 so
    # this only matters near the 07:00 boundary; small value is fine.
    return 600


def hotspot_enabled() -> bool:
    return os.environ.get("X_HOTSPOT_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def hotspot_interval_seconds() -> int:
    try:
        return max(600, int(os.environ.get("X_HOTSPOT_INTERVAL_SECONDS", "7200")))
    except ValueError:
        return 7200


def hotspot_guard_seconds() -> int:
    try:
        return max(60, int(os.environ.get("X_HOTSPOT_GUARD_SECONDS", "600")))
    except ValueError:
        return 600


def next_hotspot_after(now: datetime) -> datetime:
    return now + timedelta(seconds=hotspot_interval_seconds())
