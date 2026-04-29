# X Reply Bot

Standalone X auto-reply flow using `browser-harness` plus your own
OpenAI-compatible API key.

## What It Does

1. Reuse your existing logged-in Chrome tab/profile.
2. Open `x.com/home`, scan feed posts, build a shortlist.
3. Use AI to reject ads, spam, and non Chinese/English posts, then pick one.
4. Read the full status page.
5. Call your own LLM API to write a short reply.
6. Post the reply through the browser.

This is a standalone bot.

## Requirements

- Logged-in X session in Chrome.
- Chrome CDP reachable at one of:
  - `http://127.0.0.1:9222`
  - `http://10.0.0.175:9223`
  - or set `X_REPLY_CDP_URL`
- `browser-harness` installed:
  - default path: `/home/will/.local/bin/browser-harness`
  - override with `BROWSER_HARNESS_BIN`
- A compatible LLM API:
  - `X_REPLY_API_KEY`
  - `X_REPLY_BASE_URL`
  - `X_REPLY_MODEL`

`OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL` also work as fallbacks.

This directory already includes a local `.env` configured for DashScope
compatible mode with `qwen3.5-flash`.

`qwen3.5-flash` is the official DashScope-compatible model name. `qwen3.5flash`
will not match the documented model id.

## Quick Start

```bash
cd /home/will/x-reply-bot
bash start_bot.sh
```

Check status:

```bash
cd /home/will/x-reply-bot
bash status_bot.sh
```

Telegram commands from your phone:

```text
/run
/status
/post_once
/post_dry_run
/post_status
/learn_once
/learn_status
```

Stop it:

```bash
cd /home/will/x-reply-bot
bash stop_bot.sh
```

## Step By Step

Prepare a post:

```bash
python3 prepare_post.py
```

`prepare_post.py` now does two stages:
- Browser stage: collect a shortlist from the X feed.
- AI stage: reject ads/spam, keep only Chinese/English posts, and pick one
  candidate URL from the shortlist.

Generate a reply:

```bash
python3 generate_reply.py
```

Send a reply:

```bash
python3 send_reply.py --reply "your reply here"
```

Run one immediate cycle without background mode:

```bash
python3 run_once.py
```

Add a proactive post topic:

```bash
python3 post_topics.py --add "很多 AI 产品最后输的不是模型，而是把用户折腾到不想再打开。"
```

List proactive post topics:

```bash
python3 post_topics.py
```

Run one proactive post dry run:

```bash
python3 post_once.py --dry-run
```

Run one real proactive post:

```bash
python3 post_once.py
```

Run one learning pass:

```bash
python3 observe_feed.py
```

Proactive post scheduler defaults:
- Beijing time `11:00` and `19:00`
- each slot sleeps a deterministic random `0-1800` seconds
- daily limit `2`
- only runs when `state/post_topics.json` still has `pending` topics

Optional env overrides in `.env`:
- `X_POST_SCHEDULE_HOURS=11,19`
- `X_POST_JITTER_SECONDS=1800`
- `X_POST_DAILY_LIMIT=2`
- `X_LEARN_ENABLED=1`
- `X_LEARN_INTERVAL_SECONDS=900`
- `X_LEARN_GUARD_SECONDS=600`

Each successful cycle now saves:
- post URL
- selection reason
- post text
- reply text
- reply reason

Files:
- `state/latest_run.json`
- `state/history/*.json`
- `state/post_topics.json`
- `state/latest_post_run.json`
- `state/post_history/*.json`
- `state/learning.db`
- `state/latest_learning_run.json`
- `state/learning_history/*.json`

Run the hourly scheduler once without waiting for jitter:

```bash
bash scheduled_run.sh --no-jitter
```

If you ever want cron instead of the background bot, install it with:

```bash
bash install_cron.sh
```

Remove the cron schedule:

```bash
bash uninstall_cron.sh
```

The optional cron schedule is:
- Beijing time `07:00` through `23:00`
- once per hour
- each run sleeps a random `0-1800` seconds before starting
- overlap is blocked with `flock`

## Files

- `state/selected_post.json`: current selected post
- `state/replied_posts.json`: dedupe list
- `state/run_log.json`: recent runs
- `state/latest_run.json`: latest full reply record
- `state/history/`: per-run archived records
- `state/post_topics.json`: proactive post topic queue
- `state/latest_post_run.json`: latest proactive post record
- `state/post_history/`: proactive post archives
- `state/screenshots/`: browser screenshots
- `state/bot.pid`: background bot pid
- `state/logs/bot.log`: background bot log
- `state/logs/cron.log`: scheduler log

## Notes

- The generator is intentionally generic and uses the OpenAI-compatible
  `/chat/completions` path.
- Telegram notify is optional. Set `X_REPLY_TG_BOT_TOKEN` and
  `X_REPLY_TG_CHAT_ID` in `.env` to receive a message after successful sends.
- When Telegram is configured, the background bot also accepts `/run` and
  `/status` from the configured chat id.
- Learning mode runs in idle windows between reply jobs and proactive-post jobs.
- Learning mode studies the feed, saves high-quality posts to SQLite, and feeds
  recent learnings back into proactive-post generation as style references.
- Proactive posting MVP currently uses only the local topic queue as input.
- Telegram commands for proactive posting: `/post_once`, `/post_dry_run`,
  `/post_status`.
- Telegram commands for learning mode: `/learn_once`, `/learn_status`.
- `post_send.py` posts first, then resolves the newest status URL from the top
  of your profile timeline.
- The browser part is still deterministic and browser-harness-based.
- If X changes selectors again, you only need to update `prepare_post.py` and
  `send_reply.py`.
- Learning-mode spec: `LEARNING_MODE.md`
- Background loop: `bot_loop.sh`
- Background daemon: `bot_daemon.py`
- Proactive posting: `post_topics.py`, `post_generate.py`, `post_send.py`, `post_once.py`
- Observation and learning: `observe_feed.py`, `learning_store.py`
- Start/stop/status: `start_bot.sh`, `stop_bot.sh`, `status_bot.sh`
- Scheduler wrapper: `scheduled_run.sh`
- Cron installer/removal: `install_cron.sh`, `uninstall_cron.sh`
