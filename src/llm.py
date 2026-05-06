#!/usr/bin/env python3
"""LLM client: chat_completion, chat_text_result, chat_json_result, cost estimation.

Extracted from common.py to isolate LLM concerns from I/O and browser logic.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from src.common import env_first, model_name, base_url, api_key
from src.logger import get_logger

logger = get_logger(__name__)


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
            retryable = attempt < 3 and should_retry_http_error(exc.code, detail)
            logger.warning("%s HTTP %d attempt=%d/%d retryable=%s detail=%s", label, exc.code, attempt + 1, 4, retryable, detail[:300])
            if retryable:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"{label} transport error: {exc}")
            logger.warning("%s transport error attempt=%d/%d: %s", label, attempt + 1, 4, exc)
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

    m = model_name()
    prompt_chars = sum(len(str(item.get("content") or "")) for item in messages)
    logger.info("chat_completion start provider=%s model=%s messages=%d prompt_chars=%d temp=%.2f max_tokens=%s",
                 provider_mode(), m, len(messages), prompt_chars, temperature, max_tokens)

    t0 = time.time()
    if provider_mode() == "anthropic":
        data = anthropic_completion(messages, temperature=temperature, max_tokens=max_tokens)
    else:
        data = _openai_completion(messages, temperature=temperature, max_tokens=max_tokens)

    return data


def _openai_completion(
    messages: list[dict],
    *,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> dict:
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
            "Authorization": f"Bearer {api_key()}",
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

    effective_max_tokens = int(max_tokens or 2048)
    payload: dict = {
        "model": model_name(),
        "messages": converted_messages,
        "temperature": temperature,
        "max_tokens": effective_max_tokens,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)

    if effective_max_tokens >= 1024:
        payload["thinking"] = {
            "type": "enabled",
            "budget_tokens": min(effective_max_tokens // 2, 4096),
        }

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
    stop_reason = str(data.get("stop_reason") or "").strip()
    if not stop_reason:
        first_choice = (data.get("choices") or [{}])[0]
        stop_reason = str(first_choice.get("finish_reason") or "").strip()
    if provider_mode() == "anthropic":
        content_blocks = data.get("content") or []
        content = "\n".join(
            str(block.get("text") or "").strip()
            for block in content_blocks
            if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text") or "").strip()
        ).strip()
        if not content:
            has_thinking = any(
                isinstance(block, dict) and block.get("type") == "thinking"
                for block in content_blocks
            )
            if has_thinking and stop_reason == "max_tokens":
                raise RuntimeError(
                    "LLM_BUDGET_EXHAUSTED: thinking-only response, "
                    f"stop_reason=max_tokens id={data.get('id')} model={data.get('model')}"
                )
    else:
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(
                f"No choices in response: id={data.get('id')} model={data.get('model')}"
            )
        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if content:
            content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.S).strip()
    if not content:
        raise RuntimeError(
            f"Empty model response: id={data.get('id')} stop_reason={stop_reason} model={data.get('model')}"
        )
    usage = extract_usage(data)
    cost = estimate_cost(usage, data.get("model") or model_name())
    logger.info("chat_text_result done model=%s prompt=%d completion=%d total=%d cost_cny=%.6f content_len=%d",
                 cost["model"], usage["prompt_tokens"], usage["completion_tokens"],
                 usage["total_tokens"], cost["total_cost"], len(content))
    return {
        "content": content,
        "usage": usage,
        "cost": cost,
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
            exc_str = str(exc)
            extra_instruction = "上一次输出不是合法、完整、闭合的 JSON。现在只输出一个 JSON 对象，不要 markdown，不要解释，不要额外文本。"
            if "Could not find JSON object" in exc_str or "JSONDecodeError" in exc_str or "Expecting" in exc_str:
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
            if "LLM_BUDGET_EXHAUSTED" in exc_str:
                retry_max_tokens = min(8000, max(retry_max_tokens * 2, 2048))
            else:
                retry_max_tokens = min(8000, retry_max_tokens + 500)

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
