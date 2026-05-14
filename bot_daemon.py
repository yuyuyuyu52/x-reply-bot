#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import os
import re
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

from src import job_store
from src.common import (
    BOT_LOCK_PATH,
    LOG_DIR,
    ensure_state_dirs,
    load_env_file,
    telegram_enabled,
    telegram_notify,
)
from src.job_runner import JobRunner
from src.job_specs import build_job_command, job_spec
from src.logger import get_logger
from src.reporters import (
    count_scheduled_posts,
    format_kv,
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
_JOB_STARTED_ATTR = "_x_reply_started_at"
_JOB_OUTPUT_PATH_ATTR = "_x_reply_output_path"


def job_timeout_seconds() -> int:
    try:
        return max(300, int(os.environ.get("X_JOB_TIMEOUT_SECONDS", "3600")))
    except ValueError:
        return 3600


def mark_job_started(proc: subprocess.Popen[str], started_at=None):
    setattr(proc, _JOB_STARTED_ATTR, started_at or _beijing_now())
    return proc


def mark_job_output_path(proc: subprocess.Popen[str], output_path: Path):
    setattr(proc, _JOB_OUTPUT_PATH_ATTR, output_path)
    return proc


def job_timeout_info(proc: subprocess.Popen[str], now=None) -> tuple[bool, int, int]:
    started_at = getattr(proc, _JOB_STARTED_ATTR, None)
    if started_at is None:
        return False, 0, job_timeout_seconds()
    current = now or _beijing_now()
    elapsed = max(0, int((current - started_at).total_seconds()))
    limit = job_timeout_seconds()
    return elapsed > limit, elapsed, limit


def _child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    return env


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


def start_job(script: str, trigger: str, extra_args: list[str] | None = None) -> subprocess.Popen[str]:
    logger.info(f"{script} start trigger={trigger}")
    cmd = [sys.executable, str(ROOT / script), "--trigger", trigger]
    if extra_args:
        cmd.extend(extra_args)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe_script = re.sub(r"[^0-9A-Za-z_.-]+", "_", script)
    stamp = _beijing_now().strftime("%Y%m%d_%H%M%S")
    output_path = LOG_DIR / f"job-{stamp}-{os.getpid()}-{safe_script}.log"
    output_fh = output_path.open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=output_fh,
            stderr=subprocess.STDOUT,
            text=True,
            env=_child_env(),
        )
    finally:
        output_fh.close()
    mark_job_output_path(proc, output_path)
    return mark_job_started(proc)


def terminate_timed_out_job(run_proc: subprocess.Popen[str], label: str, trigger: str, elapsed: int, limit: int) -> None:
    logger.warning("%s timed out trigger=%s elapsed=%ss limit=%ss; terminating", label, trigger, elapsed, limit)
    if telegram_enabled():
        try:
            telegram_notify(
                "\n".join(
                    [
                        "⚠️ 任务超时",
                        "",
                        format_kv("🧩", "任务", label or "job"),
                        format_kv("⚙️", "触发", trigger),
                        format_kv("⏱️", "已运行", f"{elapsed}s / {limit}s"),
                        "",
                        "已终止该任务，调度器会在下一轮继续处理后续 slot。",
                    ]
                )
            )
        except Exception as exc:
            logger.warning(f"telegram timeout notify failed: {exc}")
    try:
        run_proc.terminate()
        try:
            run_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("%s did not exit after timeout SIGTERM; killing", label)
            run_proc.kill()
            run_proc.wait(timeout=10)
    except Exception as exc:
        logger.warning(f"{label} timeout termination failed: {exc}")


def finish_run(run_proc: subprocess.Popen[str], trigger: str, label: str) -> None:
    output_path = getattr(run_proc, _JOB_OUTPUT_PATH_ATTR, None)
    if output_path:
        try:
            output = Path(output_path).read_text(encoding="utf-8")
        except Exception as exc:
            output = f"(failed to read job output {output_path}: {exc})"
    else:
        output = run_proc.stdout.read() if run_proc.stdout else ""
    for line in (output or "").splitlines():
        logger.info(f"{label}[{trigger}] {line}")
    code = run_proc.returncode
    logger.info(f"{label} end trigger={trigger} code={code}")
    if code != 0 and telegram_enabled():
        action_match = re.search(r'GENERATED_ACTION:\s*(\w+)', output or "")
        action_info = f" ({action_match.group(1)})" if action_match else ""
        try:
            telegram_notify(
                "\n".join(
                    [
                        "⚠️ 任务失败",
                        "",
                        format_kv("🧩", "任务", f"{label}{action_info}"),
                        format_kv("⚙️", "触发", trigger),
                        format_kv("🔢", "exit_code", code),
                        "",
                        "📄 最近输出:",
                        (output or "").strip()[-1500:] or "(empty)",
                    ]
                )
            )
        except Exception as exc:
            logger.warning(f"telegram failure notify failed: {exc}")


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
            elif (
                in_revisit_window(now)
                and now >= next_revisit_at
                and (next_run_at - now).total_seconds() > revisit_guard_seconds()
                and (next_post_run_at - now).total_seconds() > revisit_guard_seconds()
            ):
                enqueue_scheduled_job("revisit", next_revisit_at)
                next_revisit_at = next_revisit_after(now)

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
