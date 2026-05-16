from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_cron_installs_hourly_except_midnight_revisit_hour():
    text = (ROOT / "scripts/install_cron.sh").read_text(encoding="utf-8")

    assert 'job="0 1-23 * * * /usr/bin/env bash \\"${script}\\""' in text


def test_scheduled_run_only_skips_midnight_revisit_hour():
    text = (ROOT / "scripts/scheduled_run.sh").read_text(encoding="utf-8")

    assert 'if ((10#$hour == 0)); then' in text
    assert "outside Beijing window" not in text
