#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path

from src import job_store
from src.common import LOG_DIR, telegram_enabled, telegram_notify
from src.logger import get_logger
from src.scheduling import BEIJING_TZ

logger = get_logger(__name__)


def _kv(icon: str, label: str, value) -> str:
    return f"{icon} {label}: {value}"


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
        proc = self.proc
        job = self.job
        try:
            if proc is not None and job is not None:
                code = proc.poll()
                finished_at = datetime.now(tz=BEIJING_TZ)
                if code is None:
                    self._terminate_process_tree(proc)
                    job_store.mark_finished(int(job["id"]), "interrupted", None, finished_at)
                else:
                    status = "succeeded" if code == 0 else "failed"
                    finished = job_store.mark_finished(int(job["id"]), status, int(code), finished_at)
                    if status != "succeeded":
                        self._notify_failure(finished or job, status, int(code))
            elif job is not None:
                job_store.mark_finished(int(job["id"]), "interrupted", None, datetime.now(tz=BEIJING_TZ))
        finally:
            self._clear_state()

    def _start(self, job: dict, now: datetime) -> None:
        output_fh = None
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            output_path = self.log_dir / f"job-{job['id']}.log"
            output_fh = output_path.open("w", encoding="utf-8")
            command = json.loads(job["command_json"])
        except Exception as exc:
            if output_fh is not None:
                output_fh.close()
            job_store.mark_finished(int(job["id"]), "failed", None, now, f"setup failed: {exc}")
            self._clear_state()
            return
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
                start_new_session=True,
            )
        except Exception as exc:
            output_fh.close()
            job_store.mark_finished(int(job["id"]), "failed", None, now, f"spawn failed: {exc}")
            return
        self.proc = proc
        self.job = job
        self._output_fh = output_fh
        try:
            self.job = job_store.mark_started(int(job["id"]), proc.pid, output_path, now)
        except Exception as exc:
            try:
                self._terminate_process_tree(proc)
                job_store.mark_finished(
                    int(job["id"]),
                    "interrupted",
                    None,
                    now,
                    f"mark_started failed: {exc}",
                )
            finally:
                self._clear_state()
            raise

    def _check_running(self, now: datetime) -> None:
        assert self.proc is not None
        assert self.job is not None
        code = self.proc.poll()
        if code is None:
            try:
                wait_code = self.proc.wait(timeout=0.1)
                code = wait_code if isinstance(wait_code, int) else None
            except subprocess.TimeoutExpired:
                code = None
        if code is not None:
            status = "succeeded" if code == 0 else "failed"
            job_ref = self.job
            try:
                finished = job_store.mark_finished(int(self.job["id"]), status, int(code), now)
            finally:
                self._clear_state()
            if status != "succeeded":
                self._notify_failure(finished or job_ref, status, int(code))
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
        proc = self.proc
        job = self.job
        self._terminate_process_tree(proc)
        try:
            finished = job_store.mark_timed_out(int(job["id"]), now, "job exceeded timeout")
        finally:
            self._clear_state()
        self._notify_failure(finished or job, "timed_out", None)

    def _terminate_process_tree(self, proc: subprocess.Popen[str]) -> None:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait(timeout=10)

    def _clear_state(self) -> None:
        self._close_output()
        self.proc = None
        self.job = None

    def _close_output(self) -> None:
        if self._output_fh is not None:
            self._output_fh.close()
            self._output_fh = None

    def _notify_failure(self, job: dict, status: str, exit_code: int | None) -> None:
        if not telegram_enabled():
            return
        output = tail_output(job, 1500)
        body = "\n".join([
            "⚠️ 任务失败",
            "",
            _kv("🧩", "任务", f"#{job.get('id', '?')} {job.get('label', '')}"),
            _kv("⚙️", "触发", job.get("trigger", "")),
            _kv("📌", "状态", status),
            _kv("🔢", "exit_code", exit_code if exit_code is not None else ""),
            _kv("📄", "日志", job.get("output_path", "")),
            "",
            "📄 最近输出:",
            output or "(empty)",
        ])
        try:
            telegram_notify(body)
        except Exception as exc:
            logger.warning(f"failure notify failed: {exc}")


def tail_output(job: dict, chars: int = 1500) -> str:
    path = Path(str(job.get("output_path") or ""))
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[-chars:]
