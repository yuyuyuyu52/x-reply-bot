#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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




def build_messages(post: dict, system_prompt: str) -> list[dict]:
    learning_ctx = build_learning_context()
    persona_ctx = build_persona_context()
    feedback_ctx = build_feedback_context()

    system_parts = [system_prompt]
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
                f"URL: {url}\n\n"
                "Main post (this is the post you are replying to):\n"
                f"{post_text}"
                f"{quote_note}"
            ),
        },
    ]


def generate_reply_payload(post: dict, system_prompt: str = DEFAULT_PROMPT, allowed_actions: list[str] | None = None) -> dict:
    result = chat_json_result(build_messages(post, system_prompt), temperature=0.7, max_tokens=520)
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

    return {
        "action": action,
        "reply": text,
        "reason": reason,
        "like": like,
        "source_post_url": str(post.get("url") or "").strip(),
        "selection_id": str(post.get("selection_id") or "").strip(),
        "usage": result["usage"],
        "cost": result["cost"],
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
