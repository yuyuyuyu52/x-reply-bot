#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import signal
import time
import traceback
from pathlib import Path

from src import job_store
from src.common import (
    BOT_LOCK_PATH,
    ensure_state_dirs,
    load_env_file,
)
from src.job_runner import JobRunner
from src.job_specs import build_job_command, job_spec
from src.logger import get_logger
from src.reporters import (
    count_scheduled_posts,
    maybe_send_daily_cost_report,
    maybe_send_revisit_report,
    post_daily_limit,
)
from src.scheduling import (
    _beijing_now,
    hotspot_enabled,
    hotspot_guard_seconds,
    in_revisit_window,
    learning_enabled,
    learning_guard_seconds,
    next_hotspot_after,
    next_learning_after,
    next_proactive_after,
    next_revisit_after,
    next_scheduled_after,
    revisit_guard_seconds,
)
from src.telegram_commands import poll_updates

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent
# Re-export for backward compatibility with any code that previously imported LOCK_PATH from here.
LOCK_PATH = BOT_LOCK_PATH


# Set by SIGTERM/SIGHUP handler; checked from the main loop for clean shutdown.
# Module-level so the signal handler (which can't easily capture closures) and
# the loop can share state without globals-in-main wiring.
_shutdown_requested = False


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


def main() -> int:
    load_env_file()
    ensure_state_dirs()

    # Install signal handlers BEFORE acquiring the lock so a SIGTERM during
    # startup also takes the clean-shutdown path. The handler just flips a
    # flag — the main loop checks it on every tick and breaks out cleanly,
    # so child subprocesses get terminated and the flock gets released.
    # Slot-carry-over invariant: when the loop wakes after a long-running
    # job finishes, any other slot whose due-time elapsed during the run
    # is "carried over" (next-fire set to finished_at) instead of skipped.
    # See the post-job block in the main loop. Keep this invariant when
    # editing the loop — naïve recomputation will silently drop slots.
    def _handle_shutdown(signum, _frame):
        global _shutdown_requested
        _shutdown_requested = True
        logger.info(f"bot daemon got signal {signum}; requesting shutdown")

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _handle_shutdown)
        except (ValueError, OSError):
            # Not a main thread or unsupported on this platform; ignore.
            pass

    lock_fh = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning("bot daemon already running")
        return 0
    now = _beijing_now()
    next_run_at = next_scheduled_after(now)
    next_post_run_at = next_proactive_after(now)
    next_learn_at = next_learning_after(now)
    next_revisit_at = next_revisit_after(now)
    next_hotspot_at = next_hotspot_after(now)

    job_store.init_job_store()
    interrupted = job_store.recover_running_jobs(now)
    for row in interrupted:
        logger.warning(
            "recovered interrupted job #%s %s (was %s)",
            row.get("id"), row.get("label"), row.get("status"),
        )

    runner = JobRunner(ROOT)

    try:
        logger.info("bot daemon started")
        while True:
            if _shutdown_requested:
                logger.info("bot daemon shutdown requested; exiting main loop")
                break
            now = _beijing_now()

            # Enqueue due slots (at most one per tick — the scheduled-backlog
            # cap inside enqueue_scheduled_job further bounds the queue depth).
            if now >= next_run_at:
                enqueue_scheduled_job("reply", next_run_at)
                next_run_at = next_scheduled_after(now)
            elif now >= next_post_run_at:
                today = now.strftime("%Y-%m-%d")
                if count_scheduled_posts(today) < post_daily_limit():
                    enqueue_scheduled_job("post", next_post_run_at)
                next_post_run_at = next_proactive_after(now)
            elif (
                in_revisit_window(now)
                and now >= next_revisit_at
                and (next_run_at - now).total_seconds() > revisit_guard_seconds()
                and (next_post_run_at - now).total_seconds() > revisit_guard_seconds()
            ):
                enqueue_scheduled_job("revisit", next_revisit_at)
                next_revisit_at = next_revisit_after(now)
            elif (
                learning_enabled()
                and now >= next_learn_at
                and (next_run_at - now).total_seconds() > learning_guard_seconds()
                and (next_post_run_at - now).total_seconds() > learning_guard_seconds()
            ):
                enqueue_scheduled_job("learn", next_learn_at)
                next_learn_at = next_learning_after(now)
            elif (
                hotspot_enabled()
                and now >= next_hotspot_at
                and (next_run_at - now).total_seconds() > hotspot_guard_seconds()
                and (next_post_run_at - now).total_seconds() > hotspot_guard_seconds()
            ):
                enqueue_scheduled_job("hotspot", next_hotspot_at)
                next_hotspot_at = next_hotspot_after(now)
            try:
                runner.tick(now)
            except Exception as exc:
                logger.warning(f"job runner tick error: {exc}")

            try:
                poll_updates(None, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, "", "")
            except Exception as exc:
                logger.warning(f"telegram poll error: {exc}")

            # runner.proc is None when idle and a live Popen when a job is
            # running; both report-dispatchers accept that shape via .poll().
            try:
                maybe_send_daily_cost_report(now, runner.proc)
            except Exception as exc:
                logger.warning(f"daily cost report error: {exc}")

            try:
                maybe_send_revisit_report(now, runner.proc)
            except Exception as exc:
                logger.warning(f"revisit report error: {exc}")

            time.sleep(5)
    finally:
        try:
            runner.shutdown()
        except Exception as exc:
            logger.warning(f"job runner shutdown failed: {exc}")
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise
    except Exception:
        logger.error("bot daemon crashed")
        logger.error(traceback.format_exc())
        raise
