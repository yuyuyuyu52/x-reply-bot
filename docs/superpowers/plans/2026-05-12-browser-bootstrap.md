# Browser Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Chrome CDP and browser-harness setup part of normal repo deployment.

**Architecture:** Keep browser control external to the Python job pipeline, but install and start the external dependencies through repo-owned shell scripts. Python defaults point at repo-local `.bin/` and `.deps/` paths, while `.env` can still override every host-specific value.

**Tech Stack:** Bash, Python 3, pytest, Chrome CDP, browser-harness, uv.

---

### Task 1: Lock Down Browser Defaults

**Files:**
- Create: `tests/unit/test_browser_setup.py`
- Modify: `src/harness.py`

- [x] **Step 1: Write failing pytest coverage**

Add tests that clear `BROWSER_HARNESS_BIN` and `BROWSER_HARNESS_ROOT`, and assert defaults resolve to `.bin/browser-harness` and `vendor/browser-harness`.

- [x] **Step 2: Verify red**

Run: `pytest tests/unit/test_browser_setup.py -q`
Expected: failure because `src.harness` still used legacy `/home/will/...` defaults and the new scripts did not exist.

- [x] **Step 3: Implement repo-local defaults**

Add repo-root constants in `src/harness.py`, prefer configured env vars, then `.bin/browser-harness`, then `PATH`, and default the harness root to `vendor/browser-harness`.

- [x] **Step 4: Verify green**

Run: `pytest tests/unit/test_browser_setup.py -q`
Expected: pass.

### Task 2: Add Deployment Scripts

**Files:**
- Create: `scripts/bootstrap_browser.sh`
- Create: `scripts/start_chrome.sh`
- Create: `scripts/status_browser.sh`
- Modify: `.gitignore`
- Modify: `.env.example`

- [x] **Step 1: Add script syntax tests**

Add `bash -n` checks for all three scripts.

- [x] **Step 2: Implement scripts**

`bootstrap_browser.sh` installs Linux dependencies, `uv`, Chrome, installs the vendored browser-harness from `vendor/browser-harness`, creates `.bin/browser-harness`, and appends missing browser values to `.env`.

`start_chrome.sh` starts Chrome with `--remote-debugging-port`, a persistent profile, and logs to `state/logs/chrome.log`.

`status_browser.sh` checks CDP and browser-harness health.

- [x] **Step 3: Verify scripts**

Run: `bash -n scripts/bootstrap_browser.sh scripts/start_chrome.sh scripts/status_browser.sh`
Expected: pass.

### Task 3: Update Operator Documentation

**Files:**
- Modify: `DEPLOY.md`
- Modify: `CHANGELOG.md`

- [x] **Step 1: Document the new deployment flow**

Update first-time install instructions to run `bootstrap_browser.sh`, then `start_chrome.sh`, then manually log in to X once.

- [x] **Step 2: Record the user-facing change**

Add an Unreleased changelog entry for browser dependency bootstrap scripts.
