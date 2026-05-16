from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.reply.generate_reply import (  # noqa: E402
    build_messages,
    detect_reply_language,
    generate_reply_payload,
    reply_language_matches,
)


def test_detect_reply_language_uses_main_post_not_quoted_post():
    post = {
        "main_post_text": "Just got Codex Pro, now how do I make $1M/month?",
        "quoted_post_text": "这个中文引用只是上下文，不应该决定回复语言",
    }

    assert detect_reply_language(post) == "en"


def test_build_messages_explicitly_names_english_target_language():
    post = {
        "url": "https://x.com/alice/status/123",
        "main_post_text": "Just got Codex Pro, now how do I make $1M/month?",
        "quoted_post_text": "",
    }

    with (
        patch("src.reply.generate_reply.build_learning_context", return_value=""),
        patch("src.reply.generate_reply.build_persona_context", return_value="中文 persona context"),
        patch("src.reply.generate_reply.build_feedback_context", return_value=""),
    ):
        messages = build_messages(post, "base prompt")

    joined = "\n\n".join(str(item["content"]) for item in messages)
    assert "Target reply language: English" in joined
    assert "Do not write the reply/quote text in Chinese" in joined


def test_build_messages_includes_x_twitter_style_rules():
    post = {
        "url": "https://x.com/alice/status/123",
        "main_post_text": "Just got Codex Pro, now how do I make $1M/month?",
        "quoted_post_text": "",
    }

    with (
        patch("src.reply.generate_reply.build_learning_context", return_value=""),
        patch("src.reply.generate_reply.build_persona_context", return_value=""),
        patch("src.reply.generate_reply.build_feedback_context", return_value=""),
    ):
        messages = build_messages(post, "base prompt")

    system = str(messages[0]["content"])
    assert "X/Twitter Style Rules:" in system
    assert "Brevity is mandatory. Use one sentence when one sentence works." in system
    assert "Speak like a peer with specific taste, not like an assistant." in system
    assert 'The "Question? Answer." trope' in system
    assert '"It\'s not just A, it\'s B" structure' in system


def test_reply_language_matches_blocks_chinese_for_english_target():
    assert reply_language_matches("en", "Step 1: close this tab. Step 2: build something.")
    assert not reply_language_matches("en", "这个问题其实很简单，先关掉这个页面去做产品")


def test_generate_reply_retries_language_mismatch_before_returning():
    post = {
        "url": "https://x.com/alice/status/123",
        "selection_id": "sel-1",
        "main_post_text": "Just got Codex Pro, now how do I make $1M/month?",
    }
    calls = [
        {
            "payload": {
                "action": "reply",
                "text": "这个问题其实很简单，先关掉这个页面去做产品",
                "reason": "第一次跑偏成中文",
                "like": False,
            },
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "cost": {"total_cost": 0.1},
        },
        {
            "payload": {
                "action": "reply",
                "text": "Close this tab first. Build something real for six months.",
                "reason": "重试后改为英文",
                "like": False,
            },
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            "cost": {"total_cost": 0.2},
        },
    ]

    with (
        patch("src.reply.generate_reply.build_learning_context", return_value=""),
        patch("src.reply.generate_reply.build_persona_context", return_value="中文 persona context"),
        patch("src.reply.generate_reply.build_feedback_context", return_value=""),
        patch("src.reply.generate_reply.chat_json_result", side_effect=calls) as chat,
    ):
        payload = generate_reply_payload(post)

    assert chat.call_count == 2
    assert payload["reply"] == "Close this tab first. Build something real for six months."
    assert payload["target_language"] == "en"
    assert payload["usage"]["prompt_tokens"] == 3
    assert payload["usage"]["completion_tokens"] == 2
    assert payload["usage"]["total_tokens"] == 5
    assert payload["cost"]["total_cost"] == 0.3
