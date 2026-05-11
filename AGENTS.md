# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Overview

Standalone Python bot that drives a logged-in Chrome session on `x.com` to: (1) reply to feed posts, (2) post proactively from a topic queue, (3) silently observe-and-learn from the feed in idle windows. All browser interaction goes through an external `browser-harness` CLI talking to Chrome over CDP. All LLM calls go through an OpenAI-compatible (or Anthropic-compatible) `/chat/completions`-style endpoint configured via `.env`.

User-facing strings (Telegram messages, prompts, notifications) are in Chinese — preserve this when editing.

## Commands

Single-shot Python entrypoints (run from repo root, after `.env` is populated):

```bash
python3 prepare_post.py          # browser scan + AI selection → state/selected_post.json
python3 generate_reply.py        # LLM reply generation, prints JSON on stdout
python3 send_reply.py --reply "..."   # post the reply via browser
python3 run_once.py              # full reply cycle (prepare → generate → send), used by daemon
python3 post_once.py [--dry-run] # one proactive post (uses post_topics queue)
python3 src/learning/observe.py  # one learning pass into state/learning.db
python3 post_topics.py [--add "…"]   # list / append topic queue entries
python3 sync_tg_commands.py      # push the /run /status /post_* /learn_* /revisit_* command list to Telegram
```

Background daemon (long-running, schedules all three job types):

```bash
bash start_bot.sh    # tmux session "x-reply-bot" running bot_daemon.py
bash status_bot.sh
bash stop_bot.sh
bash scheduled_run.sh [--no-jitter]   # one cron-style run; alternative to the daemon
bash install_cron.sh / uninstall_cron.sh   # install/remove the hourly cron job
```

Note: the shell scripts derive the repo root from their own location via `scripts/_common.sh` (which also loads `.env` without overriding parent-shell vars), so they work in any deployment directory — no more hardcoded `/home/will/...`. They still assume `tmux` + `flock` on the daemon host (Linux production); on macOS dev machines they will fail at the first `tmux`/`flock` call, which is fine since you generally only run individual Python entrypoints there. Override `X_REPLY_PYTHON`, `X_REPLY_TMUX_SESSION`, `X_REPLY_TZ`, or `BROWSER_HARNESS_BIN` in `.env` when the defaults don't match the host.

There is no test suite, no linter, and no build step. `__pycache__/` is the only build artifact.

## Architecture

### Four job types, one daemon

`bot_daemon.py` is the long-running scheduler. It computes four independent next-fire times and runs at most one job at a time:

1. **Reply job** (`run_once.py`) — hourly between Beijing 07:00–23:00, with a deterministic per-hour jitter seeded by `YYYYMMDDHH`.
2. **Proactive post job** (`post_once.py`) — at the hours in `X_POST_SCHEDULE_HOURS` (default `11,19`), gated by `X_POST_DAILY_LIMIT` and the `pending` count in `state/post_topics.json`.
3. **Learning job** (`src/learning/observe.py`) — runs in the gaps; refuses to start if the next reply or post slot is within `X_LEARN_GUARD_SECONDS`.
4. **Revisit job** (`src/learning/revisit.py`) — only fires inside the 23:00–07:00 night window, every 30 min; scans `state/post_history/` (proactive posts) and `state/history/` (replies) for items ≥24h old and writes back `engagement_24h` metrics. For replies, we open the original post URL, scroll the thread, and locate our own nested reply by exact `reply_text` match (with author-handle confirmation). Failures retry up to 3 attempts before being marked `failed`.

The daemon also long-polls Telegram (`getUpdates`) and routes commands (`/run`, `/post_once`, `/post_dry_run`, `/post_status`, `/learn_once`, `/learn_status`, `/revisit_once`, `/revisit_status`, `/event`, `/status`) into the same `start_job` path. A daily cost summary is sent once after 23:00, and a 24h engagement digest is sent once per night window after the first revisit batch completes. Concurrency is enforced by `fcntl.flock` on `state/bot.lock` (daemon) and `state/run_once.lock` / `state/post_once.lock` (jobs).

Schedule slot accounting: when a job finishes, the daemon checks whether *another* slot fired during the run and "carries over" by setting that slot to fire immediately, instead of dropping it (`carry_over_*` logic in `bot_daemon.main`). This matters when editing the loop — naïve recomputation will silently skip slots.

### The reply pipeline (`run_once.py`)

`run_once.py` orchestrates three subprocesses sequentially and writes one consolidated record:

1. `prepare_post.py` — runs `browser-harness` to scrape a shortlist from `x.com/home`, then asks the LLM to pick one post that isn't an ad / spam / non-CN-EN. Writes `state/selected_post.json` (with a `selection_id` and `reason`). On `ai_rejected_all_candidates` / `no_suitable_feed_candidates` it exits non-zero and `run_once.py` records a `skipped` log entry, **not** a failure.
2. `generate_reply.py` — reads the selected post, calls the LLM, prints a JSON blob containing `reply`, `reason`, `source_post_url`, `selection_id`, `usage`, `cost`.
3. `send_reply.py --url --reply` — posts the reply via the harness.

A consistency guard checks that `reply.source_post_url` and `reply.selection_id` match `selected.url` / `selected.selection_id` before sending — this catches stale-state bugs where `selected_post.json` was overwritten between steps. **Don't remove this check.**

`post_once.py` follows the same pattern but uses `post_generate.generate_post_plan` + `post_send.py` (which posts, then resolves the new status URL by visiting the user's profile timeline and matching the first 30 chars of the text — the post URL isn't returned by the composer).

### `common.py` is the shared spine

Almost everything imports from `common.py`:

