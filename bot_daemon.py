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

from src.common import (
    BOT_LOCK_PATH,
    ensure_state_dirs,
    load_env_file,
    telegram_enabled,
    telegram_notify,
)
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


def _child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    return env


def start_job(script: str, trigger: str) -> subprocess.Popen[str]:
    logger.info(f"{script} start trigger={trigger}")
    return subprocess.Popen(
        [sys.executable, str(ROOT / script), "--trigger", trigger],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_child_env(),
    )


def finish_run(run_proc: subprocess.Popen[str], trigger: str, label: str) -> None:
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
    run_proc: subprocess.Popen[str] | None = None
    run_trigger = ""
    active_label = ""

    try:
        logger.info("bot daemon started")
        while True:
            if _shutdown_requested:
                logger.info("bot daemon shutdown requested; exiting main loop")
                break
            now = _beijing_now()

            if run_proc and run_proc.poll() is not None:
                finished_at = _beijing_now()
                carry_over_post_slot = (
                    not active_label.startswith("post_once.py")
                    and next_post_run_at <= finished_at
                )
                carry_over_reply_slot = (
                    active_label != "run_once.py"
                    and next_run_at <= finished_at
                )
                carry_over_revisit_slot = (
                    active_label != "src/learning/revisit.py"
                    and in_revisit_window(finished_at)
                    and next_revisit_at <= finished_at
                )
                carry_over_hotspot_slot = (
                    active_label != "discover_hotspots.py"
                    and hotspot_enabled()
                    and next_hotspot_at <= finished_at
                )
                finish_run(run_proc, run_trigger, active_label or "job")
                run_proc = None
                run_trigger = ""
                active_label = ""
                next_run_at = finished_at if carry_over_reply_slot else next_scheduled_after(finished_at)
                if carry_over_post_slot:
                    next_post_run_at = finished_at
                else:
                    next_post_run_at = next_proactive_after(finished_at)
                next_learn_at = next_learning_after(finished_at)
                next_revisit_at = finished_at if carry_over_revisit_slot else next_revisit_after(finished_at)
                next_hotspot_at = finished_at if carry_over_hotspot_slot else next_hotspot_after(finished_at)

            if run_proc is None and now >= next_run_at:
                run_proc = start_job("run_once.py", "schedule")
                run_trigger = "schedule"
                active_label = "run_once.py"
            elif run_proc is None and now >= next_post_run_at:
                today = now.strftime("%Y-%m-%d")
                if count_scheduled_posts(today) < post_daily_limit():
                    run_proc = start_job("post_once.py", "schedule")
                    run_trigger = "schedule"
                    active_label = "post_once.py"
                next_post_run_at = next_proactive_after(now)
            elif (
                run_proc is None
                and learning_enabled()
                and now >= next_learn_at
                and (next_run_at - now).total_seconds() > learning_guard_seconds()
                and (next_post_run_at - now).total_seconds() > learning_guard_seconds()
            ):
                run_proc = start_job("src/learning/observe.py", "schedule")
                run_trigger = "schedule"
                active_label = "src/learning/observe.py"
                next_learn_at = next_learning_after(now)
            elif (
                run_proc is None
                and hotspot_enabled()
                and now >= next_hotspot_at
                and (next_run_at - now).total_seconds() > hotspot_guard_seconds()
                and (next_post_run_at - now).total_seconds() > hotspot_guard_seconds()
            ):
                run_proc = start_job("discover_hotspots.py", "schedule")
                run_trigger = "schedule"
                active_label = "discover_hotspots.py"
                next_hotspot_at = next_hotspot_after(now)
            elif (
                run_proc is None
                and in_revisit_window(now)
                and now >= next_revisit_at
                and (next_run_at - now).total_seconds() > revisit_guard_seconds()
                and (next_post_run_at - now).total_seconds() > revisit_guard_seconds()
            ):
                run_proc = start_job("src/learning/revisit.py", "schedule")
                run_trigger = "schedule"
                active_label = "src/learning/revisit.py"
                next_revisit_at = next_revisit_after(now)

            try:
                run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label = poll_updates(
                    run_proc,
                    next_run_at,
                    next_post_run_at,
                    next_learn_at,
                    next_revisit_at,
                    next_hotspot_at,
                    run_trigger,
                    active_label,
                )
            except Exception as exc:
                logger.warning(f"telegram poll error: {exc}")

            try:
                maybe_send_daily_cost_report(now, run_proc)
            except Exception as exc:
                logger.warning(f"daily cost report error: {exc}")

            try:
                maybe_send_revisit_report(now, run_proc)
            except Exception as exc:
                logger.warning(f"revisit report error: {exc}")

            time.sleep(5)
    finally:
        # Clean-shutdown: if a child job is still running (e.g. SIGTERM
        # arrived while a Popen was alive), terminate it before we release
        # the daemon lock. Otherwise the orphan keeps the job-level
        # run_once.lock / post_once.lock and the next daemon start can't
        # acquire it, even though no daemon is around to babysit it.
        if run_proc is not None and run_proc.poll() is None:
            logger.info(f"bot daemon stopping; terminating active child {active_label}")
            try:
                run_proc.terminate()
                try:
                    run_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning(f"child {active_label} did not exit on SIGTERM; killing")
                    run_proc.kill()
                    try:
                        run_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        logger.warning(f"child {active_label} did not exit after SIGKILL")
            except Exception as exc:
                logger.warning(f"child terminate failed: {exc}")
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
