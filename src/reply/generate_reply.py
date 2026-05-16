#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime

from src.logger import get_logger

logger = get_logger(__name__)

from src.common import (
    SELECTED_PATH,
    chat_json_result,
    count_daily_reposts,
    ensure_state_dirs,
    load_env_file,
    load_json,
    quote_enabled,
    repost_daily_limit,
    repost_enabled,
)
from src.context_builder import build_feedback_context, build_learning_context, build_persona_context

DEFAULT_PROMPT = """You evaluate an X post to decide whether to reply, quote tweet, or retweet (repost).

Return strict JSON:
{"action": "reply" | "quote" | "repost", "text":"...", "reason":"...", "like": true/false}

Action Rules:
- "reply": You want to talk directly to the author or participants in the thread.
- "quote": The post is worth sharing with your followers — add your own sharp take or context to make it useful.
- "repost": The post is genuinely interesting and speaks for itself, share it as-is without adding text. Use this sparingly, maybe once or twice a week, for posts that truly deserve it.

Which action to choose:
- Consider the interaction style: is this a post where adding YOUR voice makes it better (reply/quote), or is it already perfect (repost)?
- Vary your actions across runs. Don't always reply — sometimes quote or repost to keep your timeline natural and human-like.

Like Rules:
- `like` should be true when you genuinely appreciate the post (about 40-60% of the time).
- Don't like every single post; be selective. If the post is mediocre or you're just being polite, set it to false.
- You can like regardless of which action you choose (reply, quote, or repost).

Text Rules:
- If action is "repost", `text` should be "".
- If action is "reply" or "quote", write the content in `text`.
- Reply/Quote in the same language as the post.
- Sound like a real long-time X/Twitter user, not like an assistant.
- Prefer one sharp point over a balanced summary.
- It is okay to sound opinionated, as long as it feels natural.
- Avoid generic filler like: "确实", "有道理", "本质上", "关键还是", "归根结底", "值得思考".
- No hashtags, no emojis, no em dashes (——), no marketing tone.
- Chinese text must stay under 70 characters.
- English text must stay under 35 words.
- `reason` must be written in Chinese and briefly state your action choice and concrete angle.
"""


X_TWITTER_STYLE_PROMPT = """X/Twitter Style Rules:
- Brevity is mandatory. Use one sentence when one sentence works.
- Speak like a peer with specific taste, not like an assistant.
- Share a direct opinion or an uncomfortable truth when the post gives you a real angle.
- Prefer concrete judgment over balanced analysis.
- Parentheses are allowed for side-comments or internal thoughts, but don't force them.

Avoid:
- The "Question? Answer." trope, e.g. "The result? Better sales."
- "Picture this", "In the realm of", or similar essay openers.
- "It's not just A, it's B" structure.
- Generic agreement, summary, or advice-column tone.
"""


def _language_counts(text: str) -> dict[str, int]:
    return {
        "han": len(re.findall(r"[\u4e00-\u9fff]", text or "")),
        "latin": len(re.findall(r"[A-Za-z]", text or "")),
    }


def detect_reply_language(post: dict) -> str:
    """Detect reply language from the main post, not quoted context."""
    text = str(post.get("main_post_text") or "").strip()
    counts = _language_counts(text)
    han = counts["han"]
    latin = counts["latin"]
    if latin >= 20 and latin >= han * 2:
        return "en"
    if han >= 4 and han >= latin / 2:
        return "zh"
    if han and latin:
        return "mixed"
    if latin:
        return "en"
    if han:
        return "zh"
    return "unknown"


def reply_language_instruction(target_language: str) -> str:
    if target_language == "en":
        return (
            "Target reply language: English.\n"
            "- The main post is English. Write `text` in English only.\n"
            "- Do not write the reply/quote text in Chinese, even if persona, feedback, UI, or other context is Chinese.\n"
            "- `reason` must still be written in Chinese."
        )
    if target_language == "zh":
        return (
            "Target reply language: Chinese.\n"
            "- The main post is Chinese. Write `text` in Chinese, allowing normal English product/model names when natural.\n"
            "- `reason` must be written in Chinese."
        )
    if target_language == "mixed":
        return (
            "Target reply language: match the main post's Chinese/English mix.\n"
            "- Follow the main post language balance, not the persona or feedback context.\n"
            "- `reason` must be written in Chinese."
        )
    return (
        "Target reply language: infer from the main post only.\n"
        "- Do not let quoted-post text, UI text, persona, or feedback context decide the reply language.\n"
        "- `reason` must be written in Chinese."
    )


def reply_language_matches(target_language: str, text: str) -> bool:
    if target_language not in {"en", "zh"}:
        return True
    counts = _language_counts(text)
    han = counts["han"]
    latin = counts["latin"]
    if target_language == "en":
        return han < 4
    return han >= 2 or latin < 20


def _merge_numeric_dicts(*items: dict) -> dict:
    merged: dict = {}
    for item in items:
        for key, value in (item or {}).items():
            if isinstance(value, bool):
                merged.setdefault(key, value)
            elif isinstance(value, (int, float)):
                merged[key] = merged.get(key, 0) + value
            else:
                merged[key] = value
    if "total_cost" in merged:
        merged["total_cost"] = round(float(merged["total_cost"]), 8)
    return merged


