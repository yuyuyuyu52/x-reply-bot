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
