# Persistent Job Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the daemon's in-memory subprocess state with a SQLite-backed persistent job queue and single-job runner.

**Architecture:** Add `src/job_specs.py`, `src/job_store.py`, and `src/job_runner.py`. The daemon enqueues due scheduled jobs and Telegram-triggered jobs, then calls `JobRunner.tick()` to supervise one running subprocess. Job output goes to `state/logs/job-<id>.log`; lifecycle state goes to `state/jobs.db`.

**Tech Stack:** Python standard library only: `sqlite3`, `subprocess`, `json`, `dataclasses`, `datetime`, `pathlib`, `os`, `signal`, and existing project modules.

---

## Current Worktree Note

There are existing uncommitted mitigation edits in `bot_daemon.py`, `src/reporters.py`, `src/telegram_commands.py`, and related tests. Treat those as temporary scaffolding. Integrate their useful behavior into the new runner, then remove obsolete in-memory `run_proc` helpers from `bot_daemon.py`.

Do not revert unrelated user edits. Only replace the temporary mitigation code when the new queue covers the same behavior.

## File Map

- Modify: `src/common.py`
  Add `JOBS_DB_PATH`.
- Create: `src/job_specs.py`
  Defines supported job kinds, commands, timeout, priority, and labels.
- Create: `src/job_store.py`
  Owns SQLite schema and lifecycle transitions.
- Create: `src/job_runner.py`
  Owns subprocess start, timeout, completion, and log tailing.
- Modify: `bot_daemon.py`
  Replace direct `Popen` lifecycle with schedule enqueue + runner tick.
- Modify: `src/telegram_commands.py`
  Replace start-job command handling with enqueue acknowledgements.
- Modify: `src/reporters.py`
  Make `/status` read active/queued/recent jobs from job store.
- Modify: `CHANGELOG.md`
  Add a user-facing Fixed/Changed line under `[Unreleased]`.
- Test: `tests/unit/test_job_specs.py`
- Test: `tests/unit/test_job_store.py`
- Test: `tests/unit/test_job_runner.py`
- Test: `tests/unit/test_telegram_job_queue.py`
- Test: `tests/unit/test_reporters_job_queue_status.py`
- Test: `tests/integration/test_daemon_schedule.py`

## Behavior Rule: Scheduled Backlog Cap

First version should enqueue at most one pending or running scheduled job per kind. This prevents a bad Chrome/X outage from creating a burst of stale replies. Telegram/manual commands may enqueue multiple jobs because they are explicit operator actions.

Implement with `job_store.has_pending_or_running(kind, trigger="schedule")` before scheduled enqueue. If one exists, advance the next-fire timestamp but do not enqueue another scheduled job for that kind.

---

### Task 1: Add Job Specs

**Files:**
- Create: `src/job_specs.py`
- Create: `tests/unit/test_job_specs.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_job_specs.py`:

```python
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.job_specs import build_job_command, job_spec


class JobSpecsTests(unittest.TestCase):
    def test_reply_spec_builds_python_command_with_trigger(self):
        spec = job_spec("reply")
        cmd = build_job_command(spec, ROOT, "telegram")
        self.assertEqual(spec.label, "run_once.py")
        self.assertEqual(spec.priority, 10)
        self.assertEqual(cmd[1:], [str(ROOT / "run_once.py"), "--trigger", "telegram"])

    def test_scheduled_learning_uses_lower_priority(self):
        spec = job_spec("learn", trigger="schedule")
        self.assertEqual(spec.priority, 80)
        self.assertEqual(spec.label, "src/learning/observe.py")

    def test_post_dry_adds_dry_run_flag(self):
        spec = job_spec("post_dry")
        cmd = build_job_command(spec, ROOT, "telegram")
        self.assertEqual(cmd[1:], [str(ROOT / "post_once.py"), "--trigger", "telegram", "--dry-run"])

    def test_update_uses_shell_command(self):
        spec = job_spec("update")
        cmd = build_job_command(spec, ROOT, "telegram")
        self.assertEqual(cmd, ["/usr/bin/env", "bash", str(ROOT / "scripts/update_bot.sh")])

    def test_unknown_kind_raises(self):
        with self.assertRaises(KeyError):
            job_spec("missing")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3 -m unittest tests.unit.test_job_specs -v
```

Expected: import failure for missing `src.job_specs`.

- [ ] **Step 3: Implement `src/job_specs.py`**

Create `src/job_specs.py`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class JobSpec:
    kind: str
    label: str
    entrypoint: str
    extra_args: tuple[str, ...] = ()
    shell: bool = False
    timeout_seconds: int = 3600
    priority: int = 50


def _priority(kind: str, trigger: str) -> int:
    if trigger in {"telegram", "manual"}:
        return 10
    if kind == "learn":
        return 80
    return 50


