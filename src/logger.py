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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "state" / "logs"
LOG_PATH = LOG_DIR / "x-reply-bot.log"

_initialized = False


def init_logging(*, debug: bool = False) -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # File handler — everything
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
