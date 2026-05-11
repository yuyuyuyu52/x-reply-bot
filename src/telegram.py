#!/usr/bin/env python3
"""Telegram notification and command helpers.

Extracted from common.py to isolate Telegram concerns.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from src.common import env_first

# Telegram caps sendMessage text at 4096 chars. Leave a small headroom.
_TG_MAX_CHARS = 4000


def _chunk_text(text: str, limit: int = _TG_MAX_CHARS) -> list[str]:
    """Split text into chunks <= limit chars, preferring newline boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # Prefer splitting at the last newline within the limit window.
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            # No newline found in window — fall back to a hard cut.
            split_at = limit
        chunk = remaining[:split_at]
        chunks.append(chunk)
        # Drop a leading newline on the remainder so it doesn't start with "\n".
        remaining = remaining[split_at:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _post_send_message(token: str, chat_id: str, text: str) -> dict:
    """POST sendMessage once, honoring a single 429 retry_after retry."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    attempts = 0
    while True:
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        body_text: str
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body_text = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # 429 (or other) error responses still carry a JSON body with retry_after.
            try:
                body_text = e.read().decode("utf-8")
            except Exception:
                body_text = ""
            try:
                err_body = json.loads(body_text) if body_text else {}
            except Exception:
                err_body = {}
            retry_after = (err_body.get("parameters") or {}).get("retry_after")
            if retry_after is not None and attempts < 1:
                wait_s = int(retry_after) + 1
                print(
                    f"[telegram] rate limited (HTTP {e.code}); sleeping {wait_s}s then retrying once",
                    file=sys.stderr,
                )
                time.sleep(wait_s)
                attempts += 1
                continue
            raise

        # Success path: Telegram may still signal 429 with HTTP 200 + ok=false.
        try:
            body = json.loads(body_text)
        except Exception:
            return {"ok": False, "raw": body_text}
        if not body.get("ok", False):
            retry_after = (body.get("parameters") or {}).get("retry_after")
            if retry_after is not None and attempts < 1:
                wait_s = int(retry_after) + 1
                print(
                    f"[telegram] rate limited (ok=false); sleeping {wait_s}s then retrying once",
                    file=sys.stderr,
                )
                time.sleep(wait_s)
                attempts += 1
                continue
        return body


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

    chunks = _chunk_text(text)
    last_response: dict = {}
    for chunk in chunks:
        last_response = _post_send_message(token, chat_id, chunk)
    return last_response


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
