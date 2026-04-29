# Observation And Learning Mode

## Goal

When the bot is not replying and not proactively posting, it should keep scanning the X home feed, identify worth-learning posts, analyze why they perform, and save those learnings for later imitation and innovation.

## Scope

- Runs only in idle windows.
- Does not reply.
- Does not post.
- Reads feed posts, scores them, and studies only a shortlist.

## Run Loop

1. Open `x.com/home`
2. Scroll the feed for several screens
3. Collect visible posts and interaction signals
4. Filter obvious spam, promo, unsupported-language noise, and self posts
5. Shortlist by engagement and discussion signals
6. Ask the model which posts are actually worth learning
7. Save:
   - raw post text
   - views / replies / reposts / likes / bookmarks
   - quality label
   - writing style summary
   - structure pattern
   - why it works
   - imitation takeaway
   - innovation direction
8. Feed recent high-quality learnings back into proactive-post generation as style references

## Quality Labels

- `high_quality`: clearly worth studying
- `worth_watching`: not top-tier, but still teaches a useful pattern
- `seen`: collected but not selected

## What Gets Stored

SQLite:
- `state/learning.db`

Latest JSON:
- `state/latest_learning_run.json`

Per-run archive:
- `state/learning_history/*.json`

## Scheduling

The daemon treats learning as a third job type:

- reply schedule keeps priority
- proactive posting keeps second priority
- observation/learning runs only in remaining idle time

Default knobs:
- `X_LEARN_ENABLED=1`
- `X_LEARN_INTERVAL_SECONDS=900`
- `X_LEARN_GUARD_SECONDS=600`

`X_LEARN_GUARD_SECONDS` means the daemon will not start a learning run if the next reply or proactive-post slot is too close.

## Telegram

New commands:

- `/learn_once`
- `/learn_status`

## Current Limits

- Quote/reply/repost breakdown is inferred from feed-visible controls. X does not always expose quote count cleanly in feed DOM, so the learning logic currently relies on the most stable visible signals: views, replies, reposts, likes, and bookmarks.
- This mode learns from feed-distributed posts, not from a global X firehose.