- **State paths**: all under `state/` (gitignored). `LATEST_RUN_PATH`, `HISTORY_DIR`, `POST_HISTORY_DIR`, `POST_TOPICS_PATH`, `TELEGRAM_STATE_PATH`, etc. — use these constants instead of rebuilding paths.
- **LLM client**: `chat_text_result` / `chat_json_result` auto-switch between OpenAI-compatible and Anthropic-compatible based on whether `base_url()` contains `/anthropic`. `chat_json_result` retries with progressively stronger "output valid JSON" instructions if `parse_json_object` fails — relying on this is fine.
- **Cost accounting**: `estimate_cost` knows pricing for `qwen3.5-flash` (tiered by prompt token count) and the `MiniMax-M2.x` family. Unknown models cost 0. Every record persisted to history includes `total_cost_cny`; the daily report aggregates from `state/history/`.
- **Browser harness**: `run_harness(code, timeout)` writes Python to `browser-harness` over stdin and retries up to 3 times on transport errors, calling `restart_harness_daemon` between attempts. The harness exposes globals like `goto`, `js`, `click`, `type_text`, `screenshot`, `page_info`, `list_tabs`, `new_tab`, `switch_tab`, `wait_for_load` — these are not Python imports, they're injected into the harness exec context. CDP endpoint is auto-resolved by trying `X_REPLY_CDP_URL` → `127.0.0.1:9222` → `10.0.0.175:9223`.
- **Telegram**: `telegram_notify` / `telegram_set_commands` / `telegram_get_commands` — all no-op-ish (raise) when `X_REPLY_TG_BOT_TOKEN` + `X_REPLY_TG_CHAT_ID` aren't both set; callers gate with `telegram_enabled()`.
- **Topic queue**: `load_post_topics` / `save_post_topics` / `next_pending_post_topic` / `mark_post_topic_status`. Topic types are constrained to `VALID_POST_TOPIC_TYPES` = `{news_react, story, argument, casual}`; `normalize_post_topic` coerces unknown types to `argument`.

### Learning store

`src/learning/store.py` owns `state/learning.db` (SQLite). Schema is defined inline in the `SCHEMA` list and applied idempotently. `src/learning/observe.py` writes via `src.learning.store`; `src/post/post_generate.py` reads recent high-quality samples back as style references for proactive posts. Quality labels rank `skip < seen < worth_watching < high_quality`.

### Selectors are the fragile boundary

If `x.com` changes DOM, the files that need updating are the harness scripts embedded as f-strings in `src/reply/prepare_post.py`, `src/reply/send_reply.py`, `src/post/post_send.py`, `src/post/article_send.py`, `src/learning/observe.py`, `src/learning/revisit.py`, and `src/hotspot/discover.py::_fetch_company_x_profile`. Shared upload-image selectors live in `src/harness.py::harness_upload_image_snippet`. They use `data-testid` attributes (`tweetTextarea_0`, `tweetButton`, `SideNav_NewTweet_Button`, `AppTabBar_Profile_Link`, `article`) — keep these centralized to those locations.

## Configuration

`.env` is loaded by `common.load_env_file()` (called at the top of every entrypoint) — values *don't* override existing env vars set by the shell. Required for LLM:

- `X_REPLY_BASE_URL`, `X_REPLY_API_KEY`, `X_REPLY_MODEL` (fallback names: `OPENAI_*`, `ANTHROPIC_*`)

Optional behavior knobs (see README + LEARNING_MODE.md for full list):

- `X_REPLY_CDP_URL`, `BROWSER_HARNESS_BIN` — point at non-default harness setup
- `X_REPLY_JITTER_SECONDS` — reply-slot jitter (default 1800)
- `X_POST_SCHEDULE_HOURS` (`11,19`), `X_POST_JITTER_SECONDS` (1800), `X_POST_DAILY_LIMIT` (2)
- `X_LEARN_ENABLED` (1), `X_LEARN_INTERVAL_SECONDS` (900, min 300), `X_LEARN_GUARD_SECONDS` (600, min 60)
- `X_REPLY_TG_BOT_TOKEN`, `X_REPLY_TG_CHAT_ID` — enables Telegram notify + command intake

The model name `qwen3.5-flash` (with the dot) is the official DashScope id. `qwen3.5flash` will silently 404 from DashScope — don't "fix" the dot.

## Conventions

- All persisted records use `time_beijing` (formatted) and `date_beijing` (`%Y-%m-%d`) — the daily report aggregator filters by `date_beijing`. Don't introduce parallel `time_utc` keys; convert at the edges.
- Per-run history files are written to both `LATEST_*_PATH` (single file, overwritten) and `*_HISTORY_DIR/<stamp>.json` (append-only archive). When you add a new field to a record, write it to both places in the same call site.
- Every job type uses `--trigger {schedule|manual|telegram}` so log/history records can distinguish cron-driven runs from human-driven ones. Preserve this when adding new jobs.
- LLM JSON outputs go through `parse_json_object` which strips ```` ``` ```` fences and finds the first balanced `{...}` — prompts can be lax about extra prose, but new prompts should still ask for "only a JSON object".
- After every user-visible change (new feature, behavior change, bug fix that the operator would notice), append one line to `CHANGELOG.md` under `## [Unreleased]` in the appropriate `Added` / `Changed` / `Fixed` / `Removed` sub-section. Each line: action verb, present tense, no trailing period, optionally suffixed with `(<short-sha>)`. Skip pure refactors, test-only additions, and doc micro-edits. When a batch of `Unreleased` items reaches a natural milestone or the day ends, rename `[Unreleased]` to `[YYYY-MM-DD]` (Beijing date) and start a fresh `[Unreleased]` block above it.
