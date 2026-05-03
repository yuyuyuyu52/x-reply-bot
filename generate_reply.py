#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from common import (
    SELECTED_PATH,
    chat_json_result,
    ensure_state_dirs,
    load_env_file,
    load_json,
)

DEFAULT_PROMPT = """You write short, natural X replies.

Return strict JSON:
{"reply":"...", "reason":"..."}

Rules:
- Reply in the same language as the post.
- Sound like a real long-time X/Twitter user replying in public, not like an assistant.
- Prefer one sharp point over a balanced summary.
- It is okay to sound opinionated, as long as the reply still feels natural.
- If the post is emotional or rant-like, match that energy lightly without becoming theatrical.
- If the post overstates the case, narrow it with one short sentence instead of explaining at length.
- Avoid generic filler like: "确实", "有道理", "本质上", "关键还是", "归根结底", "值得思考".
- Avoid standard-model-answer phrasing.
- No hashtags, no emojis, no em dashes (——), no marketing tone.
- Chinese replies must stay under 70 characters.
- English replies must stay under 35 words.
- `reason` must be written in Chinese and should briefly state what concrete angle the reply adds.
- Avoid AI-sounding phrasing.
- The reply should feel like something a real account would casually post, not something polished for publication.
"""


def build_messages(post: dict, system_prompt: str) -> list[dict]:
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
        {"role": "system", "content": system_prompt},
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


def generate_reply_payload(post: dict, system_prompt: str = DEFAULT_PROMPT) -> dict:
    result = chat_json_result(build_messages(post, system_prompt), temperature=0.7, max_tokens=520)
    payload = result["payload"]
    reply = str(payload.get("reply") or "").strip().replace("\n", " ")
    reason = str(payload.get("reason") or "").strip().replace("\n", " ")
    if not reply:
        raise RuntimeError(f"Model returned empty reply payload: {payload}")
    return {
        "reply": reply,
        "reason": reason,
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
    selected = load_json(SELECTED_PATH, {})
    if not selected.get("ok"):
        print("No prepared post found. Run prepare_post.py first.")
        return 2

    payload = generate_reply_payload(selected, args.system_prompt)
    if args.text_only:
        print(payload["reply"])
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
