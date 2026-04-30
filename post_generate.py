#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from common import (
    chat_text_result,
    ensure_state_dirs,
    load_env_file,
    next_pending_post_topic,
    normalize_post_topic,
    parse_json_object,
    topic_summary_text,
)
from learning_store import recent_learning_references

CANDIDATE_PROMPT = """你在为一个真实的 X 账号写主动发帖。

输出严格 JSON：
{
  "candidates": [
    {"text":"...", "angle":"...", "reason":"..."},
    {"text":"...", "angle":"...", "reason":"..."},
    {"text":"...", "angle":"...", "reason":"..."}
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
- 不要用 hashtag，不要 emoji。
- 禁止默认用这些句式起手：很多……、本质上……、归根结底……、真正的……、问题不在……而在……。
- 三条候选的角度必须明显不同，不能只是同一句话改写。
- `reason` 和 `angle` 都用中文。

按类型写：
- news_react：针对一个热点/新能力/新闻，先点明发生了什么，再说为什么重要。主力长度 120-220 字。
- story：叙述一件具体的事或一次具体经历，再落到判断。主力长度 120-220 字。
- argument：明确表达一个观点，并给出 2-3 层具体论证。主力长度 120-220 字。
- casual：像顺手发一条，但也必须挂在具体对象或语境上。长度 35-90 字。

长度规则：
- casual 之外，优先写成单条中长帖，允许分成 2-4 个短段。
- 不要写 thread，不要编号列表，不要拉得像文章。
- 如果提供了近期高质量帖子学习样本，只学习它们的动作、节奏、结构、hook，不要复述，不要洗稿。
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
- 不要 hashtag，不要 emoji。
- casual 保持短；其他类型保持单条中长帖，120-220 字优先。
- `reason` 用中文，简短说明这次重写补了什么具体东西。
"""


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


def build_candidate_messages(topic: dict) -> list[dict]:
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
        {"role": "system", "content": CANDIDATE_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "生成 3 条不同角度的主动发帖候选",
                    "topic": payload,
                    "requirements": [
                        "不要泛泛地谈 founder、AI 产品、独立开发这类大词",
                        "一定要挂在具体对象、最近观察、一次经历、一个热点或一段明确论证上",
                        "如果是 argument，必须把观点展开，不要只给一句口号",
                    ],
                    "recent_learning_references": compact_refs,
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


def build_review_messages(topic: dict, candidate: dict) -> list[dict]:
    return [
        {"role": "system", "content": REVIEW_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "topic": topic_payload(topic),
                    "candidate": candidate,
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
            }
        )
    if len(normalized) < 3 or not all(item["text"] for item in normalized):
        raise RuntimeError(f"Candidate list invalid: {normalized}")
    return normalized


def rewrite_selected_candidate(topic: dict, candidate: dict, review_reason: str, rewrite_hint: str) -> tuple[dict, dict, dict]:
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
    }
    if not rewritten["text"]:
        raise RuntimeError(f"Rewrite returned empty text: {rewrite_payload}")
    review_result, review_payload = chat_json_result(
        build_review_messages(topic, rewritten),
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
    if not topic_summary_text(topic):
        raise RuntimeError("Empty topic.")

    candidate_result, candidate_payload = chat_json_result(
        build_candidate_messages(topic),
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
            build_review_messages(topic, selected_candidate),
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
