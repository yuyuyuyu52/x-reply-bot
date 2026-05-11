#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from src.common import (
    THREAD_MAX_SEGMENT_CHARS,
    THREAD_MAX_SEGMENTS,
    THREAD_MIN_SEGMENTS,
    chat_text_result,
    ensure_state_dirs,
    load_env_file,
    next_pending_post_topic,
    normalize_post_topic,
    parse_json_object,
    topic_summary_text,
)
from src.image_search import image_search_available
from src.learning.store import recent_learning_references
from src.context_builder import build_feedback_context, build_learning_context, build_persona_context, persona_context_dict
from src.logger import get_logger

logger = get_logger(__name__)


def cjk_weight(s: str) -> int:
    """X-style character weight: CJK chars count as 2, others as 1.

    X's tweet-length limit is based on weighted character count; CJK
    (Chinese / Japanese / Korean) characters count double. A 280-char
    pure-CJK string therefore weighs 560 and the tweetButton stays
    disabled. This mirrors how X computes ``tweetTextarea_0`` length.
    """
    total = 0
    for ch in s:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF  # CJK Unified Ideographs (一-鿿)
            or 0x3040 <= cp <= 0x30FF  # Hiragana + Katakana (぀-ヿ)
            or 0xAC00 <= cp <= 0xD7AF  # Hangul Syllables (가-힯)
        ):
            total += 2
        else:
            total += 1
    return total


def truncate_to_weight(s: str, max_weight: int) -> str:
    """Truncate ``s`` so its X-style weight does not exceed ``max_weight``.

    Returns the prefix; caller is responsible for logging the truncation.
    """
    total = 0
    out: list[str] = []
    for ch in s:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF
            or 0x3040 <= cp <= 0x30FF
            or 0xAC00 <= cp <= 0xD7AF
        ):
            w = 2
        else:
            w = 1
        if total + w > max_weight:
            break
        out.append(ch)
        total += w
    return "".join(out)

CANDIDATE_PROMPT = """你在为一个真实的 X 账号写主动发帖。

输出严格 JSON：
{
  "candidates": [
    {"text":"...", "angle":"...", "reason":"...", "image_query":"..."},
    {"text":"...", "angle":"...", "reason":"...", "image_query":"..."},
    {"text":"...", "angle":"...", "reason":"...", "image_query":"..."}
  ]
}

账号风格：
- 20 岁 founder / indie dev
- 长期混 X/Twitter
- 关注 AI、产品、分发、工作流、激励、工具体验
- 说话像真人账号，不像助手，不像顾问，不像内容农场

总规则：
- 一律用中文。
- 不要泛泛而谈，必须挂在具体对象、事件、场景或使用体验上。
- 不要写成金句生成器，不要只输出抽象判断。
- 不要写成公众号、周报、营销文案、鸡汤。
- 不要用 hashtag，不要 emoji，禁止用破折号（——），禁止用引号（包括中文引号 ""、英文引号 ""）。
- 禁止默认用这些句式起手：很多……、本质上……、归根结底……、真正的……、问题不在……而在……。
- 禁止以"这才是X""归根结底""本质上""真正的问题""说到底"等抽象句式收尾；帖子可以没有结论，以感受、细节或疑问结束都行。
- 不要写完整三段式（背景→分析→总结），写一两段就够；句子允许不对称、不整齐。
- 格式：一句话一行。不要写成一整段，用换行隔开每句话。段落之间空一行。像真人发帖一样自然断开，而不是堆成一大坨文字。
- 时间锚规则：如果要用"上周""昨天""最近""前几天""上个月"，必须来自 persona_context.recent_events；如果该列表为空，改用无时间锚的当下观察或感受。
- 禁止伪造时间长度，如"试了两天""用了三周"等——你刚从热点发现这个信息，不可能已经用了很久。用"试了下""看了下"或直接说观察到的事即可。
- 三条候选的角度必须明显不同，不能只是同一句话改写。
- `reason` 和 `angle` 都用中文。

按类型写：
- news_react：针对一个热点/新能力/新闻。如果 topic 标注"今天"，用"今天看到""刚刷到""看到有人说"开头，体现出你在实时跟进热点，不要用"最近重翻""前阵子"这种不新鲜的口吻。先点明发生了什么，再说为什么重要。主力长度 120-220 字。
- story：叙述一件具体的事或一次具体经历，再落到判断。主力长度 120-220 字。
- argument：明确表达一个观点，并给出 2-3 层具体论证。主力长度 120-220 字。
- casual：像顺手发一条，但也必须挂在具体对象或语境上。长度 35-90 字。

长度规则：
- casual 之外，优先写成单条中长帖，允许分成 2-4 个短段。
- 不要写 thread，不要编号列表，不要拉得像文章。
- 如果提供了近期高质量帖子学习样本，只学习它们的动作、节奏、结构、hook，不要复述，不要洗稿。

Image Rules（仅主动发帖，不是回复）：
- `image_query` 是英文生图 prompt（2-8 words），用于 AI 生成配图。
- 只有当你觉得配一张图能明显增强这条帖子的表现力时才设置，约 20-30% 的发帖适合配图。
- 大多数帖子不需要配图，设为 ""。
- 好的例子："sunset over mountains silhouette","robot typing on laptop digital art","futuristic city at night neon"
- 差的例子：太泛的如 "funny","cool","image"
"""

