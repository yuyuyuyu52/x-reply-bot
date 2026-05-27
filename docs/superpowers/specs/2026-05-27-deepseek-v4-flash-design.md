# DeepSeek V4 Flash Design

## Goal

Support DeepSeek V4 Flash as a first-class OpenAI-compatible LLM backend while keeping the default behavior tuned for short bot tasks.

## Requirements

- Operators can configure `deepseek-v4-flash` with `X_REPLY_BASE_URL="https://api.deepseek.com"`.
- Both DeepSeek thinking modes are supported through `.env`.
- The default DeepSeek thinking mode is `disabled`.
- Existing OpenAI-compatible and Anthropic-compatible providers keep their current behavior.
- Cost reporting recognizes `deepseek-v4-flash` instead of reporting zero cost.
- Telegram `/config` exposes the DeepSeek-specific settings.

## Architecture

DeepSeek remains on the existing OpenAI-compatible path in `src/llm.py`; no separate provider branch is needed. The request builder detects DeepSeek by base URL or model prefix, then adds DeepSeek's `thinking` object and optional `reasoning_effort`. It also avoids sending provider-specific parameters intended for other OpenAI-compatible services.

Cost estimation adds a DeepSeek V4 Flash rate path. DeepSeek publishes USD token prices, while this project reports CNY, so a configurable `X_REPLY_USD_CNY_RATE` converts the estimate into the existing CNY shape.

## Configuration

- `X_REPLY_DEEPSEEK_THINKING`: `disabled` or `enabled`, default `disabled`.
- `X_REPLY_DEEPSEEK_REASONING_EFFORT`: `high` or `max`, default `high`; only sent when thinking is enabled.
- `X_REPLY_USD_CNY_RATE`: numeric conversion rate, default `7.2`.

## Testing

Unit tests cover request payloads for disabled and enabled thinking, DeepSeek cost estimation, and non-DeepSeek request compatibility.