def job_spec(kind: str, trigger: str = "telegram") -> JobSpec:
    specs = {
        "reply": ("run_once.py", (), False, 3600),
        "post": ("post_once.py", (), False, 3600),
        "post_dry": ("post_once.py", ("--dry-run",), False, 3600),
        "learn": ("src/learning/observe.py", (), False, 1800),
        "revisit": ("src/learning/revisit.py", (), False, 1800),
        "hotspot": ("discover_hotspots.py", (), False, 1800),
        "update": ("scripts/update_bot.sh", (), True, 1800),
    }
    entrypoint, extra_args, shell, timeout = specs[kind]
    return JobSpec(
        kind=kind,
        label=entrypoint if not extra_args else f"{entrypoint} {' '.join(extra_args)}",
        entrypoint=entrypoint,
        extra_args=extra_args,
        shell=shell,
        timeout_seconds=timeout,
        priority=_priority(kind, trigger),
    )


def build_job_command(spec: JobSpec, root: Path, trigger: str) -> list[str]:
    if spec.shell:
        return ["/usr/bin/env", "bash", str(root / spec.entrypoint)]
    return [sys.executable, str(root / spec.entrypoint), "--trigger", trigger, *spec.extra_args]
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
python3 -m unittest tests.unit.test_job_specs -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/job_specs.py tests/unit/test_job_specs.py
git commit -m "feat: add job specifications"
```

---

### Task 2: Add SQLite Job Store

**Files:**
- Modify: `src/common.py`
- Create: `src/job_store.py`
- Create: `tests/unit/test_job_store.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_job_store.py`:

```python
from __future__ import annotations

import json
import sys
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src import job_store

CST = timezone(timedelta(hours=8))


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 5, 14, hour, minute, tzinfo=CST)


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "jobs.db"
        self.patch = patch.object(job_store, "JOBS_DB_PATH", self.db_path)
        self.patch.start()
        job_store.init_job_store()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_enqueue_and_claim_job(self):
        job = job_store.enqueue_job(
            kind="reply",
            label="run_once.py",
            command=["python", "run_once.py", "--trigger", "telegram"],
            trigger="telegram",
            created_at=at(9),
        )
        self.assertEqual(job["status"], "queued")
        self.assertEqual(json.loads(job["command_json"])[1], "run_once.py")

        claimed = job_store.claim_next_job(at(9, 1))

        self.assertEqual(claimed["id"], job["id"])
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["attempts"], 1)

    def test_duplicate_slot_key_returns_existing_job(self):
        first = job_store.enqueue_job(
            kind="reply",
            label="run_once.py",
            command=["python", "run_once.py"],
            trigger="schedule",
            slot_key="schedule:reply:2026-05-14T09",
            created_at=at(9),
        )
        second = job_store.enqueue_job(
            kind="reply",
            label="run_once.py",
            command=["python", "run_once.py"],
            trigger="schedule",
            slot_key="schedule:reply:2026-05-14T09",
            created_at=at(9, 5),
        )
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(job_store.queued_jobs(limit=10)), 1)

    def test_priority_order(self):
        low = job_store.enqueue_job("learn", "observe.py", ["python", "observe.py"], "schedule", priority=80, created_at=at(9))
        high = job_store.enqueue_job("reply", "run_once.py", ["python", "run_once.py"], "telegram", priority=10, created_at=at(9, 1))

        claimed = job_store.claim_next_job(at(9, 2))

        self.assertEqual(claimed["id"], high["id"])
        self.assertNotEqual(claimed["id"], low["id"])

    def test_mark_started_and_finished(self):
        job = job_store.enqueue_job("reply", "run_once.py", ["python", "run_once.py"], "telegram", created_at=at(9))
        claimed = job_store.claim_next_job(at(9, 1))
        job_store.mark_started(claimed["id"], pid=123, output_path="state/logs/job-1.log", started_at=at(9, 1))
        job_store.mark_finished(claimed["id"], status="succeeded", exit_code=0, finished_at=at(9, 2))

        recent = job_store.recent_jobs(["succeeded"], limit=1)
        self.assertEqual(recent[0]["pid"], 123)
        self.assertEqual(recent[0]["exit_code"], 0)
        self.assertEqual(recent[0]["status"], "succeeded")

    def test_has_pending_or_running(self):
        self.assertFalse(job_store.has_pending_or_running("reply", trigger="schedule"))
        job_store.enqueue_job("reply", "run_once.py", ["python", "run_once.py"], "schedule", created_at=at(9))
        self.assertTrue(job_store.has_pending_or_running("reply", trigger="schedule"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3 -m unittest tests.unit.test_job_store -v
```

Expected: import failure or missing attributes.

- [ ] **Step 3: Add `JOBS_DB_PATH`**

Modify `src/common.py` near the other state paths:

```python
JOBS_DB_PATH = STATE_DIR / "jobs.db"
```

- [ ] **Step 4: Implement `src/job_store.py`**

Create `src/job_store.py` with:

```python
#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Iterable

from src.common import JOBS_DB_PATH
from src.scheduling import BEIJING_TZ


FINAL_STATUSES = {"succeeded", "failed", "timed_out", "cancelled", "interrupted"}
ACTIVE_STATUSES = {"queued", "running"}


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        label TEXT NOT NULL,
        command_json TEXT NOT NULL,
        trigger TEXT NOT NULL,
        status TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 100,
        slot_key TEXT NOT NULL DEFAULT '',
        scheduled_for TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL DEFAULT '',
        finished_at TEXT NOT NULL DEFAULT '',
        pid INTEGER NOT NULL DEFAULT 0,
        exit_code INTEGER,
        output_path TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 1,
        timeout_seconds INTEGER NOT NULL DEFAULT 3600,
        error_summary TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_slot_key ON jobs(slot_key) WHERE slot_key != ''",
    "CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, priority, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at)",
]


def _now_text(now: datetime | None = None) -> str:
    return (now or datetime.now(tz=BEIJING_TZ)).strftime("%Y-%m-%d %H:%M:%S %Z")


def _connect() -> sqlite3.Connection:
    JOBS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(JOBS_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def init_job_store() -> None:
    with closing(_connect()) as conn:
        for stmt in SCHEMA:
            conn.execute(stmt)
        conn.commit()


def _get_job(conn: sqlite3.Connection, job_id: int) -> dict:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(f"job {job_id} not found")
    return dict(row)


def enqueue_job(
    kind: str,
    label: str,
    command: list[str],
    trigger: str,
    *,
    slot_key: str = "",
    scheduled_for: str = "",
    priority: int = 100,
    timeout_seconds: int = 3600,
    max_attempts: int = 1,
    created_at: datetime | None = None,
) -> dict:
    init_job_store()
    now_text = _now_text(created_at)
    command_json = json.dumps(command, ensure_ascii=False)
    with closing(_connect()) as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO jobs (
                    kind, label, command_json, trigger, status, priority, slot_key,
                    scheduled_for, max_attempts, timeout_seconds, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    kind, label, command_json, trigger, priority, slot_key,
                    scheduled_for, max_attempts, timeout_seconds, now_text, now_text,
                ),
            )
            conn.commit()
            return _get_job(conn, int(cur.lastrowid))
        except sqlite3.IntegrityError:
            if not slot_key:
                raise
            row = conn.execute("SELECT * FROM jobs WHERE slot_key = ?", (slot_key,)).fetchone()
            if row is None:
                raise
            return dict(row)


def claim_next_job(now: datetime | None = None) -> dict | None:
    init_job_store()
    now_text = _now_text(now)
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status = 'queued'
            ORDER BY priority ASC, created_at ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        job_id = int(row["id"])
        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (now_text, job_id),
        )
        conn.commit()
        return _get_job(conn, job_id)


