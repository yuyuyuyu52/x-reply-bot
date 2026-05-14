# Persistent Job Queue Design

## Context

The bot daemon currently keeps one in-memory `run_proc` and starts jobs directly with `subprocess.Popen`. This caused two operational problems:

- A long-running or blocked child can hold the single daemon slot indefinitely.
- Child output captured through an unread `stdout=PIPE` can fill the OS pipe buffer and block the child before the daemon reads it.

The immediate mitigation is to write child output to per-job log files and enforce a timeout. The target design is a small persistent job system backed by SQLite, so scheduled, Telegram, and manual jobs have durable lifecycle state.

## Goals

- Persist every scheduled or operator-triggered job in `state/jobs.db`.
- Keep browser/CDP work serialized: one running job at a time.
- Preserve current schedule semantics, including carry-over behavior when a slot fires while another job is running.
- Make `/status` report current running job, queued jobs, recent failures, and upcoming schedule slots from durable state.
- Recover cleanly after daemon restart: stale `running` jobs are marked `interrupted`, and queued jobs remain eligible.
- Avoid subprocess pipe deadlocks by writing every job's stdout/stderr to a job log file.
- Provide bounded failure handling: timeout, terminate, kill, exit-code capture, and short Telegram failure summaries.

## Non-Goals

- No concurrent job execution in the first version.
- No web UI.
- No complex retry policy in the first version.
- No change to the business logic inside `run_once.py`, `post_once.py`, learning, revisit, or hotspot scripts.
- No migration of historical run records into the job database.

## Architecture

The daemon becomes a coordinator with three responsibilities:

1. Compute schedule slots and enqueue due jobs.
2. Poll Telegram and enqueue requested jobs.
3. Tick the job runner, which claims and supervises one queued job at a time.

The job runner owns subprocess lifecycle. It starts jobs, writes output to `state/logs/job-<id>.log`, records pid and timestamps, enforces timeout, and finalizes status.

The job store owns the SQLite schema and all lifecycle transitions. It exposes small operations rather than letting daemon code write SQL directly.

```text
schedule loop ─┐
               ├─> src/job_store.py ── state/jobs.db
telegram cmds ─┘             ▲
                             │
                       src/job_runner.py ── subprocess ── state/logs/job-<id>.log
                             │
                             ▼
                       src/reporters.py (/status, failure summaries)
```

## Data Model

Create `state/jobs.db` with a single `jobs` table.

```text
jobs
- id INTEGER PRIMARY KEY AUTOINCREMENT
- kind TEXT NOT NULL
- label TEXT NOT NULL
- command_json TEXT NOT NULL
- trigger TEXT NOT NULL
- status TEXT NOT NULL
- priority INTEGER NOT NULL DEFAULT 100
- slot_key TEXT NOT NULL DEFAULT ''
- scheduled_for TEXT NOT NULL DEFAULT ''
- started_at TEXT NOT NULL DEFAULT ''
- finished_at TEXT NOT NULL DEFAULT ''
- pid INTEGER NOT NULL DEFAULT 0
- exit_code INTEGER
- output_path TEXT NOT NULL DEFAULT ''
- attempts INTEGER NOT NULL DEFAULT 0
- max_attempts INTEGER NOT NULL DEFAULT 1
- timeout_seconds INTEGER NOT NULL DEFAULT 3600
- error_summary TEXT NOT NULL DEFAULT ''
- created_at TEXT NOT NULL
- updated_at TEXT NOT NULL
```

Indexes:

- Unique partial index on non-empty `slot_key` to prevent duplicate schedule slots.
- `(status, priority, created_at)` for claiming the next job.
- `(updated_at)` for recent status and cleanup queries.

Allowed statuses:

- `queued`
- `running`
- `succeeded`
- `failed`
- `timed_out`
- `cancelled`
- `interrupted`

## Job Kinds

Create `src/job_specs.py` as the mapping from logical job kind to executable command.

```text
reply       -> run_once.py
post        -> post_once.py
post_dry    -> post_once.py --dry-run
learn       -> src/learning/observe.py
revisit     -> src/learning/revisit.py
hotspot     -> discover_hotspots.py
update      -> scripts/update_bot.sh
```

Every spec includes:

- `kind`
- `label`
- command builder
- default timeout
- default priority
- whether it uses Python entrypoint or shell entrypoint

All Python job commands keep the existing `--trigger {schedule|manual|telegram}` convention.

## Scheduling Semantics

The schedule loop no longer starts jobs directly. When a slot is due, it calls `enqueue_once(...)`.

Example slot keys:

```text
schedule:reply:2026-05-14T10
schedule:post:2026-05-14T11
schedule:learn:2026-05-14T09:45
schedule:revisit:2026-05-14T23:30
schedule:hotspot:2026-05-14T07:30
```

If a slot fires while a job is running, it still becomes a queued row. This replaces the fragile in-memory carry-over arithmetic. Deduplication through `slot_key` prevents duplicate enqueue on daemon restart.

Learning, revisit, and hotspot guard windows still apply before enqueue:

- Learning does not enqueue if reply or post is inside its guard window.
- Revisit only enqueues inside the night window and obeys its guard.
- Hotspot obeys its configured time and guard.

Post daily limit checks remain before scheduled post enqueue.

## Runner Lifecycle

On every daemon tick:

