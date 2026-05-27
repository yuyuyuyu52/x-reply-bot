#!/usr/bin/env python3
"""Telegram-managed configuration registry and .env editor."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from src.common import ENV_PATH, LOG_DIR, ROOT, STATE_DIR, ensure_state_dirs, load_json, write_json

PENDING_PATH = STATE_DIR / "config_pending.json"


@dataclass(frozen=True)
class ConfigSpec:
    key: str
    group: str
    label: str
    kind: str = "str"
    default: str = ""
    sensitive: bool = False
    confirm: bool = False
    min_value: int | None = None


def _spec(
    key: str,
    group: str,
    label: str,
    *,
    kind: str = "str",
    default: str = "",
    sensitive: bool = False,
    confirm: bool = False,
    min_value: int | None = None,
) -> ConfigSpec:
    return ConfigSpec(key, group, label, kind, default, sensitive, confirm or sensitive, min_value)


CONFIG_SPECS: dict[str, ConfigSpec] = {
    s.key: s
    for s in [
        _spec("X_REPLY_BASE_URL", "LLM", "LLM API 地址", kind="url", sensitive=True, default="https://api.minimaxi.com/v1"),
        _spec("X_REPLY_API_KEY", "LLM", "LLM API Key", sensitive=True),
        _spec("X_REPLY_MODEL", "LLM", "LLM 模型", sensitive=True, default="MiniMax-M2.7"),
        _spec("X_REPLY_DEEPSEEK_THINKING", "LLM", "DeepSeek Thinking 模式", kind="deepseek_thinking", default="disabled"),
        _spec("X_REPLY_DEEPSEEK_REASONING_EFFORT", "LLM", "DeepSeek 推理强度", kind="deepseek_reasoning_effort", default="high"),
        _spec("X_REPLY_USD_CNY_RATE", "LLM", "美元兑人民币估算汇率", kind="float", default="7.2", min_value=0),
        _spec("OPENAI_BASE_URL", "LLM", "OpenAI 兼容 API 地址", kind="url", sensitive=True),
        _spec("OPENAI_API_KEY", "LLM", "OpenAI API Key", sensitive=True),
        _spec("OPENAI_MODEL", "LLM", "OpenAI 模型", sensitive=True),
        _spec("ANTHROPIC_BASE_URL", "LLM", "Anthropic 兼容 API 地址", kind="url", sensitive=True),
        _spec("ANTHROPIC_API_KEY", "LLM", "Anthropic API Key", sensitive=True),
        _spec("ANTHROPIC_MODEL", "LLM", "Anthropic 模型", sensitive=True),
        _spec("X_REPLY_CDP_URL", "Browser", "Chrome CDP 地址", kind="url", sensitive=True),
        _spec("BROWSER_HARNESS_BIN", "Browser", "browser-harness 可执行文件", kind="path_exec", sensitive=True),
        _spec("BROWSER_HARNESS_ROOT", "Browser", "browser-harness 项目目录", kind="path_dir", sensitive=True),
        _spec("X_REPLY_TG_BOT_TOKEN", "Telegram", "Telegram Bot Token", sensitive=True),
        _spec("X_REPLY_TG_CHAT_ID", "Telegram", "Telegram Chat ID", sensitive=True),
        _spec("X_REPLY_JITTER_SECONDS", "Reply", "回复随机延迟秒数", kind="int", default="1800", min_value=0),
        _spec("X_REPLY_ENABLE_REPOST", "Reply", "允许转发", kind="bool", default="1"),
        _spec("X_REPLY_ENABLE_QUOTE", "Reply", "允许引用", kind="bool", default="1"),
        _spec("X_REPOST_DAILY_LIMIT", "Reply", "每日转发上限", kind="int", default="1", min_value=0),
        _spec("X_POST_SCHEDULE_HOURS", "Post", "主动发帖小时", kind="hours", default="09,13,17,21"),
        _spec("X_POST_JITTER_SECONDS", "Post", "主动发帖随机延迟秒数", kind="int", default="1800", min_value=0),
        _spec("X_POST_DAILY_LIMIT", "Post", "主动发帖每日上限", kind="int", default="4", min_value=1),
        _spec("X_LEARN_ENABLED", "Learning", "观察学习开关", kind="bool", default="1"),
        _spec("X_LEARN_INTERVAL_SECONDS", "Learning", "观察学习间隔秒数", kind="int", default="900", min_value=300),
        _spec("X_LEARN_GUARD_SECONDS", "Learning", "观察学习保护间隔秒数", kind="int", default="600", min_value=60),
        _spec("X_HOTSPOT_ENABLED", "Hotspot", "热点发现开关", kind="bool", default="1"),
        _spec("X_HOTSPOT_SCHEDULE_TIME", "Hotspot", "每日热点发现时间", kind="time", default="07:30"),
        _spec("X_HOTSPOT_SOURCES", "Hotspot", "热点数据源", default="hn,producthunt,reddit,lobsters,simonw,github_trending,hf_papers,tldr_ai,openai,anthropic,google"),
        _spec("X_HOTSPOT_LLM_CANDIDATES", "Hotspot", "热点 LLM 评估候选数", kind="int", default="30", min_value=3),
        _spec("X_PRODUCT_HUNT_TOKEN", "Hotspot", "Product Hunt API Token", sensitive=True),
        _spec("X_PRODUCT_HUNT_API_KEY", "Hotspot", "Product Hunt API Key", sensitive=True),
        _spec("X_PRODUCT_HUNT_API_SECRET", "Hotspot", "Product Hunt API Secret", sensitive=True),
        _spec("X_HOTSPOT_ENABLE_X_SCRAPE", "Hotspot", "允许抓取 X 公司账号", kind="bool", default="0"),
        _spec("X_HOTSPOT_X_SCRAPE_TIMEOUT", "Hotspot", "X 抓取超时秒数", kind="int", default="25", min_value=10),
        _spec("X_HOTSPOT_GUARD_SECONDS", "Hotspot", "热点发现保护间隔秒数", kind="int", default="600", min_value=60),
        _spec("X_HOTSPOT_DAILY_LIMIT", "Hotspot", "热点每日发帖上限", kind="int", default="3", min_value=1),
        _spec("GIPHY_API_KEY", "Media", "GIPHY API Key", sensitive=True),
        _spec("UNSPLASH_ACCESS_KEY", "Media", "Unsplash Access Key", sensitive=True),
        _spec("X_REPLY_IMAGE_API_KEY", "Media", "AI 生图 API Key", sensitive=True),
        _spec("X_REPLY_IMAGE_API_URL", "Media", "AI 生图 API 地址", kind="url", sensitive=True),
        _spec("X_REPLY_IMAGE_MODEL", "Media", "AI 生图模型", sensitive=True, default="gpt-image-2"),
        _spec("X_REPLY_IMAGE_COST_CNY", "Media", "AI 生图单价", kind="float", default="0.070", min_value=0),
        _spec("X_REPLY_LOG_LEVEL", "System", "日志等级", kind="choice", default="", min_value=None),
        _spec("X_REPLY_TZ", "System", "时区", default="Asia/Shanghai"),
    ]
}

LOG_LEVELS = {"", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _parse_env_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    key = stripped.split("=", 1)[0].strip()
    return key if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key) else None


def _quote_env(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _unquote_env(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def read_env_values(path: Path | None = None) -> dict[str, str]:
    path = path or ENV_PATH
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        key = _parse_env_key(line)
        if not key:
            continue
        raw = line.strip()
        if raw.startswith("export "):
            raw = raw[len("export "):].lstrip()
        values[key] = _unquote_env(raw.split("=", 1)[1])
    return values


def _write_env_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _backup_env(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.name}.bak.{stamp}.{os.getpid()}")
    if path.exists():
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        backup.write_text("", encoding="utf-8")
    return backup


def set_env_value(key: str, value: str, path: Path | None = None) -> Path:
    path = path or ENV_PATH
    backup = _backup_env(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated: list[str] = []
    replaced = False
    for line in lines:
        if _parse_env_key(line) == key:
            if not replaced:
                updated.append(f"{key}={_quote_env(value)}")
                replaced = True
            continue
        updated.append(line)
    if not replaced:
        if updated and updated[-1].strip():
            updated.append("")
        updated.append(f"{key}={_quote_env(value)}")
    _write_env_lines(path, updated)
    return backup


def unset_env_value(key: str, path: Path | None = None) -> Path:
    path = path or ENV_PATH
    backup = _backup_env(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    _write_env_lines(path, [line for line in lines if _parse_env_key(line) != key])
    return backup


def get_spec(key: str) -> ConfigSpec:
    normalized = key.strip().upper()
    if normalized not in CONFIG_SPECS:
        raise KeyError(f"未知配置项：{key}")
    return CONFIG_SPECS[normalized]


def validate_value(spec: ConfigSpec, value: str) -> str:
    value = value.strip()
    if spec.kind == "bool":
        lowered = value.lower()
        if lowered in {"1", "true", "yes", "on", "enable", "enabled", "开", "开启"}:
            return "1"
        if lowered in {"0", "false", "no", "off", "disable", "disabled", "关", "关闭"}:
            return "0"
        raise ValueError(f"{spec.key} 需要布尔值：1/0、true/false、on/off")
    if spec.kind == "int":
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"{spec.key} 需要整数") from exc
        if spec.min_value is not None and parsed < spec.min_value:
            raise ValueError(f"{spec.key} 不能小于 {spec.min_value}")
        return str(parsed)
    if spec.kind == "float":
        try:
            parsed = float(value)
        except ValueError as exc:
            raise ValueError(f"{spec.key} 需要数字") from exc
        if spec.min_value is not None and parsed < spec.min_value:
            raise ValueError(f"{spec.key} 不能小于 {spec.min_value}")
        return str(parsed)
    if spec.kind == "deepseek_thinking":
        lowered = value.lower()
        if lowered in {"1", "true", "yes", "on", "enable", "enabled", "开", "开启"}:
            return "enabled"
        if lowered in {"0", "false", "no", "off", "disable", "disabled", "关", "关闭"}:
            return "disabled"
        raise ValueError(f"{spec.key} 只支持 enabled 或 disabled")
    if spec.kind == "deepseek_reasoning_effort":
        lowered = value.lower()
        if lowered in {"high", "max"}:
            return lowered
        if lowered == "xhigh":
            return "max"
        raise ValueError(f"{spec.key} 只支持 high 或 max")
    if spec.kind == "hours":
        hours: list[int] = []
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                hour = int(part)
            except ValueError as exc:
                raise ValueError(f"{spec.key} 需要 0-23 的小时列表，例如 11,19") from exc
            if hour < 0 or hour > 23:
                raise ValueError(f"{spec.key} 小时必须在 0-23 之间")
            hours.append(hour)
        if not hours:
            raise ValueError(f"{spec.key} 不能为空")
        return ",".join(str(h) for h in sorted(set(hours)))
    if spec.kind == "time":
        try:
            hour_text, minute_text = value.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
        except ValueError as exc:
            raise ValueError(f"{spec.key} 需要 HH:MM 格式，例如 07:30") from exc
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError(f"{spec.key} 时间必须在 00:00-23:59 之间")
        return f"{hour:02d}:{minute:02d}"
    if spec.kind == "url":
        if not re.match(r"^https?://", value):
            raise ValueError(f"{spec.key} 需要 http:// 或 https:// 地址")
        return value
    if spec.kind == "path_exec":
        p = Path(value).expanduser()
        if not p.exists() or not p.is_file() or not os.access(p, os.X_OK):
            raise ValueError(f"{spec.key} 路径不存在或不可执行：{value}")
        return str(p)
    if spec.kind == "path_dir":
        p = Path(value).expanduser()
        if not p.exists() or not p.is_dir():
            raise ValueError(f"{spec.key} 目录不存在：{value}")
        return str(p)
    if spec.kind == "choice":
        upper = value.upper()
        if upper not in LOG_LEVELS:
            raise ValueError(f"{spec.key} 只支持：DEBUG、INFO、WARNING、ERROR、CRITICAL")
        return upper
    return value


def mask_value(value: str) -> str:
    if not value:
        return "(未设置)"
    if len(value) <= 8:
        return value[0] + "***" + value[-1] if len(value) > 1 else "*"
    return value[:4] + "***" + value[-4:]


def display_value(spec: ConfigSpec, values: dict[str, str]) -> str:
    value = values.get(spec.key, os.environ.get(spec.key, spec.default))
    if spec.sensitive:
        return mask_value(value)
    return value if value != "" else "(未设置)"


def config_get_text(key: str) -> str:
    spec = get_spec(key)
    values = read_env_values()
    return f"⚙️ {spec.key}\n\n{spec.label}\n当前值：{display_value(spec, values)}"


def config_list_text() -> str:
    values = read_env_values()
    groups: dict[str, list[str]] = {}
    for spec in CONFIG_SPECS.values():
        groups.setdefault(spec.group, []).append(f"{spec.key}={display_value(spec, values)}")
    lines = ["⚙️ 配置列表"]
    for group in sorted(groups):
        lines.append("")
        lines.append(f"[{group}]")
        lines.extend(groups[group])
    return "\n".join(lines)


def _load_pending() -> dict:
    return load_json(PENDING_PATH, {"items": []})


def _save_pending(data: dict) -> None:
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_json(PENDING_PATH, data)


def _pending_id() -> str:
    return time.strftime("%Y%m%d%H%M%S") + f"-{os.getpid()}"


def pending_text() -> str:
    data = _load_pending()
    items = data.get("items") or []
    if not items:
        return "📭 当前没有等待确认的配置变更。"
    lines = ["🕓 等待确认的配置变更"]
    for item in items:
        spec = get_spec(item["key"])
        value = mask_value(item.get("value", "")) if spec.sensitive else item.get("value", "")
        action = "删除" if item.get("unset") else "设置"
        lines.append(f"{item['id']}  {action} {item['key']}={value}")
    return "\n".join(lines)


def _default_apply(backup: Path, key: str) -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(ROOT),
    }
    subprocess.Popen(
        ["/usr/bin/env", "bash", str(ROOT / "scripts/apply_config_restart.sh"), str(backup), key],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        start_new_session=True,
    )


def stage_or_apply_config(key: str, value: str, apply_func=_default_apply) -> dict:
    spec = get_spec(key)
    normalized = validate_value(spec, value)
    if spec.confirm:
        data = _load_pending()
        items = [item for item in data.get("items", []) if item.get("key") != spec.key]
        item = {
            "id": _pending_id(),
            "key": spec.key,
            "value": normalized,
            "unset": False,
            "created_at": int(time.time()),
        }
        items.append(item)
        _save_pending({"items": items})
        return {"status": "pending", "id": item["id"], "key": spec.key}
    backup = set_env_value(spec.key, normalized)
    apply_func(backup, spec.key)
    return {"status": "applied", "key": spec.key, "backup": str(backup)}


def confirm_pending_config(pending_id: str, apply_func=_default_apply) -> dict:
    data = _load_pending()
    items = data.get("items") or []
    match = next((item for item in items if item.get("id") == pending_id), None)
    if not match:
        raise KeyError(f"未找到待确认配置：{pending_id}")
    spec = get_spec(match["key"])
    if match.get("unset"):
        backup = unset_env_value(spec.key)
    else:
        backup = set_env_value(spec.key, validate_value(spec, match.get("value", "")))
    _save_pending({"items": [item for item in items if item.get("id") != pending_id]})
    apply_func(backup, spec.key)
    return {"status": "applied", "key": spec.key, "backup": str(backup)}


def cancel_pending_config(pending_id: str) -> dict:
    data = _load_pending()
    items = data.get("items") or []
    kept = [item for item in items if item.get("id") != pending_id]
    if len(kept) == len(items):
        raise KeyError(f"未找到待确认配置：{pending_id}")
    _save_pending({"items": kept})
    return {"status": "cancelled", "id": pending_id}


def unset_config(key: str, apply_func=_default_apply) -> dict:
    spec = get_spec(key)
    if spec.confirm:
        data = _load_pending()
        items = [item for item in data.get("items", []) if item.get("key") != spec.key]
        item = {
            "id": _pending_id(),
            "key": spec.key,
            "value": "",
            "unset": True,
            "created_at": int(time.time()),
        }
        items.append(item)
        _save_pending({"items": items})
        return {"status": "pending", "id": item["id"], "key": spec.key}
    backup = unset_env_value(spec.key)
    apply_func(backup, spec.key)
    return {"status": "applied", "key": spec.key, "backup": str(backup)}


def _format_apply_result(result: dict) -> str:
    if result["status"] == "pending":
        return "\n".join(
            [
                "🕓 敏感配置等待确认",
                "",
                f"配置项：{result['key']}",
                f"确认 ID：{result['id']}",
                "",
                f"确认生效：/config confirm {result['id']}",
                f"取消变更：/config cancel {result['id']}",
            ]
        )
    return f"✅ 配置已保存：{result['key']}\n\n正在检查并重启 bot，完成后会再通知你。"


def handle_config_command(args_text: str) -> str:
    try:
        parts = shlex.split(args_text or "")
    except ValueError as exc:
        return f"⚠️ 命令解析失败：{exc}"
    if not parts or parts[0] in {"help", "-h", "--help"}:
        return "\n".join(
            [
                "⚙️ 配置命令",
                "",
                "/config list",
                "/config get KEY",
                "/config set KEY VALUE",
                "/config unset KEY",
                "/config pending",
                "/config confirm <id>",
                "/config cancel <id>",
            ]
        )
    action = parts[0].lower()
    try:
        if action == "list":
            return config_list_text()
        if action == "get" and len(parts) == 2:
            return config_get_text(parts[1])
        if action == "set" and len(parts) >= 3:
            return _format_apply_result(stage_or_apply_config(parts[1], " ".join(parts[2:])))
        if action == "unset" and len(parts) == 2:
            return _format_apply_result(unset_config(parts[1]))
        if action == "pending":
            return pending_text()
        if action == "confirm" and len(parts) == 2:
            return _format_apply_result(confirm_pending_config(parts[1]))
        if action == "cancel" and len(parts) == 2:
            cancel_pending_config(parts[1])
            return "✅ 已取消待确认配置。"
    except KeyError as exc:
        return f"❌ {exc}"
    except ValueError as exc:
        return f"❌ 配置无效：{exc}"
    except Exception as exc:
        return f"❌ 配置操作失败：{exc}"
    return "⚠️ 用法错误。发送 /config help 查看命令。"


if __name__ == "__main__":
    ensure_state_dirs()
    print(handle_config_command(" ".join(sys.argv[1:])))