def mark_started(job_id: int, pid: int, output_path: str | Path, started_at: datetime | None = None) -> dict:
    now_text = _now_text(started_at)
    with closing(_connect()) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET pid = ?, output_path = ?, started_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (pid, str(output_path), now_text, now_text, job_id),
        )
        conn.commit()
        return _get_job(conn, job_id)


def mark_finished(
    job_id: int,
    status: str,
    exit_code: int | None,
    finished_at: datetime | None = None,
    error_summary: str = "",
) -> dict:
    if status not in FINAL_STATUSES:
        raise ValueError(f"invalid final status: {status}")
    now_text = _now_text(finished_at)
    with closing(_connect()) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, exit_code = ?, finished_at = ?, error_summary = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, exit_code, now_text, error_summary, now_text, job_id),
        )
        conn.commit()
        return _get_job(conn, job_id)


def mark_timed_out(job_id: int, finished_at: datetime | None = None, error_summary: str = "") -> dict:
    return mark_finished(job_id, "timed_out", None, finished_at, error_summary)


def active_job() -> dict | None:
    init_job_store()
    with closing(_connect()) as conn:
        return _row_to_dict(conn.execute("SELECT * FROM jobs WHERE status = 'running' ORDER BY updated_at DESC LIMIT 1").fetchone())


def queued_jobs(limit: int = 10) -> list[dict]:
    init_job_store()
    with closing(_connect()) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY priority ASC, created_at ASC LIMIT ?",
            (limit,),
        )]


def recent_jobs(statuses: Iterable[str], limit: int = 5) -> list[dict]:
    init_job_store()
    status_list = list(statuses)
    if not status_list:
        return []
    placeholders = ",".join("?" for _ in status_list)
    with closing(_connect()) as conn:
        return [dict(row) for row in conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY updated_at DESC LIMIT ?",
            (*status_list, limit),
        )]


def has_pending_or_running(kind: str, trigger: str | None = None) -> bool:
    init_job_store()
    sql = "SELECT 1 FROM jobs WHERE kind = ? AND status IN ('queued', 'running')"
    params: list[object] = [kind]
    if trigger is not None:
        sql += " AND trigger = ?"
        params.append(trigger)
    sql += " LIMIT 1"
    with closing(_connect()) as conn:
        return conn.execute(sql, params).fetchone() is not None