1. Recover stale running jobs if needed.
2. If a job is running, check timeout and process exit.
3. If no job is running, claim the next queued job and start it.

Claim order:

```text
ORDER BY priority ASC, created_at ASC
```

First-version priority values:

```text
telegram/manual immediate jobs: 10
scheduled reply/post/hotspot/revisit: 50
scheduled learning: 80
```

The runner records:

- `started_at`
- `pid`
- `output_path`
- `attempts`
- `status=running`

When the process exits:

- exit code `0` -> `succeeded`
- non-zero exit code -> `failed`
- timeout path -> `timed_out`

For timeout:

1. Send `SIGTERM`.
2. Wait 10 seconds.
3. Send `SIGKILL` if still alive.
4. Finalize with `status=timed_out`.

## Restart Recovery

On daemon startup, `recover_running_jobs()` checks rows with `status=running`.

- If `pid` is no longer alive, mark `interrupted`.
- If `pid` is alive but belongs to a previous daemon-owned child, mark `interrupted` and leave process termination to operator scripts. This avoids the new daemon adopting unknown process state.

Queued jobs remain queued.

## Telegram Behavior

Commands that start work enqueue jobs instead of directly spawning subprocesses.

Examples:

- `/run` enqueues `reply` with `trigger=telegram`.
- `/post_once` enqueues `post` with `trigger=telegram`.
- `/post_dry_run` enqueues `post_dry` with `trigger=telegram`.
- `/learn_once` enqueues `learn` with `trigger=telegram`.
- `/revisit_once` enqueues `revisit` with `trigger=telegram`.
- `/hotspot_discover` enqueues `hotspot` with `trigger=telegram`.
- `/update` enqueues `update` with `trigger=telegram`.

If a job is already running, Telegram commands should still enqueue unless the command is explicitly unsafe to queue. The response changes from "当前已有任务在执行" to "已加入队列" with the job id and queue position.

## Status Reporting

`/status` should include:

- Current active job: id, label, trigger, elapsed time.
- Queued jobs: count plus the next few labels.
- Recent failed/timed-out/interrupted jobs.
- Next computed schedule slots.
- Existing latest reply summary from `state/latest_run.json`.

When a schedule slot has already been queued, status should show it as queued rather than pretending the next slot is in the past.

## Failure Notifications

On `failed`, `timed_out`, or `interrupted`, send a Telegram notification with:

- job id
- label
- trigger
- status
- exit code if available
- elapsed time
- log path
- last 1500 characters from job output

User-facing text remains Chinese.

## File Responsibilities

### `src/job_store.py`

Owns SQLite schema and state transitions.

Public API:

- `init_job_store()`
- `enqueue_job(spec, trigger, slot_key="", scheduled_for="", priority=None, timeout_seconds=None) -> dict`
- `claim_next_job(now) -> dict | None`
- `mark_started(job_id, pid, output_path, started_at)`
- `mark_finished(job_id, status, exit_code, finished_at, error_summary="")`
- `mark_timed_out(job_id, finished_at, error_summary)`
- `recover_running_jobs(now) -> list[dict]`
- `active_job() -> dict | None`
- `queued_jobs(limit=10) -> list[dict]`
- `recent_jobs(statuses, limit=5) -> list[dict]`
- `queue_position(job_id) -> int`

### `src/job_specs.py`

Defines supported job kinds and command construction.

Public API:

- `JobSpec`
- `job_spec(kind: str) -> JobSpec`
- `build_job_command(spec, root, trigger) -> list[str]`

### `src/job_runner.py`

Owns subprocess lifecycle.

Public API:

- `JobRunner.tick(now) -> None`
- `JobRunner.shutdown() -> None`
- `tail_output(job, chars=1500) -> str`

### `bot_daemon.py`

Keeps the scheduling loop and Telegram polling, but delegates all process lifecycle to `JobRunner`.

### `src/telegram_commands.py`

Converts commands into queued jobs and formats queue acknowledgements.

### `src/reporters.py`

Reads job store state for `/status` and failure summaries.

## Testing Strategy

Use `unittest`, matching the current test suite style.

Tests:

- Job store schema initializes idempotently.
- Enqueue creates a queued row with expected command and trigger.
- Duplicate non-empty slot key does not create duplicate scheduled jobs.
- Claim order respects priority then created time.
- Started and finished transitions persist pid, timestamps, exit code, and status.
- Restart recovery marks stale running jobs as interrupted.
- Runner writes stdout/stderr to file, not pipe.
- Runner marks non-zero exit as failed.
- Runner marks long-running process as timed_out and terminates it.
- Telegram commands enqueue jobs and return queue acknowledgements.
- Daemon schedule loop enqueues due slots without starting subprocesses directly.
- `/status` reports running and queued jobs.

## Rollout Plan

Implement in small steps:

1. Add store/spec/runner modules and tests.
2. Wire daemon scheduled jobs through queue while keeping one-at-a-time execution.
3. Wire Telegram commands through queue.
4. Update `/status`.
5. Remove obsolete in-memory `run_proc` lifecycle code.
6. Keep the existing per-job log-file behavior and timeout as part of the runner.

## Operator Notes

After deployment, restart the daemon once so the new runner takes over. Existing `state/latest_*.json` files remain unchanged and continue to feed summaries. The new `state/jobs.db` starts empty and records jobs from the first post-deploy enqueue onward.
