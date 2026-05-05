#!/usr/bin/env python3
"""Telegram notification and command helpers.

Extracted from common.py to isolate Telegram concerns.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from src.common import env_first


def telegram_token() -> str:
    return env_first("X_REPLY_TG_BOT_TOKEN", "TELEGRAM_BOT_TOKEN")


def telegram_chat_id() -> str:
    return env_first("X_REPLY_TG_CHAT_ID", "TELEGRAM_CHAT_ID")


def telegram_enabled() -> bool:
    return bool(telegram_token() and telegram_chat_id())


def telegram_notify(text: str) -> dict:
    token = telegram_token()
    chat_id = telegram_chat_id()
    if not token or not chat_id:
        raise RuntimeError("Missing X_REPLY_TG_BOT_TOKEN/TELEGRAM_BOT_TOKEN or X_REPLY_TG_CHAT_ID/TELEGRAM_CHAT_ID.")

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def telegram_set_commands(commands: list[dict], scope: dict | None = None) -> dict:
    token = telegram_token()
    if not token:
        raise RuntimeError("Missing X_REPLY_TG_BOT_TOKEN/TELEGRAM_BOT_TOKEN.")

    payload: dict = {
        "commands": commands,
    }
    if scope:
        payload["scope"] = scope

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/setMyCommands",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def telegram_get_commands(scope: dict | None = None) -> dict:
    token = telegram_token()
    if not token:
        raise RuntimeError("Missing X_REPLY_TG_BOT_TOKEN/TELEGRAM_BOT_TOKEN.")

    payload: dict = {}
    if scope:
        payload["scope"] = scope

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getMyCommands",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def tg_api(method: str, params: dict | None = None, timeout: int = 30) -> dict:
    """Low-level Telegram Bot API call (used by daemon for getUpdates etc.)."""
    token = telegram_token()
    if not token:
        raise RuntimeError("telegram not configured")
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
