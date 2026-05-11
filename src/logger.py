#!/usr/bin/env python3
"""Structured logging for x-reply-bot.

Writes to state/logs/x-reply-bot.log (daily rotation, 14-day retention)
and to stderr for WARNING and above.  Set X_REPLY_LOG_LEVEL=DEBUG to see
INFO/DEBUG on stderr too.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler

from src.common import LOG_DIR

# Per-PID log file path: each process writes to its own file so daemon +
# subprocess children don't race on midnight rotation (TimedRotatingFileHandler
# renames the underlying file, which can leave a sibling process with a stale
# closed fd → ``ValueError: I/O operation on closed file``).
LOG_PATH = LOG_DIR / f"x-reply-bot.{os.getpid()}.log"
LOG_CURRENT_SYMLINK = LOG_DIR / "x-reply-bot.current.log"

_initialized = False


def _update_current_symlink() -> None:
    """Best-effort: point ``x-reply-bot.current.log`` at this process's log
    so users have a stable path to ``tail -F``. Errors are ignored
    (filesystem doesn't support symlinks, racing process, etc.)."""
    try:
        target = LOG_PATH.name  # relative; both files live in LOG_DIR
        if LOG_CURRENT_SYMLINK.is_symlink() or LOG_CURRENT_SYMLINK.exists():
            try:
                LOG_CURRENT_SYMLINK.unlink()
            except Exception:
                return
        os.symlink(target, LOG_CURRENT_SYMLINK)
    except Exception:
        pass


def init_logging(*, debug: bool = False) -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _update_current_symlink()

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # File handler — everything. Per-PID path makes midnight rotation a
    # per-process operation, so no cross-process fd race.
    file_handler = TimedRotatingFileHandler(
        str(LOG_PATH),
        when="midnight",
        interval=1,
        backupCount=14,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)-5s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(file_handler)

    # Console handler — WARNING+ by default
    console_level = logging.DEBUG if debug else logging.WARNING
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)-5s] [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(console)


def get_logger(name: str) -> logging.Logger:
    env_level = (os.environ.get("X_REPLY_LOG_LEVEL") or "").strip().upper()
    debug = env_level == "DEBUG"
    init_logging(debug=debug)
    return logging.getLogger(name)
