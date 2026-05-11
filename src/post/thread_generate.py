#!/usr/bin/env python3
"""Thread (multi-segment) post generation pipeline.

Split out from src/post/post_generate.py to keep that file focused on the
single-post candidate→rerank→review→rewrite flow. Shared helpers
(cjk_weight, truncate_to_weight, topic_payload, chat_json_result) are
re-imported from post_generate so behavior stays identical.
"""
from __future__ import annotations

import json

from src.common import (
    THREAD_MAX_SEGMENT_CHARS,
    THREAD_MAX_SEGMENTS,
    THREAD_MIN_SEGMENTS,
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


THREAD_GENERATE_PROMPT = """你在为一个真实的 X 账号写一条帖串（thread），用 3-5 段连贯的推文把一个话题讲透。

输出严格 JSON：
{"segments": [{"text":"...", "position_hint":"..."}, ...], "thread_angle":"...", "thread_reason":"...", "image_query":"..."}

账号风格：
- 20 岁 founder / indie dev，长期混 X/Twitter
- 关注 AI、产品、分发、工作流、激励、工具体验
- 说话像真人账号，不像助手、不像顾问、不像内容农场

帖串规则：
- 段数 3-5 段，每段 60-280 中文字符。
- 第一段就是第一推，必须能独立吸引读者，像一条完整的好帖子。
- 每段都能单独阅读和理解，但串在一起有递进关系。
- 禁止用 1/ 2/ 3/ 或 ① ② ③ 等编号，段与段靠内容自然衔接。
- 禁止每段之间重复意思，禁止水字数。
- 前一段的结尾自然引出下一段的开头。
- 最后一段可以有总结或反问，但不能空洞。
- 不要写成文章拆分——这是帖串，不是博客。

段角色（每段给一个 position_hint，英文）：
- 第 1 段通常是 "hook" 或 "setup"
- 中间段通常是 "expansion", "evidence", "counterpoint" 等
- 最后一段通常是 "conclusion", "takeaway", "question" 等

总规则：
- 一律用中文。
- 必须挂在具体对象、事件、场景或使用体验上。
- 不要泛泛而谈，不要写成金句生成器。
- 不要写成公众号、周报、营销文案、鸡汤。
- 不要用 hashtag，不要 emoji，禁止用破折号（——），禁止用引号（包括中文引号 ""、英文引号 ""）。
- 禁止以抽象句式收尾（"这才是X""本质上""真正的问题"等）。
- thread_angle 和 thread_reason 用中文，各控制在 80 字内。

Image Rules（可选）：
- `image_query` 是英文生图 prompt（2-8 words），用于 AI 生成配图。
- 只有当你觉得配一张图能明显增强整个帖串的表现力时才设置，约 20-30% 适合配图。
- 大多数帖串不需要配图，设为 ""。
"""

THREAD_REVIEW_PROMPT = """你在做帖串发帖前的最后审稿，判断整个帖串是否适合发出去。

输出严格 JSON：
{"pass":true, "reason":"...", "rewrite_hint":"..."}

规则：
- `pass` 为 true 仅当整个帖串满足：
  1. 段数在 3-5 之间
  2. 每段内容具体、自然、像真人
  3. 段之间有递进关系，不是同一句话变体
  4. 没有 1/ 2/ 3/ 等编号
  5. 第一段能独立吸引读者
  6. 整体没有 AI 味、公众号味
- 如果只是个别句子略显工整但整体自然，不要判 false。
- `reason` 和 `rewrite_hint` 用中文，各控制在 80 字内。
"""

THREAD_REWRITE_PROMPT = """你在重写一条帖串，根据审稿意见改进。

输出严格 JSON：
{"segments": [{"text":"...", "position_hint":"..."}, ...], "reason":"..."}

规则：
- 保持 topic 核心意思。
- 必须按 rewrite_hint 改得更具体、更像真人帖串。
- 段数不变（保持 3-5 段）。
- 每段 60-280 中文字符。
- 禁止编号，段之间靠内容自然衔接。
- `reason` 用中文，简短说明重写补了什么。
"""


def normalize_thread_segments(segments: list[dict]) -> list[dict]:
    """Validate and normalize thread segments from LLM output.

    Truncates by X's weighted-character count (CJK weighs 2) rather than
    Python ``len()`` — otherwise a 280 CJK-char segment weighs 560 and the
    tweetButton stays disabled, silently breaking the post.
    """
    if not segments or len(segments) < THREAD_MIN_SEGMENTS:
        raise RuntimeError(f"Thread needs at least {THREAD_MIN_SEGMENTS} segments, got {len(segments) if segments else 0}")
    normalized = []
    for idx, item in enumerate(segments[:THREAD_MAX_SEGMENTS]):
        text = str((item or {}).get("text") or "").strip()
        if not text:
            raise RuntimeError(f"Segment {idx} has empty text")
        weight = cjk_weight(text)
        if weight > THREAD_MAX_SEGMENT_CHARS:
            truncated = truncate_to_weight(text, THREAD_MAX_SEGMENT_CHARS)
            logger.warning(
                "thread segment %d truncated by X weight: %d -> %d (chars %d -> %d)",
                idx,
                weight,
                cjk_weight(truncated),
                len(text),
                len(truncated),
            )
            text = truncated
        normalized.append({
            "index": idx,
            "text": text,
            "position_hint": str((item or {}).get("position_hint") or "").strip(),
        })
    if len(normalized) < THREAD_MIN_SEGMENTS:
        raise RuntimeError(f"After validation only {len(normalized)} valid segments remain")
    return normalized


def build_thread_generate_messages(topic: dict, persona_context: dict) -> list[dict]:
    payload = topic_payload(topic)
    references = recent_learning_references(limit=4)
    compact_refs = [
        {
            "post_text": str(item.get("post_text") or "")[:220],
            "format_guess": str(item.get("format_guess") or ""),
            "hook_type": str(item.get("hook_type") or ""),
            "style_summary": str(item.get("style_summary") or ""),
            "why_it_works": str(item.get("why_it_works") or ""),
            "imitation_takeaway": str(item.get("imitation_takeaway") or ""),
        }
        for item in references
    ]
    return [
        {"role": "system", "content": THREAD_GENERATE_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": f"写一条 {THREAD_MIN_SEGMENTS}-{THREAD_MAX_SEGMENTS} 段的帖串",
                    "topic": payload,
                    "persona_context": persona_context,
                    "requirements": [
                        f"每段 {THREAD_MIN_SEGMENTS}-{THREAD_MAX_SEGMENTS} 段，每段 60-280 中文字符",
                        "第一段要能独立吸引读者",
                        "段之间自然衔接，不要编号",
                        "挂在具体对象/事件/场景上",
                    ],
                    "recent_learning_references": compact_refs,
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]


def build_thread_review_messages(topic: dict, segments: list[dict], persona_context: dict) -> list[dict]:
    segments_text = "\n\n---\n\n".join(
        f"[段 {s['index']+1}] {s['position_hint']}\n{s['text']}"
        for s in segments
    )
    return [
        {"role": "system", "content": THREAD_REVIEW_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "topic": topic_payload(topic),
                    "segments": segments_text,
                    "segment_count": len(segments),
                    "recent_events": persona_context.get("recent_events", []),
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]


def build_thread_rewrite_messages(topic: dict, segments: list[dict], review_reason: str, rewrite_hint: str) -> list[dict]:
    segments_text = "\n\n---\n\n".join(
        f"[段 {s['index']+1}] {s['position_hint']}\n{s['text']}"
        for s in segments
    )
    return [
        {"role": "system", "content": THREAD_REWRITE_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "topic": topic_payload(topic),
                    "current_segments": segments_text,
                    "review_reason": review_reason,
                    "rewrite_hint": rewrite_hint,
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]


def generate_thread_plan(topic: dict) -> dict:
    """Generate a multi-segment thread plan. Different pipeline from single-post."""
    topic = normalize_post_topic(topic)
    persona_context = persona_context_dict()
    if not topic_summary_text(topic):
        raise RuntimeError("Empty topic.")

    gen_result, gen_payload = chat_json_result(
        build_thread_generate_messages(topic, persona_context),
        temperature=0.95,
        max_tokens=2000,
    )
    segments = normalize_thread_segments(gen_payload.get("segments") or [])
    thread_angle = str(gen_payload.get("thread_angle") or "").strip()
    thread_reason = str(gen_payload.get("thread_reason") or "").strip()
    image_query = str(gen_payload.get("image_query") or "").strip()

    review_result, review_payload = chat_json_result(
        build_thread_review_messages(topic, segments, persona_context),
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
            build_thread_rewrite_messages(topic, segments, review_reason, review_rewrite_hint),
            temperature=0.85,
            max_tokens=2000,
        )
        segments = normalize_thread_segments(rewrite_payload.get("segments") or [])
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
        "thread_mode": True,
        "segments": segments,
        "thread_angle": thread_angle,
        "thread_reason": thread_reason,
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
