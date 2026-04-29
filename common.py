#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
SELECTED_PATH = STATE_DIR / "selected_post.json"
REPLIED_PATH = STATE_DIR / "replied_posts.json"
RUN_LOG_PATH = STATE_DIR / "run_log.json"
SCREENSHOT_DIR = STATE_DIR / "screenshots"
HISTORY_DIR = STATE_DIR / "history"
LATEST_RUN_PATH = STATE_DIR / "latest_run.json"
POST_HISTORY_DIR = STATE_DIR / "post_history"
LATEST_POST_RUN_PATH = STATE_DIR / "latest_post_run.json"
POST_TOPICS_PATH = STATE_DIR / "post_topics.json"
TELEGRAM_STATE_PATH = STATE_DIR / "telegram_state.json"
DAILY_REPORT_STATE_PATH = STATE_DIR / "daily_report_state.json"
ENV_PATH = ROOT / ".env"
VALID_POST_TOPIC_TYPES = {"news_react", "story", "argument", "casual"}

BROWSER_HARNESS = os.environ.get(
    "BROWSER_HARNESS_BIN",
    "/home/will/.local/bin/browser-harness",
)
BROWSER_HARNESS_ROOT = Path("/home/will/Developer/browser-harness")
CDP_URLS = [
    os.environ.get("X_REPLY_CDP_URL", "").strip(),
    "http://127.0.0.1:9222",
    "http://10.0.0.175:9223",
]


def ensure_state_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    POST_HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def load_env_file() -> None:
    if not ENV_PATH.exists():
        return
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return default


def model_name() -> str:
    return env_first("X_REPLY_MODEL", "OPENAI_MODEL", "ANTHROPIC_MODEL", default="MiniMax-M2.7")


def base_url() -> str:
    return env_first(
        "X_REPLY_BASE_URL",
        "OPENAI_BASE_URL",
        "ANTHROPIC_BASE_URL",
        default="https://api.minimaxi.com/v1",
    )


def api_key() -> str:
    return env_first("X_REPLY_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def provider_mode() -> str:
    url = base_url().rstrip("/").lower()
    if url.endswith("/anthropic") or "/anthropic" in url:
        return "anthropic"
    return "openai"


def should_retry_http_error(code: int, detail: str) -> bool:
    if code in {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}:
        return True
    lowered = (detail or "").lower()
    retry_markers = [
        "overloaded_error",
        "rate limit",
        "too many requests",
        "timeout",
        "timed out",
        "system error",
        "internal error",
        "downstream",
        "\"status_code\":1000",
        "\"status_code\":1001",
        "\"status_code\":1002",
        "\"status_code\":1024",
        "\"status_code\":1033",
        "\"status_code\":2056",
    ]
    return any(marker in lowered for marker in retry_markers)


def post_json_with_retries(url: str, payload: dict, headers: dict[str, str], *, label: str) -> dict:
    last_error: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"{label} API error {exc.code}: {detail}")
            if attempt < 3 and should_retry_http_error(exc.code, detail):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"{label} transport error: {exc}")
            if attempt < 3:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last_error from exc
    if last_error:
        raise last_error
    raise RuntimeError(f"{label} request failed without exception")


def parse_json_object(text: str) -> dict:
    stripped = (text or "").strip()
    if not stripped:
        raise RuntimeError("Empty JSON response.")
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    json_slice = extract_first_json_object(stripped)
    if not json_slice:
        raise RuntimeError(f"Could not find JSON object in: {stripped[:400]}")
    parsed = json.loads(json_slice)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Expected JSON object, got: {type(parsed).__name__}")
    return parsed


def extract_first_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return ""