RERANK_PROMPT = """你在挑选最适合公开发到 X 的候选帖。

输出严格 JSON：
{"selected_index":0, "reason":"..."}

规则：
- 优先选最像真人账号发言的，不选最工整的。
- 必须优先选择“有具体对象/事件/场景”的那条。
- 如果一条看起来像抽象观点、行业黑话总结、AI 在产出金句，直接淘汰。
- 如果一条像公众号段落、咨询顾问总结、内容农场，也淘汰。
- `reason` 用中文。
- 如果三条都弱，返回：
{"selected_index":-1, "reason":"三条都偏空、偏泛或AI味太重"}
"""

REVIEW_PROMPT = """你在做发帖前的最后审稿，只判断这条适不适合发。

输出严格 JSON：
{"pass":true, "reason":"...", "rewrite_hint":"..."}

规则：
- `pass` 为 true 仅当这条内容足够具体、自然、像真人账号。
- 只要出现以下任一问题，就判 false：
  1. 过于抽象，没有具体对象/事件/场景
  2. 像 AI 在总结行业观点
  3. 太工整、太完整，像文章摘要
  4. 起手就是套话或抽象判断
  5. 明显没有按 topic type 去写
  6. 出现时间词（上周/昨天/最近/前几天/上个月）但 recent_events 里没有对应事件支撑
  7. 结尾是抽象总结句（"这才是X""归根结底""真正的…""说到底"类）
- 如果只是个别句子略显工整，但整体具体、自然、有对象，不要判 false，给 pass true。
- `reason` 和 `rewrite_hint` 用中文。
- `reason` 控制在 80 个中文字符内。
- `rewrite_hint` 控制在 120 个中文字符内。
- `rewrite_hint` 要具体指出应补什么对象、场景或论证，不要空话。
"""

REWRITE_PROMPT = """你在重写一条准备发到 X 的主动帖子。

输出严格 JSON：
{"text":"...", "reason":"..."}

规则：
- 一律用中文。
- 必须保留原 topic 的核心意思。
- 必须按 rewrite_hint 把内容改得更具体、更像真人账号。
- 不要泛泛而谈，不要像 AI 助手。
- 不要 hashtag，不要 emoji，禁止用破折号（——），禁止用引号（包括中文引号 ""、英文引号 ""）。
- casual 保持短；其他类型保持单条中长帖，120-220 字优先。
- `reason` 用中文，简短说明这次重写补了什么具体东西。
"""

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
    import math
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


def chat_json_result(
    messages: list[dict],
    *,
    temperature: float,
    max_tokens: int,
    retries: int = 2,
) -> tuple[dict, dict]:
    last_error: Exception | None = None
    retry_messages = list(messages)
    retry_temperature = temperature
    retry_max_tokens = max_tokens
    for _ in range(retries + 1):
        # chat_text_result raises on thinking-only / empty responses; that has
        # to live inside the try so retries can grow the budget instead of
        # bubbling the first failure up to post_once and crashing the run.
        try:
            result = chat_text_result(retry_messages, temperature=retry_temperature, max_tokens=retry_max_tokens)
            return result, parse_json_object(result["content"])
        except Exception as exc:
            last_error = exc
            retry_messages = list(messages) + [
                {
                    "role": "system",
                    "content": "你的上一条输出不是可解析的完整 JSON。请这次只输出一个完整、闭合、合法的 JSON 对象，不要 markdown，不要解释，不要额外文本。",
                }
            ]
            retry_temperature = min(retry_temperature, 0.4)
            # Reasoning models burn the budget on thinking; on
            # LLM_BUDGET_EXHAUSTED double, otherwise grow modestly. Cap at 8000.
            if "LLM_BUDGET_EXHAUSTED" in str(exc):
                retry_max_tokens = min(8000, max(retry_max_tokens * 2, 2048))
            else:
                retry_max_tokens = min(8000, retry_max_tokens + 500)
    if last_error:
        raise last_error
    raise RuntimeError("chat_json_result failed without an exception")