def build_messages(post: dict, system_prompt: str) -> list[dict]:
    learning_ctx = build_learning_context()
    persona_ctx = build_persona_context()
    feedback_ctx = build_feedback_context()
    target_language = detect_reply_language(post)

    system_parts = [system_prompt, X_TWITTER_STYLE_PROMPT]
    if learning_ctx:
        system_parts.append(learning_ctx)
    if persona_ctx:
        system_parts.append(persona_ctx)
    if feedback_ctx:
        system_parts.append(feedback_ctx)
    if learning_ctx:
        system_parts.append(
            "根据这条帖子的话题，优先使用上面规律中效果最好的 hook 类型开头，而不是写通用答案。"
        )
    system_parts.append(reply_language_instruction(target_language))

    full_system = "\n\n".join(system_parts)

    post_text = (post.get("main_post_text") or "").strip()
    quoted_post_text = (post.get("quoted_post_text") or "").strip()
    url = (post.get("url") or "").strip()
    quote_note = ""
    if quoted_post_text:
        quote_note = (
            "\n\nThis post quotes another post.\n"
            "Reply target: the main post author and main post text.\n"
            "Quoted post: context only. Do not write as if you are replying to the quoted author.\n\n"
            "Quoted post text:\n"
            f"{quoted_post_text}"
        )
    return [
        {"role": "system", "content": full_system},
        {
            "role": "user",
            "content": (
                "Write a reply for this X post.\n\n"
                f"Target reply language: {target_language}\n\n"
                f"URL: {url}\n\n"
                "Main post (this is the post you are replying to):\n"
                f"{post_text}"
                f"{quote_note}"
            ),
        },
    ]


def generate_reply_payload(post: dict, system_prompt: str = DEFAULT_PROMPT, allowed_actions: list[str] | None = None) -> dict:
    target_language = detect_reply_language(post)
    messages = build_messages(post, system_prompt)
    result = chat_json_result(messages, temperature=0.7, max_tokens=520)
    usage = result["usage"]
    cost = result["cost"]
    payload = result["payload"]
    action = str(payload.get("action") or "reply").strip().lower()
    if action not in ["reply", "quote", "repost"]:
        action = "reply"
    if allowed_actions and action not in allowed_actions:
        logger.warning("action=%s not in allowed=%s, falling back to reply", action, allowed_actions)
        action = "reply"
    
    text = str(payload.get("text") or payload.get("reply") or "").strip().replace("\n", " ")
    reason = str(payload.get("reason") or "").strip().replace("\n", " ")
    like = bool(payload.get("like")) if "like" in payload else False

    if action in ["reply", "quote"] and not text:
        raise RuntimeError(f"Model returned empty text for {action}: {payload}")
    if action in ["reply", "quote"] and not reply_language_matches(target_language, text):
        retry_result = chat_json_result(
            messages
            + [
                {
                    "role": "system",
                    "content": (
                        "上一条输出的 `text` 语言不符合目标主帖语言。"
                        "请重新输出完整 JSON，并严格遵守 Target reply language。"
                        "如果目标语言是 English，`text` 只能用英文；`reason` 仍用中文。"
                    ),
                }
            ],
            temperature=0.4,
            max_tokens=520,
        )
        usage = _merge_numeric_dicts(usage, retry_result["usage"])
        cost = _merge_numeric_dicts(cost, retry_result["cost"])
        payload = retry_result["payload"]
        action = str(payload.get("action") or action).strip().lower()
        if action not in ["reply", "quote", "repost"]:
            action = "reply"
        if allowed_actions and action not in allowed_actions:
            logger.warning("retry action=%s not in allowed=%s, falling back to reply", action, allowed_actions)
            action = "reply"
        text = str(payload.get("text") or payload.get("reply") or "").strip().replace("\n", " ")
        reason = str(payload.get("reason") or reason).strip().replace("\n", " ")
        like = bool(payload.get("like")) if "like" in payload else like
        if action in ["reply", "quote"] and not text:
            raise RuntimeError(f"Model returned empty text for {action} after language retry: {payload}")
        if action in ["reply", "quote"] and not reply_language_matches(target_language, text):
            raise RuntimeError(f"Model returned {target_language} language mismatch for {action}: {payload}")

    return {
        "action": action,
        "reply": text,
        "reason": reason,
        "like": like,
        "target_language": target_language,
        "source_post_url": str(post.get("url") or "").strip(),
        "selection_id": str(post.get("selection_id") or "").strip(),
        "usage": usage,
        "cost": cost,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--system-prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--text-only", action="store_true")
    args = parser.parse_args()

    load_env_file()
    ensure_state_dirs()

    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    allowed_actions = ["reply"]
    restrictions = []
    if quote_enabled():
        allowed_actions.append("quote")
    else:
        restrictions.append('"quote" 已被配置禁用，不要选择它。')
    if repost_enabled():
        limit = repost_daily_limit()
        count = count_daily_reposts(today)
        if count < limit:
            allowed_actions.append("repost")
        else:
            restrictions.append(f'"repost" 今日次数已达上限 ({limit}/{limit})，今天不要再选了。')
    else:
        restrictions.append('"repost" 已被配置禁用，不要选择它。')

    system_prompt = args.system_prompt
    if restrictions:
        system_prompt = system_prompt + "\n\n" + "\n".join(f"- {r}" for r in restrictions)
    selected = load_json(SELECTED_PATH, {})
    if not selected.get("ok"):
        logger.warning("no prepared post found")
        print("No prepared post found. Run prepare_post.py first.")
        return 2

    payload = generate_reply_payload(selected, system_prompt, allowed_actions)
    if args.text_only:
        print(payload["reply"])
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
