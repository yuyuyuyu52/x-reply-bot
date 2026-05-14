#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
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
        proc = self.proc
        job = self.job
        try:
            if proc is not None and proc.poll() is None:
                self._terminate_process_tree(proc)
            if job is not None:
                job_store.mark_finished(int(job["id"]), "interrupted", None, datetime.now(tz=BEIJING_TZ))
        finally:
            self._clear_state()

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
            try:
                job_store.mark_finished(int(self.job["id"]), status, int(code), now)
            finally:
                self._clear_state()
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
            job_store.mark_timed_out(int(job["id"]), now, "job exceeded timeout")
        finally:
            self._clear_state()

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


def tail_output(job: dict, chars: int = 1500) -> str:
    path = Path(str(job.get("output_path") or ""))
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[-chars:]
