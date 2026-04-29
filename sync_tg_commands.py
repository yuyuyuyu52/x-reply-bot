#!/usr/bin/env python3
from __future__ import annotations

import json

from common import (
    ensure_state_dirs,
    load_env_file,
    telegram_get_commands,
    telegram_set_commands,
)

COMMANDS = [
    {"command": "run", "description": "立即跑一轮回复"},
    {"command": "status", "description": "查看回复机器人状态"},
    {"command": "post_once", "description": "立即主动发帖"},
    {"command": "post_dry_run", "description": "生成主动发帖草稿"},
    {"command": "post_status", "description": "查看主动发帖状态"},
    {"command": "learn_once", "description": "立即观察学习一轮"},
    {"command": "learn_status", "description": "查看观察学习状态"},
]

SCOPES = [
    {"type": "default"},
    {"type": "all_private_chats"},
]


def main() -> int:
    load_env_file()
    ensure_state_dirs()
    results = []
    for scope in SCOPES:
        set_result = telegram_set_commands(COMMANDS, scope=scope)
        get_result = telegram_get_commands(scope=scope)
        results.append(
            {
                "scope": scope,
                "set_result": set_result,
                "get_result": get_result,
            }
        )
    print(json.dumps({"commands": COMMANDS, "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