def topic_payload(topic: dict) -> dict:
    normalized = normalize_post_topic(topic)
    return {
        "id": normalized.get("id", ""),
        "type": normalized.get("type", "argument"),
        "source": normalized.get("source", ""),
        "text": normalized.get("text", ""),
        "subject": normalized.get("subject", ""),
        "event_or_context": normalized.get("event_or_context", ""),
        "stance": normalized.get("stance", ""),
        "evidence_hint": normalized.get("evidence_hint", ""),
        "summary": topic_summary_text(normalized),
    }


def build_candidate_messages(topic: dict, persona_context: dict) -> list[dict]:
    prompt = CANDIDATE_PROMPT
    if not image_search_available():
        prompt = prompt + '\n\n当前图片搜索不可用（未配置 GIPHY/Unsplash API），请不要设置 image_query。'

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
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "生成 3 条不同角度的主动发帖候选",
                    "topic": payload,
                    "persona_context": persona_context,
                    "requirements": [
                        "不要泛泛地谈 founder、AI 产品、独立开发这类大词",
                        "一定要挂在具体对象、最近观察、一次经历、一个热点或一段明确论证上",
                        "如果是 argument，必须把观点展开，不要只给一句口号",
                    ],
                    "recent_learning_references": compact_refs,
                    "human_feedback_references": build_feedback_context() or None,
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]


