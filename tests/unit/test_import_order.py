from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "module",
    [
        "src.llm",
        "src.logger",
        "src.harness",
        "src.telegram",
        "src.topics",
        "src.reply.prepare_post",
        "src.reply.generate_reply",
        "src.reply.send_reply",
        "src.post.post_send",
        "src.post.article_send",
    ],
)
def test_repo_modules_import_cleanly_from_fresh_process(module):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)

    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert proc.returncode == 0, proc.stderr
