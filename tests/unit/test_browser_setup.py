"""Browser dependency bootstrap defaults and script smoke tests."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import src.common  # noqa: F401  # Import common first to match normal entrypoint loading.
from src import harness


ROOT = Path(__file__).resolve().parents[2]


def test_browser_harness_defaults_are_repo_local(monkeypatch):
    monkeypatch.delenv("BROWSER_HARNESS_BIN", raising=False)
    monkeypatch.delenv("BROWSER_HARNESS_ROOT", raising=False)

    assert harness.browser_harness_bin() == str(ROOT / ".bin" / "browser-harness")
    assert harness.browser_harness_root() == ROOT / "vendor" / "browser-harness"


def test_browser_harness_bin_prefers_repo_wrapper_over_global_binary(monkeypatch):
    monkeypatch.delenv("BROWSER_HARNESS_BIN", raising=False)

    assert harness.browser_harness_bin() == str(ROOT / ".bin" / "browser-harness")


def test_restart_harness_daemon_uses_vendored_src_layout(monkeypatch, tmp_path):
    harness_root = tmp_path / "browser-harness"
    harness_root.mkdir()
    calls = []

    monkeypatch.setenv("BROWSER_HARNESS_ROOT", str(harness_root))
    monkeypatch.setattr(
        harness.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    harness.restart_harness_daemon("test-bot")

    assert calls
    args, kwargs = calls[0]
    script = args[0][2]
    assert "sys.path.insert(0," in script
    assert str(harness_root / "src") in script
    assert "from browser_harness.admin import restart_daemon" in script
    assert kwargs["cwd"] == str(harness_root)


@pytest.mark.parametrize(
    "script",
    [
        "scripts/bootstrap_browser.sh",
        "scripts/start_chrome.sh",
        "scripts/status_browser.sh",
    ],
)
def test_browser_setup_scripts_pass_bash_syntax_check(script):
    subprocess.run(["bash", "-n", str(ROOT / script)], check=True)


def test_bootstrap_script_writes_repo_local_harness_env_defaults():
    script = (ROOT / "scripts" / "bootstrap_browser.sh").read_text(encoding="utf-8")

    assert 'BROWSER_HARNESS_ROOT="${BROWSER_HARNESS_ROOT:-$X_REPLY_ROOT/vendor/browser-harness}"' in script
    assert 'BROWSER_HARNESS_BIN="${BROWSER_HARNESS_BIN:-$X_REPLY_ROOT/.bin/browser-harness}"' in script
    assert "git clone" not in script
    assert "/home/will" not in script


def test_start_chrome_uses_macos_app_launcher_on_darwin():
    script = (ROOT / "scripts" / "start_chrome.sh").read_text(encoding="utf-8")

    assert 'if [[ "$(uname -s)" == "Darwin" ]]' in script
    assert 'open -na "$chrome_app_name" --args' in script
