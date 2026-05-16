import sys

import src.common  # noqa: F401 - initialize common before send_reply imports logger
from src.reply import send_reply


def test_generated_harness_code_compiles_for_reply_with_python_literals(monkeypatch):
    def fake_run_harness(code):
        assert "return_reply_url = False" in code
        compile(code, "<send-reply-harness>", "exec")
        return '{"ok": true}\n'

    monkeypatch.setattr(send_reply, "run_harness", fake_run_harness)
    monkeypatch.setattr(send_reply, "ensure_state_dirs", lambda: None)
    monkeypatch.setattr(send_reply, "append_log", lambda entry: None)
    monkeypatch.setattr(send_reply, "load_json", lambda path, default: default)
    monkeypatch.setattr(send_reply, "write_json", lambda path, data: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "send_reply.py",
            "--url",
            "https://x.com/example/status/123",
            "--reply",
            "增量索引这个设计挺聪明的",
            "--action",
            "reply",
        ],
    )

    assert send_reply.main() == 0


def test_return_reply_url_still_records_original_post_for_dedup(monkeypatch):
    writes = []

    def fake_run_harness(code):
        assert "return_reply_url = True" in code
        compile(code, "<send-reply-harness>", "exec")
        return '{"ok": true}\nREPLY_URL: https://x.com/me/status/456\n'

    monkeypatch.setattr(send_reply, "run_harness", fake_run_harness)
    monkeypatch.setattr(send_reply, "ensure_state_dirs", lambda: None)
    monkeypatch.setattr(send_reply, "append_log", lambda entry: None)
    monkeypatch.setattr(send_reply, "load_json", lambda path, default: {"posts": []})
    monkeypatch.setattr(send_reply, "write_json", lambda path, data: writes.append(data))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "send_reply.py",
            "--url",
            "https://x.com/example/status/123",
            "--reply",
            "This is a short reply.",
            "--action",
            "reply",
            "--return-reply-url",
        ],
    )

    assert send_reply.main() == 0
    assert writes
    assert "https://x.com/example/status/123" in writes[-1]["posts"]