def chat_completion(
    messages: list[dict],
    *,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> dict:
    key = api_key()
    if not key:
        raise RuntimeError("Missing ANTHROPIC_API_KEY, X_REPLY_API_KEY, or OPENAI_API_KEY.")

    if provider_mode() == "anthropic":
        return anthropic_completion(messages, temperature=temperature, max_tokens=max_tokens)

    system_parts: list[str] = []
    normalized_messages: list[dict] = []
    for item in messages:
        role = str(item.get("role") or "").strip().lower()
        content = item.get("content") or ""
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        if role == "system":
            if text.strip():
                system_parts.append(text.strip())
            continue
        normalized_messages.append(
            {
                "role": "assistant" if role == "assistant" else "user",
                "content": text,
            }
        )

    if system_parts:
        system_text = "\n\n".join(system_parts).strip()
        if normalized_messages and normalized_messages[0].get("role") == "user":
            normalized_messages[0]["content"] = f"[System Instructions]\n{system_text}\n\n[User Message]\n{normalized_messages[0]['content']}"
        else:
            normalized_messages.insert(0, {"role": "user", "content": f"[System Instructions]\n{system_text}"})

    payload: dict = {
        "model": model_name(),
        "messages": normalized_messages,
        "temperature": temperature,
        "reasoning_split": True,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return post_json_with_retries(
        base_url().rstrip("/") + "/chat/completions",
        payload,
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        label="OpenAI-compatible",
    )


def anthropic_completion(
    messages: list[dict],
    *,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> dict:
    key = api_key()
    url = base_url().rstrip("/") + "/v1/messages"
    system_parts: list[str] = []
    converted_messages: list[dict] = []

    for item in messages:
        role = str(item.get("role") or "").strip().lower()
        content = item.get("content") or ""
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        if role == "system":
            if text.strip():
                system_parts.append(text.strip())
            continue
        converted_role = "assistant" if role == "assistant" else "user"
        converted_messages.append(
            {
                "role": converted_role,
                "content": text,
            }
        )

    payload: dict = {
        "model": model_name(),
        "messages": converted_messages,
        "temperature": temperature,
        "max_tokens": int(max_tokens or 2048),
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)

    return post_json_with_retries(
        url,
        payload,
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        label="Anthropic-compatible",
    )


def extract_usage(data: dict) -> dict:
    usage = data.get("usage") or {}
    prompt_tokens = int(
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
        or usage.get("promptTokens")
        or 0
    )
    completion_tokens = int(
        usage.get("completion_tokens")
        or usage.get("output_tokens")
        or usage.get("completionTokens")
        or 0
    )
    total_tokens = int(
        usage.get("total_tokens")
        or usage.get("totalTokens")
        or (prompt_tokens + completion_tokens)
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def qwen35_flash_rates(prompt_tokens: int) -> dict:
    # Official qwen3.5-flash tiered pricing is based on single-request input tokens.
    if prompt_tokens <= 128_000:
        return {"input_per_million": 0.2, "output_per_million": 2.0}
    if prompt_tokens <= 256_000:
        return {"input_per_million": 0.8, "output_per_million": 8.0}
    return {"input_per_million": 1.2, "output_per_million": 12.0}


def estimate_cost(usage: dict, model: str | None = None) -> dict:
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    selected_model = (model or model_name()).strip()

    if selected_model == "qwen3.5-flash":
        rates = qwen35_flash_rates(prompt_tokens)
        input_cost = prompt_tokens / 1_000_000 * rates["input_per_million"]
        output_cost = completion_tokens / 1_000_000 * rates["output_per_million"]
    elif selected_model in {
        "MiniMax-M2.7",
        "MiniMax-M2.5",
        "MiniMax-M2.1",
        "MiniMax-M2",
        "M2-her",
    }:
        rates = {"input_per_million": 2.1, "output_per_million": 8.4}
        input_cost = prompt_tokens / 1_000_000 * rates["input_per_million"]
        output_cost = completion_tokens / 1_000_000 * rates["output_per_million"]
    elif selected_model in {
        "MiniMax-M2.7-highspeed",
        "MiniMax-M2.5-highspeed",
        "MiniMax-M2.1-highspeed",
    }:
        rates = {"input_per_million": 4.2, "output_per_million": 16.8}
        input_cost = prompt_tokens / 1_000_000 * rates["input_per_million"]
        output_cost = completion_tokens / 1_000_000 * rates["output_per_million"]
    else:
        rates = {"input_per_million": 0.0, "output_per_million": 0.0}
        input_cost = 0.0
        output_cost = 0.0

    total_cost = input_cost + output_cost
    return {
        "currency": "CNY",
        "model": selected_model,
        "input_per_million": rates["input_per_million"],
        "output_per_million": rates["output_per_million"],
        "input_cost": round(input_cost, 8),
        "output_cost": round(output_cost, 8),
        "total_cost": round(total_cost, 8),
    }


def chat_text_result(
    messages: list[dict],
    *,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> dict:
    data = chat_completion(messages, temperature=temperature, max_tokens=max_tokens)
    if provider_mode() == "anthropic":
        content_blocks = data.get("content") or []
        content = "\n".join(
            str(block.get("text") or "").strip()
            for block in content_blocks
            if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text") or "").strip()
        ).strip()
    else:
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"No choices in response: {data}")
        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if content:
            content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.S).strip()
    if not content:
        raise RuntimeError(f"Empty model response: {data}")
    usage = extract_usage(data)
    return {
        "content": content,
        "usage": usage,
        "cost": estimate_cost(usage, data.get("model") or model_name()),
        "raw": data,
    }


def chat_json_result(
    messages: list[dict],
    *,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    retries: int = 3,
) -> dict:
    last_error: Exception | None = None
    retry_messages = list(messages)
    retry_temperature = temperature
    retry_max_tokens = int(max_tokens or 512)

    for _ in range(retries + 1):
        result: dict | None = None
        try:
            result = chat_text_result(
                retry_messages,
                temperature=retry_temperature,
                max_tokens=retry_max_tokens,
            )
            payload = parse_json_object(result["content"])
            return {
                "content": result["content"],
                "usage": result["usage"],
                "cost": result["cost"],
                "raw": result["raw"],
                "payload": payload,
            }
        except Exception as exc:
            last_error = exc
            extra_instruction = "上一次输出不是合法、完整、闭合的 JSON。现在只输出一个 JSON 对象，不要 markdown，不要解释，不要额外文本。"
            if "Could not find JSON object" in str(exc) or "JSONDecodeError" in str(exc) or "Expecting" in str(exc):
                extra_instruction += " 控制字段长度，优先简洁，确保 JSON 完整闭合。字符串内部不要再出现未转义双引号。"
            if result and result.get("content"):
                retry_messages = list(messages) + [
                    {
                        "role": "assistant",
                        "content": result["content"][:6000],
                    },
                    {
                        "role": "user",
                        "content": extra_instruction + " 你上一条输出已经附在上面。请基于同一任务，重新输出一份合法 JSON。",
                    },
                ]
            else:
                retry_messages = list(messages) + [
                    {
                        "role": "system",
                        "content": extra_instruction,
                    }
                ]
            retry_temperature = min(retry_temperature, 0.35)
            retry_max_tokens = min(2800, retry_max_tokens + 500)

    if last_error:
        raise last_error
    raise RuntimeError("chat_json_result failed without an exception")


def chat_text(
    messages: list[dict],
    *,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> str:
    return chat_text_result(messages, temperature=temperature, max_tokens=max_tokens)["content"]


def append_log(entry: dict) -> None:
    logs = load_json(RUN_LOG_PATH, [])
    logs.append(entry)
    write_json(RUN_LOG_PATH, logs[-200:])


def append_jsonl(path: Path, entry: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def history_path_for(timestamp_label: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", timestamp_label)
    return HISTORY_DIR / f"{safe}.json"


def post_history_path_for(timestamp_label: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", timestamp_label)
    return POST_HISTORY_DIR / f"{safe}.json"


def topic_summary_text(topic: dict) -> str:
    text = str(topic.get("text") or "").strip()
    if text:
        return text
    stance = str(topic.get("stance") or "").strip()
    if stance:
        return stance
    subject = str(topic.get("subject") or "").strip()
    context = str(topic.get("event_or_context") or "").strip()
    return " / ".join(part for part in [subject, context] if part)


def normalize_post_topic(item: dict) -> dict:
    normalized = dict(item or {})
    topic_type = str(normalized.get("type") or "").strip().lower()
    if topic_type not in VALID_POST_TOPIC_TYPES:
        topic_type = "argument"
    normalized["type"] = topic_type

    for key in ["id", "text", "source", "status", "subject", "event_or_context", "stance", "evidence_hint"]:
        normalized[key] = str(normalized.get(key) or "").strip()

    if not normalized["status"]:
        normalized["status"] = "pending"
    if not normalized["source"]:
        normalized["source"] = "manual"

    if not normalized["stance"] and normalized["text"]:
        normalized["stance"] = normalized["text"]
    if not normalized["text"]:
        normalized["text"] = topic_summary_text(normalized)

    return normalized


def load_post_topics() -> dict:
    data = load_json(POST_TOPICS_PATH, {"topics": []})
    if not isinstance(data, dict):
        return {"topics": []}
    topics = data.get("topics")
    if not isinstance(topics, list):
        data["topics"] = []
    else:
        data["topics"] = [normalize_post_topic(item) for item in topics if isinstance(item, dict)]
    return data


def save_post_topics(data: dict) -> None:
    write_json(POST_TOPICS_PATH, data)


def next_pending_post_topic() -> dict | None:
    data = load_post_topics()
    for item in data.get("topics", []):
        if (item.get("status") or "pending") == "pending":
            return item
    return None


def mark_post_topic_status(topic_id: str, status: str, extra: dict | None = None) -> dict:
    data = load_post_topics()
    updated = None
    for item in data.get("topics", []):
        if str(item.get("id") or "") != topic_id:
            continue
        item["status"] = status
        if extra:
            item.update(extra)
        updated = item
        break
    save_post_topics(data)
    return updated or {}


def post_topic_summary() -> dict:
    data = load_post_topics()
    topics = data.get("topics", [])
    summary = {"pending": 0, "used": 0, "skipped": 0, "total": len(topics)}
    for item in topics:
        status = str(item.get("status") or "pending")
        if status in summary:
            summary[status] += 1
    return summary


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


def resolve_ws() -> str:
    if os.environ.get("BU_CDP_WS"):
        return os.environ["BU_CDP_WS"]
    errors = []
    for base in [u for u in CDP_URLS if u]:
        try:
            with urllib.request.urlopen(f"{base.rstrip('/')}/json/version", timeout=5) as resp:
                payload = json.loads(resp.read())
            return payload["webSocketDebuggerUrl"]
        except Exception as exc:
            errors.append(f"{base}: {exc}")
    raise RuntimeError("Could not resolve Chrome CDP websocket. Tried: " + " | ".join(errors))


def restart_harness_daemon(name: str = "x-reply-bot") -> None:
    script = (
        "import sys; "
        f"sys.path.insert(0, {json.dumps(str(BROWSER_HARNESS_ROOT))}); "
        "from admin import restart_daemon; "
        f"restart_daemon({json.dumps(name)})"
    )
    subprocess.run(
        ["python3", "-c", script],
        cwd=str(BROWSER_HARNESS_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def run_harness(code: str, timeout: int = 75) -> str:
    errors: list[str] = []
    for attempt in range(3):
        env = os.environ.copy()
        env["BU_CDP_WS"] = resolve_ws()
        env.setdefault("BU_NAME", "x-reply-bot")
        try:
            proc = subprocess.run(
                [BROWSER_HARNESS],
                input=code,
                text=True,
                capture_output=True,
                env=env,
                cwd="/home/will/Developer/browser-harness",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            err = f"browser-harness timed out after {timeout}s\nSTDOUT:\n{exc.stdout or ''}\nSTDERR:\n{exc.stderr or ''}"
            errors.append(err)
            if attempt == 2:
                raise RuntimeError(err)
            restart_harness_daemon(env.get("BU_NAME", "x-reply-bot"))
            time.sleep(2 + attempt)
            continue
        if proc.returncode == 0:
            return proc.stdout

        err = f"browser-harness exited {proc.returncode}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        errors.append(err)
        lower = f"{proc.stdout}\n{proc.stderr}".lower()
        retryable = proc.returncode < 0 or any(
            marker in lower
            for marker in [
                "websocket connection closed",
                "target closed",
                "connection reset",
                "session closed",
                "inspected target navigated or closed",
                "keepalive ping timeout",
                "no close frame received",
                "sent 1011",
            ]
        )
        if not retryable or attempt == 2:
            raise RuntimeError(err if attempt == 2 else err)
        restart_harness_daemon(env.get("BU_NAME", "x-reply-bot"))
        time.sleep(2 + attempt)

    raise RuntimeError(errors[-1] if errors else "browser-harness failed")