def queue_position(job_id: int) -> int:
    queued = queued_jobs(limit=1000)
    for index, job in enumerate(queued, start=1):
        if int(job["id"]) == int(job_id):
            return index
    return 0


def recover_running_jobs(now: datetime | None = None) -> list[dict]:
    init_job_store()
    now_text = _now_text(now)
    with closing(_connect()) as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM jobs WHERE status = 'running'")]
        for row in rows:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'interrupted', finished_at = ?, error_summary = ?, updated_at = ?
                WHERE id = ?
                """,
                (now_text, "daemon restarted while job was running", now_text, int(row["id"])),
            )
        conn.commit()
    return rows
```

- [ ] **Step 5: Run test to verify it passes**

Run:

```bash
python3 -m unittest tests.unit.test_job_store -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/common.py src/job_store.py tests/unit/test_job_store.py
git commit -m "feat: add persistent job store"
```

---

### Task 3: Add Job Runner

**Files:**
- Create: `src/job_runner.py`
- Create: `tests/unit/test_job_runner.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_job_runner.py`:

```python
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src import job_runner, job_store

CST = timezone(timedelta(hours=8))


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 5, 14, hour, minute, second, tzinfo=CST)


class JobRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.logs = self.root / "state" / "logs"
        self.logs.mkdir(parents=True)
        self.db_path = self.root / "state" / "jobs.db"
        self.db_patch = patch.object(job_store, "JOBS_DB_PATH", self.db_path)
        self.db_patch.start()
        job_store.init_job_store()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_tick_starts_queued_job_and_writes_to_file_not_pipe(self):
        job_store.enqueue_job("reply", "run_once.py", [sys.executable, "-c", "print('ok')"], "telegram", created_at=at(9))
        proc = MagicMock()
        proc.pid = 123
        proc.poll.return_value = None

        with patch("src.job_runner.subprocess.Popen", return_value=proc) as popen:
            runner = job_runner.JobRunner(root=self.root, log_dir=self.logs)
            runner.tick(at(9, 1))

        active = job_store.active_job()
        self.assertEqual(active["pid"], 123)
        self.assertIn("job-1.log", active["output_path"])
        self.assertIsNot(popen.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertIs(popen.call_args.kwargs["stderr"], subprocess.STDOUT)

    def test_tick_marks_completed_job_succeeded(self):
        job_store.enqueue_job("reply", "run_once.py", [sys.executable, "-c", "print('ok')"], "telegram", created_at=at(9))
        runner = job_runner.JobRunner(root=self.root, log_dir=self.logs)
        runner.tick(at(9, 1))
        runner.tick(at(9, 2))

        recent = job_store.recent_jobs(["succeeded"], limit=1)
        self.assertEqual(recent[0]["status"], "succeeded")
        self.assertEqual(recent[0]["exit_code"], 0)

    def test_tick_marks_nonzero_exit_failed(self):
        job_store.enqueue_job("reply", "run_once.py", [sys.executable, "-c", "raise SystemExit(7)"], "telegram", created_at=at(9))
        runner = job_runner.JobRunner(root=self.root, log_dir=self.logs)
        runner.tick(at(9, 1))
        runner.tick(at(9, 2))

        recent = job_store.recent_jobs(["failed"], limit=1)
        self.assertEqual(recent[0]["exit_code"], 7)

    def test_timeout_terminates_running_process(self):
        job_store.enqueue_job(
            "reply",
            "run_once.py",
            [sys.executable, "-c", "import time; time.sleep(60)"],
            "telegram",
            timeout_seconds=1,
            created_at=at(9),
        )
        proc = MagicMock()
        proc.pid = 456
        proc.poll.return_value = None

        with patch("src.job_runner.subprocess.Popen", return_value=proc):
            runner = job_runner.JobRunner(root=self.root, log_dir=self.logs)
            runner.tick(at(9, 0))
            runner.tick(at(9, 0, 2))

        proc.terminate.assert_called_once()
        recent = job_store.recent_jobs(["timed_out"], limit=1)
        self.assertEqual(recent[0]["status"], "timed_out")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3 -m unittest tests.unit.test_job_runner -v
```

Expected: import failure for missing `src.job_runner`.

- [ ] **Step 3: Implement `src/job_runner.py`**

Create `src/job_runner.py`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from src import job_store
from src.common import LOG_DIR
from src.scheduling import BEIJING_TZ


class JobRunner:
    def __init__(self, root: Path, log_dir: Path = LOG_DIR):
        self.root = Path(root)
        self.log_dir = Path(log_dir)
        self.proc: subprocess.Popen[str] | None = None
        self.job: dict | None = None
        self._output_fh = None

    def tick(self, now: datetime | None = None) -> None:
        current = now or datetime.now(tz=BEIJING_TZ)
        if self.proc is not None and self.job is not None:
            self._check_running(current)
            return
        job = job_store.claim_next_job(current)
        if job is not None:
            self._start(job, current)

    def shutdown(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        if self._output_fh is not None:
            self._output_fh.close()
            self._output_fh = None

    def _start(self, job: dict, now: datetime) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.log_dir / f"job-{job['id']}.log"
        output_fh = output_path.open("w", encoding="utf-8")
        command = json.loads(job["command_json"])
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.root)
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(self.root),
                stdout=output_fh,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
        except Exception as exc:
            output_fh.close()
            job_store.mark_finished(int(job["id"]), "failed", None, now, f"spawn failed: {exc}")
            return
        self.proc = proc
        self.job = job_store.mark_started(int(job["id"]), proc.pid, output_path, now)
        self._output_fh = output_fh

    def _check_running(self, now: datetime) -> None:
        assert self.proc is not None
        assert self.job is not None
        code = self.proc.poll()
        if code is not None:
            self._close_output()
            status = "succeeded" if code == 0 else "failed"
            job_store.mark_finished(int(self.job["id"]), status, int(code), now)
            self.proc = None
            self.job = None
            return
        if self._timed_out(now):
            self._terminate_timeout(now)

    def _timed_out(self, now: datetime) -> bool:
        assert self.job is not None
        started = str(self.job.get("started_at") or "")
        if not started:
            return False
        stamp = started.rsplit(" ", 1)[0]
        started_dt = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TZ)
        elapsed = (now - started_dt).total_seconds()
        return elapsed > int(self.job.get("timeout_seconds") or 3600)

    def _terminate_timeout(self, now: datetime) -> None:
        assert self.proc is not None
        assert self.job is not None
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self._close_output()
        job_store.mark_timed_out(int(self.job["id"]), now, "job exceeded timeout")
        self.proc = None
        self.job = None

    def _close_output(self) -> None:
        if self._output_fh is not None:
            self._output_fh.close()
            self._output_fh = None


def tail_output(job: dict, chars: int = 1500) -> str:
    path = Path(str(job.get("output_path") or ""))
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[-chars:]
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
python3 -m unittest tests.unit.test_job_runner -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/job_runner.py tests/unit/test_job_runner.py
git commit -m "feat: add single job runner"
```

---

### Task 4: Wire Telegram Commands To Queue

**Files:**
- Modify: `src/telegram_commands.py`
- Create: `tests/unit/test_telegram_job_queue.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_telegram_job_queue.py`:

```python
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import src.telegram_commands as tg_commands


class TelegramJobQueueTests(unittest.TestCase):
    def _args(self):
        now = datetime(2026, 5, 14, tzinfo=timezone.utc)
        return (None, now, now, now, now, now, "", "")

    def test_run_enqueues_reply_instead_of_starting_process(self):
        fake_job = {"id": 42}
        with (
            patch.object(tg_commands, "_safe_notify") as notify,
            patch.object(tg_commands, "enqueue_command_job", return_value=fake_job) as enqueue,
        ):
            result = tg_commands.handle_command("/run", *self._args())
        self.assertIsNone(result[0])
        enqueue.assert_called_once_with("reply")
        self.assertIn("已加入队列", notify.call_args.args[0])
        self.assertIn("#42", notify.call_args.args[0])

    def test_post_dry_run_enqueues_post_dry(self):
        with (
            patch.object(tg_commands, "_safe_notify"),
            patch.object(tg_commands, "enqueue_command_job", return_value={"id": 7}) as enqueue,
        ):
            tg_commands.handle_command("/post_dry_run", *self._args())
        enqueue.assert_called_once_with("post_dry")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3 -m unittest tests.unit.test_telegram_job_queue -v
```

Expected: missing `enqueue_command_job` or old direct-start behavior.

- [ ] **Step 3: Implement enqueue helper and command replacements**

In `src/telegram_commands.py`, import:

```python
from pathlib import Path

from src import job_store
from src.job_specs import build_job_command, job_spec
```

Add:

```python
ROOT = Path(__file__).resolve().parent.parent


def enqueue_command_job(kind: str) -> dict:
    spec = job_spec(kind, trigger="telegram")
    return job_store.enqueue_job(
        kind=spec.kind,
        label=spec.label,
        command=build_job_command(spec, ROOT, "telegram"),
        trigger="telegram",
        priority=spec.priority,
        timeout_seconds=spec.timeout_seconds,
    )


def _queue_notify(title: str, job: dict) -> None:
    pos = job_store.queue_position(int(job["id"]))
    suffix = f"队列位置: {pos}" if pos else "即将执行"
    _safe_notify(f"{title}\n\n✅ 已加入队列 #{job['id']}\n{suffix}")
```

Change start commands:

```python
if command.startswith("/run"):
    job = enqueue_command_job("reply")
    _queue_notify("💬 回复", job)
    return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
```

Apply the same pattern:

```text
/post_once -> post
/post_dry_run -> post_dry
/learn_once -> learn
/revisit_once -> revisit
/hotspot_discover -> hotspot
/update -> update
```

Remove old "当前已有任务在执行" checks for these enqueue commands. Leave `/config`, `/event`, `/review`, `/rate`, and status commands unchanged for this task.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
python3 -m unittest tests.unit.test_telegram_job_queue tests.unit.test_update_command -v
```

Expected: new queue tests pass. Existing update command tests will fail because behavior intentionally changed.

- [ ] **Step 5: Update existing update-command tests**

Modify `tests/unit/test_update_command.py` so it expects enqueue behavior:

```python
def test_update_command_enqueues_update_job(self):
    with patch.object(tg_commands, "_safe_notify") as notify, patch.object(
        tg_commands, "enqueue_command_job", return_value={"id": 99}
    ) as enqueue:
        result = tg_commands.handle_command("/update", *self._args())

    self.assertIsNone(result[0])
    enqueue.assert_called_once_with("update")
    self.assertIn("已加入队列", notify.call_args.args[0])
```

Remove `test_update_command_refuses_while_job_is_running` because queued jobs are allowed while a job is running.

- [ ] **Step 6: Run tests**

Run:

```bash
python3 -m unittest tests.unit.test_telegram_job_queue tests.unit.test_update_command tests.unit.test_config_command -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/telegram_commands.py tests/unit/test_telegram_job_queue.py tests/unit/test_update_command.py
git commit -m "feat: enqueue telegram jobs"
```

---

### Task 5: Wire Daemon Scheduling Through Queue And Runner

**Files:**
- Modify: `bot_daemon.py`
- Modify: `tests/integration/test_daemon_schedule.py`

- [ ] **Step 1: Add failing schedule enqueue test**

Append to `tests/integration/test_daemon_schedule.py`:

```python
class ScheduleEnqueueTests(unittest.TestCase):
    def test_enqueue_scheduled_job_skips_when_same_kind_pending(self):
        spec = bot_daemon.job_spec("reply", trigger="schedule")
        with (
            patch("bot_daemon.job_store.has_pending_or_running", return_value=True) as pending,
            patch("bot_daemon.job_store.enqueue_job") as enqueue,
        ):
            job = bot_daemon.enqueue_scheduled_job("reply", at(9, 0))
        self.assertIsNone(job)
        pending.assert_called_once_with("reply", trigger="schedule")
        enqueue.assert_not_called()

    def test_enqueue_scheduled_job_creates_slot_key(self):
        with (
            patch("bot_daemon.job_store.has_pending_or_running", return_value=False),
            patch("bot_daemon.job_store.enqueue_job", return_value={"id": 1}) as enqueue,
        ):
            job = bot_daemon.enqueue_scheduled_job("reply", at(9, 0))
        self.assertEqual(job["id"], 1)
        self.assertEqual(enqueue.call_args.kwargs["slot_key"], "schedule:reply:2026-05-05T09:00")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3 -m unittest tests.integration.test_daemon_schedule.ScheduleEnqueueTests -v
```

Expected: missing `enqueue_scheduled_job`.

- [ ] **Step 3: Add daemon enqueue helpers**

Modify `bot_daemon.py` imports:

```python
from src import job_store
from src.job_runner import JobRunner
from src.job_specs import build_job_command, job_spec
```

Add:

```python
def slot_key(kind: str, scheduled_for) -> str:
    return f"schedule:{kind}:{scheduled_for.strftime('%Y-%m-%dT%H:%M')}"


def enqueue_scheduled_job(kind: str, scheduled_for):
    if job_store.has_pending_or_running(kind, trigger="schedule"):
        logger.info("schedule %s skipped because pending/running job exists", kind)
        return None
    spec = job_spec(kind, trigger="schedule")
    return job_store.enqueue_job(
        kind=spec.kind,
        label=spec.label,
        command=build_job_command(spec, ROOT, "schedule"),
        trigger="schedule",
        slot_key=slot_key(kind, scheduled_for),
        scheduled_for=scheduled_for.strftime("%Y-%m-%d %H:%M:%S %Z"),
        priority=spec.priority,
        timeout_seconds=spec.timeout_seconds,
    )
```

- [ ] **Step 4: Replace direct Popen main loop**

In `main()`:

- Delete `run_proc`, `run_trigger`, and `active_label` state.
- Instantiate `runner = JobRunner(ROOT)`.
- On startup call `job_store.init_job_store()` and `job_store.recover_running_jobs(now)`.
- Replace each direct `start_job` branch with `enqueue_scheduled_job`.
- Always call `runner.tick(now)` once per loop.
- Pass `None` state values to existing `poll_updates` until Task 7 removes old tuple plumbing.

The schedule branch shape should be:

```python
if now >= next_run_at:
    enqueue_scheduled_job("reply", next_run_at)
    next_run_at = next_scheduled_after(now)
elif now >= next_post_run_at:
    today = now.strftime("%Y-%m-%d")
    if count_scheduled_posts(today) < post_daily_limit():
        enqueue_scheduled_job("post", next_post_run_at)
    next_post_run_at = next_proactive_after(now)
elif (
    learning_enabled()
    and now >= next_learn_at
    and (next_run_at - now).total_seconds() > learning_guard_seconds()
    and (next_post_run_at - now).total_seconds() > learning_guard_seconds()
):
    enqueue_scheduled_job("learn", next_learn_at)
    next_learn_at = next_learning_after(now)
```

Use the same pattern for hotspot and revisit.

- [ ] **Step 5: Preserve shutdown behavior**

In the `finally` block, replace child termination code with:

```python
try:
    runner.shutdown()
except Exception as exc:
    logger.warning(f"job runner shutdown failed: {exc}")
```

- [ ] **Step 6: Run daemon schedule tests**

Run:

```bash
python3 -m unittest tests.integration.test_daemon_schedule -v
```

Expected: all tests pass after updating obsolete Popen-specific tests to assert enqueue/runner behavior instead.

- [ ] **Step 7: Commit**

```bash
git add bot_daemon.py tests/integration/test_daemon_schedule.py
git commit -m "feat: enqueue scheduled daemon jobs"
```

---

### Task 6: Update Status Reporting For Job Queue

**Files:**
- Modify: `src/reporters.py`
- Create: `tests/unit/test_reporters_job_queue_status.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_reporters_job_queue_status.py`:

```python
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src import reporters

CST = timezone(timedelta(hours=8))


class JobQueueStatusTests(unittest.TestCase):
    def test_status_text_includes_running_and_queued_jobs(self):
        now = datetime(2026, 5, 14, 10, 0, tzinfo=CST)
        active = {"id": 1, "label": "run_once.py", "trigger": "schedule", "started_at": "2026-05-14 09:55:00 UTC+08:00"}
        queued = [{"id": 2, "label": "post_once.py", "trigger": "telegram"}]
        failed = [{"id": 3, "label": "discover_hotspots.py", "status": "failed"}]

        with (
            patch("src.reporters._beijing_now", return_value=now),
            patch("src.reporters.job_store.active_job", return_value=active),
            patch("src.reporters.job_store.queued_jobs", return_value=queued),
            patch("src.reporters.job_store.recent_jobs", return_value=failed),
            patch("src.reporters.latest_summary", return_value="最近: none"),
            patch("src.reporters.learning_enabled", return_value=True),
            patch("src.reporters.hotspot_enabled", return_value=True),
        ):
            text = reporters.status_text(None, now, now, now, now, now, "")

        self.assertIn("正在执行 #1 run_once.py", text)
        self.assertIn("队列: 1 个", text)
        self.assertIn("#2 post_once.py", text)
        self.assertIn("最近异常", text)
        self.assertIn("#3 discover_hotspots.py failed", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3 -m unittest tests.unit.test_reporters_job_queue_status -v
```

Expected: missing job queue details in status text.

- [ ] **Step 3: Update `src/reporters.py`**

Import:

```python
from src import job_store
```

Add helpers:

```python
def _job_queue_lines() -> list[str]:
    active = job_store.active_job()
    queued = job_store.queued_jobs(limit=5)
    recent_bad = job_store.recent_jobs(["failed", "timed_out", "interrupted"], limit=3)
    lines: list[str] = []
    if active:
        lines.append(format_kv("⏳", "当前", f"正在执行 #{active['id']} {active['label']} ({active['trigger']})"))
    else:
        lines.append(format_kv("✅", "当前", "空闲"))
    lines.append(format_kv("📚", "队列", f"{len(queued)} 个"))
    for job in queued:
        lines.append(f"  #{job['id']} {job['label']} ({job['trigger']})")
    if recent_bad:
        lines.append(format_kv("⚠️", "最近异常", ""))
        for job in recent_bad:
            lines.append(f"  #{job['id']} {job['label']} {job['status']}")
    return lines
```

In `status_text`, replace the current active/idle block with `_job_queue_lines()`. Keep next schedule times and `latest_summary()`.

- [ ] **Step 4: Run tests**

Run:

```bash
python3 -m unittest tests.unit.test_reporters_job_queue_status tests.unit.test_reporters_status_text -v
```

Expected: both tests pass. Update `tests/unit/test_reporters_status_text.py` so it asserts the new queue-aware current-job line such as `正在执行 #1 run_once.py` and keeps the overdue schedule-time assertions.

- [ ] **Step 5: Commit**

```bash
git add src/reporters.py tests/unit/test_reporters_job_queue_status.py tests/unit/test_reporters_status_text.py
git commit -m "feat: show job queue in status"
```

---

### Task 7: Clean Up Obsolete In-Memory Job Plumbing

**Files:**
- Modify: `bot_daemon.py`
- Modify: `src/telegram_commands.py`
- Modify: tests that still pass around `run_proc`

- [ ] **Step 1: Search for obsolete helpers**

Run:

```bash
rg -n "start_job|run_proc|run_trigger|active_label|mark_job_started|job_timeout_info|terminate_timed_out_job" bot_daemon.py src tests
```

Expected: only compatibility references that are still intentionally needed.

- [ ] **Step 2: Remove obsolete daemon helpers**

Delete from `bot_daemon.py` if no longer used:

```python
start_job
finish_run
job_timeout_seconds
mark_job_started
mark_job_output_path
job_timeout_info
terminate_timed_out_job
```

The equivalent behavior must now live in `src/job_runner.py`.

- [ ] **Step 3: Keep Telegram command tuple plumbing stable**

Keep `handle_command` and `poll_updates` signatures stable for this implementation. Ensure enqueue commands return the input tuple unchanged:

```python
return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
```

- [ ] **Step 4: Run targeted tests**

Run:

```bash
python3 -m unittest tests.unit.test_telegram_job_queue tests.unit.test_update_command tests.unit.test_config_command tests.integration.test_daemon_schedule -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add bot_daemon.py src/telegram_commands.py tests
git commit -m "refactor: remove in-memory daemon job state"
```

---

### Task 8: Add Notifications And Changelog

**Files:**
- Modify: `src/job_runner.py`
- Modify: `CHANGELOG.md`
- Test: `tests/unit/test_job_runner.py`

- [ ] **Step 1: Add failing notification test**

Append to `tests/unit/test_job_runner.py`:

```python
    def test_failed_job_sends_failure_notification(self):
        job_store.enqueue_job("reply", "run_once.py", [sys.executable, "-c", "print('bad'); raise SystemExit(2)"], "schedule", created_at=at(9))
        with (
            patch("src.job_runner.telegram_enabled", return_value=True),
            patch("src.job_runner.telegram_notify") as notify,
        ):
            runner = job_runner.JobRunner(root=self.root, log_dir=self.logs)
            runner.tick(at(9, 1))
            runner.tick(at(9, 2))
        notify.assert_called_once()
        text = notify.call_args.args[0]
        self.assertIn("任务失败", text)
        self.assertIn("exit_code", text)
        self.assertIn("bad", text)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3 -m unittest tests.unit.test_job_runner.JobRunnerTests.test_failed_job_sends_failure_notification -v
```

Expected: notification not sent.

- [ ] **Step 3: Implement notification helper**

In `src/job_runner.py`, import:

```python
from src.common import telegram_enabled, telegram_notify
from src.reporters import format_kv
```

Add method:

```python
def _notify_failure(self, job: dict, status: str, exit_code: int | None) -> None:
    if not telegram_enabled():
        return
    output = tail_output(job, 1500)
    try:
        telegram_notify("\n".join([
            "⚠️ 任务失败",
            "",
            format_kv("🧩", "任务", f"#{job['id']} {job['label']}"),
            format_kv("⚙️", "触发", job["trigger"]),
            format_kv("📌", "状态", status),
            format_kv("🔢", "exit_code", exit_code if exit_code is not None else ""),
            format_kv("📄", "日志", job.get("output_path", "")),
            "",
            "📄 最近输出:",
            output or "(empty)",
        ]))
    except Exception:
        pass
```

Call `_notify_failure` after marking `failed` or `timed_out`.

- [ ] **Step 4: Update changelog**

Under `CHANGELOG.md` `[Unreleased]`, add:

```markdown
- Add a SQLite-backed job queue for daemon tasks with durable status, log files, timeout handling, and queue-aware Telegram status
```

- [ ] **Step 5: Run tests**

Run:

```bash
python3 -m unittest tests.unit.test_job_runner -v
```

Expected: all runner tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/job_runner.py tests/unit/test_job_runner.py CHANGELOG.md
git commit -m "feat: notify job failures"
```

---

### Task 9: Final Verification

**Files:**
- No planned edits unless verification finds defects.

- [ ] **Step 1: Run focused unit and integration tests**

Run:

```bash
python3 -m unittest \
  tests.unit.test_job_specs \
  tests.unit.test_job_store \
  tests.unit.test_job_runner \
  tests.unit.test_telegram_job_queue \
  tests.unit.test_update_command \
  tests.unit.test_config_command \
  tests.unit.test_reporters_job_queue_status \
  tests.unit.test_reporters_status_text \
  tests.integration.test_daemon_schedule \
  -v
```

Expected: all tests pass.

- [ ] **Step 2: Compile touched modules**

Run:

```bash
python3 -m py_compile \
  bot_daemon.py \
  src/common.py \
  src/job_specs.py \
  src/job_store.py \
  src/job_runner.py \
  src/telegram_commands.py \
  src/reporters.py
```

Expected: exit code 0.

- [ ] **Step 3: Check for pipe usage regressions**

Run:

```bash
rg -n "stdout=subprocess.PIPE|stdout=PIPE" bot_daemon.py src/job_runner.py src/telegram_commands.py
```

Expected: no matches for daemon-managed long-running jobs. It is acceptable for tests or short helper subprocesses elsewhere to use `capture_output=True`.

- [ ] **Step 4: Check git diff**

Run:

```bash
git status --short
git diff --stat
```

Expected: only intended files changed.

- [ ] **Step 5: Commit any final fixes**

If verification required small fixes:

```bash
git add <fixed files>
git commit -m "fix: stabilize persistent job queue"
```

If no fixes are needed, do not create an empty commit.