def build_rerank_messages(topic: dict, candidates: list[dict]) -> list[dict]:
    return [
        {"role": "system", "content": RERANK_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "topic": topic_payload(topic),
                    "candidates": candidates,
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]


def build_review_messages(topic: dict, candidate: dict, persona_context: dict) -> list[dict]:
    return [
        {"role": "system", "content": REVIEW_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "topic": topic_payload(topic),
                    "candidate": candidate,
                    "recent_events": persona_context.get("recent_events", []),
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]


def build_rewrite_messages(topic: dict, candidate: dict, review_reason: str, rewrite_hint: str) -> list[dict]:
    return [
        {"role": "system", "content": REWRITE_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "topic": topic_payload(topic),
                    "current_candidate": candidate,
                    "review_reason": review_reason,
                    "rewrite_hint": rewrite_hint,
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]


def normalize_candidates(candidates: list[dict]) -> list[dict]:
    normalized = []
    for idx, item in enumerate(candidates[:3]):
        normalized.append(
            {
                "index": idx,
                "text": str((item or {}).get("text") or "").strip(),
                "angle": str((item or {}).get("angle") or "").strip().replace("\n", " "),
                "reason": str((item or {}).get("reason") or "").strip().replace("\n", " "),
                "image_query": str((item or {}).get("image_query") or "").strip(),
            }
        )
    if len(normalized) < 3 or not all(item["text"] for item in normalized):
        raise RuntimeError(f"Candidate list invalid: {normalized}")
    return normalized


def rewrite_selected_candidate(topic: dict, candidate: dict, review_reason: str, rewrite_hint: str, persona_context: dict) -> tuple[dict, dict, dict]:
    rewrite_result, rewrite_payload = chat_json_result(
        build_rewrite_messages(topic, candidate, review_reason, rewrite_hint),
        temperature=0.85,
        max_tokens=420,
    )
    rewritten = {
        "index": candidate["index"],
        "text": str(rewrite_payload.get("text") or "").strip(),
        "angle": candidate.get("angle", ""),
        "reason": str(rewrite_payload.get("reason") or "").strip().replace("\n", " "),
        "image_query": str(candidate.get("image_query") or "").strip(),
    }
    if not rewritten["text"]:
        raise RuntimeError(f"Rewrite returned empty text: {rewrite_payload}")
    review_result, review_payload = chat_json_result(
        build_review_messages(topic, rewritten, persona_context),
        temperature=0.2,
        max_tokens=720,
    )
    return rewritten, rewrite_result, {
        "pass": bool(review_payload.get("pass")),
        "reason": str(review_payload.get("reason") or "").strip().replace("\n", " "),
        "rewrite_hint": str(review_payload.get("rewrite_hint") or "").strip().replace("\n", " "),
        "usage": review_result["usage"],
        "cost": review_result["cost"],
    }


def generate_post_plan(topic: dict) -> dict:
    topic = normalize_post_topic(topic)
    persona_context = persona_context_dict()
    if not topic_summary_text(topic):
        raise RuntimeError("Empty topic.")

    candidate_result, candidate_payload = chat_json_result(
        build_candidate_messages(topic, persona_context),
        temperature=0.95,
        max_tokens=1400,
    )
    candidates = normalize_candidates(candidate_payload.get("candidates") or [])

    rerank_result, rerank_payload = chat_json_result(
        build_rerank_messages(topic, candidates),
        temperature=0.2,
        max_tokens=640,
    )
    selected_index = int(rerank_payload.get("selected_index", -1))
    rerank_reason = str(rerank_payload.get("reason") or "").strip().replace("\n", " ")
    selected_candidate = next((item for item in candidates if item["index"] == selected_index), None)

    # Use shaped zeros instead of empty dicts so persisted records have a
    # consistent schema across stages (review-only / rewrite-also paths).
    empty_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    empty_cost = {"total_cost": 0.0}
    review_usage = dict(empty_usage)
    review_cost = dict(empty_cost)
    review_reason = ""
    review_rewrite_hint = ""
    rewrite_usage = dict(empty_usage)
    rewrite_cost = dict(empty_cost)
    rewritten = False
    best_candidate = selected_candidate
    review_pass = bool(selected_candidate)

    if selected_candidate:
        review_result, review_payload = chat_json_result(
            build_review_messages(topic, selected_candidate, persona_context),
            temperature=0.2,
            max_tokens=720,
        )
        review_usage = review_result["usage"]
        review_cost = review_result["cost"]
        review_pass = bool(review_payload.get("pass"))
        review_reason = str(review_payload.get("reason") or "").strip().replace("\n", " ")
        review_rewrite_hint = str(review_payload.get("rewrite_hint") or "").strip().replace("\n", " ")
        if not review_pass:
            rewritten_candidate, rewrite_model_result, rewritten_review = rewrite_selected_candidate(
                topic,
                selected_candidate,
                review_reason,
                review_rewrite_hint,
                persona_context,
            )
            best_candidate = rewritten_candidate
            rewrite_usage = rewrite_model_result["usage"]
            rewrite_cost = rewrite_model_result["cost"]
            rewritten = True
            review_usage = rewritten_review["usage"]
            review_cost = rewritten_review["cost"]
            review_pass = rewritten_review["pass"]
            review_reason = rewritten_review["reason"]
            review_rewrite_hint = rewritten_review["rewrite_hint"]
            if review_pass:
                selected_candidate = rewritten_candidate
            else:
                selected_candidate = None

    total_cost = round(
        float(candidate_result["cost"].get("total_cost") or 0.0)
        + float(rerank_result["cost"].get("total_cost") or 0.0)
        + float(review_cost.get("total_cost") or 0.0)
        + float(rewrite_cost.get("total_cost") or 0.0),
        8,
    )

    return {
        "topic": topic,
        "candidates": candidates,
        "selected_candidate": selected_candidate,
        "best_candidate": best_candidate,
        "selected_index": selected_index if selected_candidate else -1,
        "selected_reason": rerank_reason,
        "review_pass": review_pass,
        "review_reason": review_reason,
        "review_rewrite_hint": review_rewrite_hint,
        "rewritten": rewritten,
        "candidate_usage": candidate_result["usage"],
        "candidate_cost": candidate_result["cost"],
        "rerank_usage": rerank_result["usage"],
        "rerank_cost": rerank_result["cost"],
        "review_usage": review_usage,
        "review_cost": review_cost,
        "rewrite_usage": rewrite_usage,
        "rewrite_cost": rewrite_cost,
        "total_cost_cny": total_cost,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic-text", default="")
    parser.add_argument("--type", default="argument")
    args = parser.parse_args()

    load_env_file()
    ensure_state_dirs()
    if args.topic_text.strip():
        topic = normalize_post_topic(
            {
                "id": "adhoc",
                "text": args.topic_text.strip(),
                "stance": args.topic_text.strip(),
                "type": args.type.strip() or "argument",
                "source": "adhoc",
                "status": "pending",
            }
        )
    else:
        topic = next_pending_post_topic()
        if not topic:
            print(json.dumps({"ok": False, "reason": "no_pending_topic"}, ensure_ascii=False, indent=2))
            return 2

    plan = generate_post_plan(topic)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
