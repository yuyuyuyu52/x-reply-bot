# DeepSeek V4 Flash Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add configurable DeepSeek V4 Flash support with disabled thinking as the default.

**Architecture:** Keep DeepSeek on the existing OpenAI-compatible client path. Add small helpers for DeepSeek detection/configuration, apply those helpers while building request payloads, and extend cost estimation with DeepSeek rates converted to CNY.

**Tech Stack:** Python stdlib, pytest/unittest, existing `.env` and Telegram config registry.

---

## File Structure

- Modify `src/llm.py`: DeepSeek detection, request payload options, cost rates.
- Modify `src/common.py`: lazy re-exports for any new LLM helpers used by tests or callers.
- Modify `src/config_manager.py`: Telegram `/config` registry entries.
- Modify `.env.example`, `README.md`, and `DEPLOY.md`: DeepSeek configuration examples.
- Modify `tests/unit/test_llm.py` and `tests/unit/test_estimate_cost.py`: regression tests.
- Modify `CHANGELOG.md`: user-visible configuration entry.

### Task 1: DeepSeek Request Payload

- [ ] **Step 1: Write failing tests**

Add tests in `tests/unit/test_llm.py` that monkeypatch `post_json_with_retries`, set `X_REPLY_BASE_URL=https://api.deepseek.com`, `X_REPLY_MODEL=deepseek-v4-flash`, and assert:

```python
payload["thinking"] == {"type": "disabled"}
"reasoning_effort" not in payload
"reasoning_split" not in payload
```

Also add an enabled-mode test that sets `X_REPLY_DEEPSEEK_THINKING=enabled` and `X_REPLY_DEEPSEEK_REASONING_EFFORT=max`, then asserts:

```python
payload["thinking"] == {"type": "enabled"}
payload["reasoning_effort"] == "max"
```

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
pytest tests/unit/test_llm.py -q
```

Expected: new DeepSeek payload tests fail because the helper/request behavior does not exist yet.

- [ ] **Step 3: Implement minimal payload support**

In `src/llm.py`, add helpers for DeepSeek detection and environment-backed thinking settings. In `_openai_completion`, add DeepSeek payload fields only for DeepSeek requests and keep `reasoning_split` only for non-DeepSeek requests.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
pytest tests/unit/test_llm.py -q
```

Expected: payload tests pass with existing tests.

### Task 2: Cost Estimation

- [ ] **Step 1: Write failing tests**

Add `deepseek-v4-flash` tests in `tests/unit/test_estimate_cost.py` and versioned-model coverage in `tests/unit/test_llm.py`.

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
pytest tests/unit/test_estimate_cost.py tests/unit/test_llm.py -q
```

Expected: DeepSeek cost tests fail because the model currently costs zero.

- [ ] **Step 3: Implement minimal cost support**

Add DeepSeek rates for cache-miss input and output. Convert USD to CNY using `X_REPLY_USD_CNY_RATE`, defaulting to `7.2`.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
pytest tests/unit/test_estimate_cost.py tests/unit/test_llm.py -q
```

Expected: all targeted unit tests pass.

### Task 3: Operator Configuration and Docs

- [ ] **Step 1: Add config registry entries**

Add `X_REPLY_DEEPSEEK_THINKING`, `X_REPLY_DEEPSEEK_REASONING_EFFORT`, and `X_REPLY_USD_CNY_RATE` to `src/config_manager.py`.

- [ ] **Step 2: Update examples and docs**

Update `.env.example`, `README.md`, and `DEPLOY.md` with the DeepSeek V4 Flash configuration.

- [ ] **Step 3: Update changelog**

Add one `Added` line under `## [Unreleased]`.

- [ ] **Step 4: Verify**

Run:

```bash
pytest tests/unit/test_estimate_cost.py tests/unit/test_llm.py tests/unit/test_config_manager.py -q
python3 -m py_compile src/llm.py src/config_manager.py
```

Expected: targeted tests and compile checks pass.
