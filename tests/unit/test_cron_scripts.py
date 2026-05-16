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


def test_start_bot_reports_locked_daemon_without_removing_lock_file():
    text = (ROOT / "scripts/start_bot.sh").read_text(encoding="utf-8")

    assert "rm -f state/bot.lock" not in text
    assert "tmux" not in text
    assert 'systemctl start "$X_REPLY_SYSTEMD_SERVICE"' in text
    assert "systemd service not installed" in text


def test_status_and_stop_bot_use_systemd_not_tmux():
    status_text = (ROOT / "scripts/status_bot.sh").read_text(encoding="utf-8")
    stop_text = (ROOT / "scripts/stop_bot.sh").read_text(encoding="utf-8")

    assert "tmux" not in status_text
    assert "tmux" not in stop_text
    assert 'systemctl is-active --quiet "$X_REPLY_SYSTEMD_SERVICE"' in status_text
    assert 'systemctl stop "$X_REPLY_SYSTEMD_SERVICE"' in stop_text


def test_systemd_install_and_uninstall_scripts_exist():
    install_text = (ROOT / "scripts/install_systemd.sh").read_text(encoding="utf-8")
    uninstall_text = (ROOT / "scripts/uninstall_systemd.sh").read_text(encoding="utf-8")

    assert "systemctl daemon-reload" in install_text
    assert "systemctl enable --now" in install_text
    assert "ExecStart=" in install_text
    assert "Restart=always" in install_text
    assert "KillMode=process" in install_text
    assert "systemctl disable --now" in uninstall_text
