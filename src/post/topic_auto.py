#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime

from src.common import chat_json_result, normalize_post_topic
from src.persona_store import get_generation_context

AUTO_TOPIC_PROMPT = """你是一个真实 X 账号的主人，正在决定今天想发什么。

输出严格 JSON：
{"type":"casual|argument|story|news_react", "text":"...", "angle":"...", "reason":"..."}

选题规则：
- 从 persona_context.static.content_pillars 里选一个最近帖子（recent_posts）覆盖较少的方向。
- 如果 recent_events 不为空：从事件里找灵感，不要复述，而是"因为这件事想到了什么"。
- 如果 recent_events 为空：从 background 和 current_projects 推断这个人最近在想什么。
- 禁止选最近三条帖子已经说过的角度或相近主题。
- 禁止泛泛大话题，必须有一个具体切入点。
- type 与内容匹配：随手感受用 casual，有论点用 argument，有故事用 story，有新闻用 news_react。
- text：选题描述，一句话，50 字以内，说清楚想说什么。
- angle：具体切入角度，20 字以内。
- reason：为什么现在想说这个，20 字以内。
- 全部用中文。
- 说话风格遵循 persona_context.static.voice。
"""


def generate_auto_topic() -> dict:
    ctx = get_generation_context()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    result = chat_json_result(
        [
            {"role": "system", "content": AUTO_TOPIC_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "today": today,
                        "persona_context": ctx,
                        "task": "决定今天最想发什么，输出一个选题",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ],
        temperature=0.9,
        max_tokens=300,
    )
    payload = result["payload"]
    now_stamp = datetime.now().astimezone().strftime("%Y%m%d%H%M%S")
    raw_text = str(payload.get("text") or "").strip()
    topic = normalize_post_topic(
        {
            "id": f"auto-{now_stamp}",
            "type": str(payload.get("type") or "argument").strip(),
            "text": raw_text,
            "angle": str(payload.get("angle") or "").strip(),
            "source": "auto",
            "status": "pending",
            "subject": "",
            "event_or_context": str(payload.get("reason") or "").strip(),
            "stance": raw_text,
            "evidence_hint": str(payload.get("angle") or "").strip(),
        }
    )
    if not topic.get("text"):
        raise RuntimeError(f"Auto-topic returned empty text: {payload}")
    return topic
