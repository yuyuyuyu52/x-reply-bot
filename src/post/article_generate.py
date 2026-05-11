#!/usr/bin/env python3
"""Article (long-form X article) post generation pipeline.

Split out from src/post/post_generate.py to keep that file focused on the
single-post candidate→rerank→review→rewrite flow. Shared helpers
(cjk_weight, truncate_to_weight, topic_payload, chat_json_result) are
re-imported from post_generate so behavior stays identical.
"""
from __future__ import annotations

import json

from src.common import (
    normalize_post_topic,
    topic_summary_text,
)
from src.context_builder import persona_context_dict
from src.learning.store import recent_learning_references
from src.logger import get_logger
from src.post.post_generate import (
    chat_json_result,
    cjk_weight,
    topic_payload,
    truncate_to_weight,
)

logger = get_logger(__name__)


ARTICLE_GENERATE_PROMPT = """你在为一个真实的 X 账号写一篇 X 文章（Article），发布到时间线上。

输出严格 JSON：
{"title":"...", "body":"...", "image_query":"...", "article_reason":"..."}

账号风格：
- 20 岁 founder / indie dev，长期混 X/Twitter
- 关注 AI、产品、分发、工作流、激励、工具体验
- 说话像真人账号，不像助手、不像顾问、不像内容农场

文章规则：
- title：标题，一句话概括核心观点或钩子，20-40 中文字符。必须让人想点进去看。
- body：正文。一句话一行，段落之间空一行。像真人在 X 上写文章一样自然，不要像博客。
- 长度：正文 300-800 中文字符。要有实质内容，不是凑字数。
- 结构：开头 hook → 中间展开论证/举例 → 结尾总结或反问。但不要明显三段式。
- article_reason：为什么想写这篇文章，用中文，60 字内。
- image_query：英文生图 prompt（2-8 words），约 30% 的文章适合配图。

总规则：
- 一律用中文。
- 必须挂在具体对象、事件、场景或使用体验上。
- 不要泛泛而谈，不要写成金句生成器。
- 不要写成公众号、周报、营销文案、鸡汤。
- 不要用 hashtag，不要 emoji，禁止用破折号（——），禁止用引号（包括中文引号 ""、英文引号 ""）。
- 禁止以抽象句式收尾。
"""

ARTICLE_REVIEW_PROMPT = """你在做文章发帖前的最后审稿，判断这篇文章是否适合发出去。

输出严格 JSON：
{"pass":true, "reason":"...", "rewrite_hint":"..."}

规则：
- `pass` 为 true 仅当：
  1. 标题有钩子，让人想点进去
  2. 正文具体、自然、像真人写的
  3. 结构有递进但不是三段式模板
  4. 没有 AI 味、公众号味
  5. 标题和正文匹配，没有标题党
- `reason` 和 `rewrite_hint` 用中文，各 80 字内。
"""

ARTICLE_REWRITE_PROMPT = """你在重写一篇 X 文章，根据审稿意见改进。

输出严格 JSON：
{"title":"...", "body":"...", "reason":"..."}

规则：
- 保持原 topic 核心意思。
- 必须按 rewrite_hint 改进标题或正文。
- 标题 20-40 中文字符，正文 300-800 中文字符。
- 一句话一行，段落之间空一行。
- `reason` 用中文，简短说明重写改了什么。
"""


def normalize_article(title: str, body: str) -> dict:
    """Validate article title and body from LLM output.

    Title length is truncated by X's weighted-character count (CJK = 2)
    so the title input never silently rejects. Body is truncated by raw
    char count (X articles don't enforce the tweet weight limit, but we
    cap to keep notification payloads reasonable).
    """
    title = title.strip()
    body = body.strip()
    if not title:
        raise RuntimeError("Article title is empty")
    if not body:
        raise RuntimeError("Article body is empty")
    title_weight = cjk_weight(title)
    if title_weight > 80:
        truncated_title = truncate_to_weight(title, 80)
        logger.warning(
            "article title truncated by X weight: %d -> %d (chars %d -> %d)",
            title_weight,
            cjk_weight(truncated_title),
            len(title),
            len(truncated_title),
        )
        title = truncated_title
    if len(body) > 1500:
        logger.warning("article body truncated: %d -> 1500 chars", len(body))
        body = body[:1500]
    return {"title": title, "body": body}


def build_article_generate_messages(topic: dict, persona_context: dict) -> list[dict]:
    payload = topic_payload(topic)
    references = recent_learning_references(limit=3)
    compact_refs = [
        {
            "post_text": str(item.get("post_text") or "")[:220],
            "style_summary": str(item.get("style_summary") or ""),
            "why_it_works": str(item.get("why_it_works") or ""),
        }
        for item in references
    ]
    return [
        {"role": "system", "content": ARTICLE_GENERATE_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "写一篇 X 文章",
                    "topic": payload,
                    "persona_context": persona_context,
                    "requirements": [
                        "标题 20-40 字，有钩子",
                        "正文 300-800 字，一句话一行，段间空行",
                        "挂在具体对象/事件/场景上",
                    ],
                    "recent_learning_references": compact_refs,
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]


def build_article_review_messages(topic: dict, title: str, body: str, persona_context: dict) -> list[dict]:
    return [
        {"role": "system", "content": ARTICLE_REVIEW_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "topic": topic_payload(topic),
                    "title": title,
                    "body": body,
                    "body_length": len(body),
                    "recent_events": persona_context.get("recent_events", []),
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]


def build_article_rewrite_messages(topic: dict, title: str, body: str, review_reason: str, rewrite_hint: str) -> list[dict]:
    return [
        {"role": "system", "content": ARTICLE_REWRITE_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "topic": topic_payload(topic),
                    "current_title": title,
                    "current_body": body,
                    "review_reason": review_reason,
                    "rewrite_hint": rewrite_hint,
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]


def generate_article_plan(topic: dict) -> dict:
    """Generate an article plan. Different pipeline from single-post and thread."""
    topic = normalize_post_topic(topic)
    persona_context = persona_context_dict()
    if not topic_summary_text(topic):
        raise RuntimeError("Empty topic.")

    gen_result, gen_payload = chat_json_result(
        build_article_generate_messages(topic, persona_context),
        temperature=0.95,
        max_tokens=2000,
    )
    title = str(gen_payload.get("title") or "").strip()
    body = str(gen_payload.get("body") or "").strip()
    image_query = str(gen_payload.get("image_query") or "").strip()
    article_reason = str(gen_payload.get("article_reason") or "").strip()
    article = normalize_article(title, body)

    review_result, review_payload = chat_json_result(
        build_article_review_messages(topic, article["title"], article["body"], persona_context),
        temperature=0.2,
        max_tokens=720,
    )
    review_pass = bool(review_payload.get("pass"))
    review_reason = str(review_payload.get("reason") or "").strip()
    review_rewrite_hint = str(review_payload.get("rewrite_hint") or "").strip()

    rewrite_usage = {}
    rewrite_cost = {}
    rewritten = False
    if not review_pass:
        rewrite_result, rewrite_payload = chat_json_result(
            build_article_rewrite_messages(topic, article["title"], article["body"], review_reason, review_rewrite_hint),
            temperature=0.85,
            max_tokens=2000,
        )
        article = normalize_article(
            str(rewrite_payload.get("title") or "").strip(),
            str(rewrite_payload.get("body") or "").strip(),
        )
        rewrite_usage = rewrite_result["usage"]
        rewrite_cost = rewrite_result["cost"]
        rewritten = True

    total_cost = round(
        float(gen_result["cost"].get("total_cost") or 0.0)
        + float(review_result["cost"].get("total_cost") or 0.0)
        + float(rewrite_cost.get("total_cost") or 0.0),
        8,
    )

    return {
        "topic": topic,
        "article_mode": True,
        "title": article["title"],
        "body": article["body"],
        "article_reason": article_reason,
        "image_query": image_query,
        "review_pass": review_pass,
        "review_reason": review_reason,
        "review_rewrite_hint": review_rewrite_hint,
        "rewritten": rewritten,
        "generate_usage": gen_result["usage"],
        "generate_cost": gen_result["cost"],
        "review_usage": review_result["usage"],
        "review_cost": review_result["cost"],
        "rewrite_usage": rewrite_usage,
        "rewrite_cost": rewrite_cost,
        "total_cost_cny": total_cost,
    }
