#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import textwrap
from collections import Counter
from datetime import datetime
from pathlib import Path

from src.harness import harness_navigate_snippet
from src.common import (
    exclusive_lock,
    BLOCK_PATTERNS,
    normalize_status_url,
    looks_supported_language,
    LATEST_POST_RUN_PATH,
    chat_json_result,
    ensure_state_dirs,
    estimate_cost,
    load_env_file,
    load_json,
    model_name,
    run_harness,
)
from src.learning_store import (
    ensure_learning_storage,
    record_learning_run,
    upsert_learning_post,
)

ROOT = Path(__file__).resolve().parent
LOCK_PATH = ROOT / "state" / "observe_feed.lock"
FOLLOW_TODAY_PATH = ROOT / "state" / "follow_today.json"

ANALYSIS_PROMPT = """你在帮一个 X 账号做观察和学习。

你的任务不是回复，也不是发帖，而是找出真正值得学习的高质量帖子。

输出严格 JSON：
{
  "selected": [
    {
      "url": "...",
      "quality_label": "high_quality",
      "quality_score": 88,
      "format_guess": "news_react",
      "hook_type": "...",
      "quality_reason": "...",
      "style_summary": "...",
      "structure_pattern": "...",
      "why_it_works": "...",
      "imitation_takeaway": "...",
      "innovation_direction": "..."
    }
  ],
  "summary": "..."
}

规则：
- 一律用中文写所有说明字段。
- 重点看四类信号：浏览、点赞、评论、转发/引用。转发和引用抓不到时，用转帖信号代替。
- 但不要被数字绑死。有些数字没那么高，但内容密度、表达方式、结构、hook 明显很强，也值得保留。
- 只保留“值得学习”的帖子，最多 6 条。其他直接忽略。
- 为了保证 JSON 稳定，请所有文本字段尽量短：
  - `hook_type` <= 16 个中文字符
  - `quality_reason` <= 60 个中文字符
  - `style_summary` <= 40 个中文字符
  - `structure_pattern` <= 50 个中文字符
  - `why_it_works` <= 60 个中文字符
  - `imitation_takeaway` <= 60 个中文字符
  - `innovation_direction` <= 60 个中文字符
  - `summary` <= 180 个中文字符
- `quality_label` 只允许 `high_quality` 或 `worth_watching`。
- `format_guess` 只允许 `news_react`、`story`、`argument`、`casual`。
- `quality_score` 是 0-100 的整数。
- `hook_type` 要说明它开头在干嘛，比如“反直觉判断”“热点承接”“个人迁移体感”“一句短观察”。
- `style_summary` 讲写作风格，不讲主题。
- `structure_pattern` 讲结构动作，比如“先抛结论，再补两层机制，再收尾”。
- `why_it_works` 讲它为什么容易拿到浏览/点赞/评论/转发。
- `imitation_takeaway` 讲这个账号以后可以模仿什么，但不要洗稿。
- `innovation_direction` 讲如果以后借鉴这个套路，应该怎么改成更像自己的内容。
- `summary` 用中文概括这批高质量帖总体上都在做什么。
"""



def collect_feed_posts() -> list[dict]:
    code = r"""
import json
import re

def norm_status(url):
    match = re.search(r'https://x.com/[^/]+/status/[0-9]+', url or '')
    return match.group(0) if match else ''

tabs = list_tabs(include_chrome=False)
x_tab = None
for tab in tabs:
    if 'x.com' in tab.get('url', ''):
        x_tab = tab
        break
if x_tab:
    switch_tab(x_tab['targetId'])
else:
    tid = new_tab('https://x.com/home')
    switch_tab(tid)
current = page_info()
if current.get('dialog'):
    js('window.onbeforeunload = null')
    goto_url('https://x.com/home')
    wait_for_load(20)
    wait(2)
    current = page_info()

js('window.onbeforeunload = null')
goto_url('https://x.com/home')
wait_for_load(20)
wait(4)
js('window.scrollTo(0, 0)')
wait(1)

seen = {}
for step in range(6):
    batch = js('''
(() => Array.from(document.querySelectorAll('article[data-testid="tweet"]')).map((el, i) => {
    const links = Array.from(el.querySelectorAll('a'))
      .map(a => a.href)
      .filter(Boolean);
    let statusUrl = '';
    for (const href of links) {
      const match = (href || '').match(/https:\\/\\/x\\.com\\/[^/]+\\/status\\/\\d+/);
      if (match) {
        statusUrl = match[0];
        break;
      }
    }
    const nameBlock = el.querySelector('[data-testid="User-Name"]');
    const textBlock = el.querySelector('[data-testid="tweetText"]');
    return {
      i,
      status_url: statusUrl,
      links: links.slice(0, 20),
      user_name_block: (nameBlock && nameBlock.innerText) || '',
      tweet_text: (textBlock && textBlock.innerText) || '',
      full_text: (el.innerText || '').slice(0, 2500),
      aria: Array.from(el.querySelectorAll('[aria-label]'))
        .map(n => n.getAttribute('aria-label'))
        .filter(Boolean)
        .slice(0, 40),
      media_count: el.querySelectorAll('[data-testid="tweetPhoto"], video').length,
    };
}))()
''') or []
    for item in batch:
        url = item.get('status_url') or ''
        if not url:
            continue
        seen[url] = item
    js('window.scrollBy(0, window.innerHeight * 0.9)')
    wait(2)

print(json.dumps(list(seen.values()), ensure_ascii=False, indent=2))
"""
    output = run_harness(textwrap.dedent(code), timeout=150)
    data = json.loads(output)
    return data if isinstance(data, list) else []


def clean_candidates(posts: list[dict]) -> list[dict]:
    own_handle = infer_own_handle()
    cleaned: list[dict] = []
    for item in posts:
        status_url = normalize_status_url(str(item.get("status_url") or ""))
        if not status_url:
            continue
        handle_match = re.search(r"https://x\.com/([^/]+)/status/\d+", status_url)
        author_handle = handle_match.group(1) if handle_match else ""
        if own_handle and author_handle.lower() == own_handle:
            continue

        post_text = str(item.get("tweet_text") or "").strip()
        if len(post_text) < 18:
            continue
        lowered = post_text.lower()
        if any(pattern in lowered for pattern in BLOCK_PATTERNS):
            continue
        if not looks_supported_language(post_text):
            continue

        full_text = str(item.get("full_text") or "").strip()
        lines = [line.strip() for line in full_text.splitlines() if line.strip()]
        author_name = lines[0] if lines else ""
        relative_time = ""
        for line in lines[:6]:
            if line.startswith("@"):
                continue
            if any(token in line for token in ["秒", "分钟", "小时", "月", "日", "前"]) or re.search(r"\d+[smhd]", line.lower()):
                relative_time = line
                break

        metrics = parse_metrics(item.get("aria") or [])
        cleaned.append(
            {
                "status_url": status_url,
                "author_handle": author_handle,
                "author_name": author_name,
                "relative_time": relative_time,
                "post_text": post_text,
                "full_text": full_text,
                "language": "zh" if re.search(r"[\u4e00-\u9fff]", post_text) else "en",
                "views": metrics["views"],
                "replies": metrics["replies"],
                "reposts": metrics["reposts"],
                "likes": metrics["likes"],
                "bookmarks": metrics["bookmarks"],
                "media_count": int(item.get("media_count") or 0),
                "engagement_score": engagement_score(metrics),
                "raw": item,
            }
        )
    deduped = {item["status_url"]: item for item in cleaned}
    return sorted(deduped.values(), key=lambda item: (item["engagement_score"], item["views"], item["likes"]), reverse=True)


def shortlist_candidates(posts: list[dict]) -> list[dict]:
    if len(posts) <= 12:
        return posts
    top_by_engagement = posts[:10]
    top_by_discussion = sorted(posts, key=lambda item: (item["replies"], item["reposts"], item["likes"]), reverse=True)[:6]
    merged: dict[str, dict] = {}
    for item in top_by_engagement + top_by_discussion:
        merged[item["status_url"]] = item
    return list(merged.values())[:12]


def analyze_candidates_once(candidates: list[dict], *, max_tokens: int) -> dict:
    payload = []
    for item in candidates:
        payload.append(
            {
                "url": item["status_url"],
                "author_handle": item["author_handle"],
                "author_name": item["author_name"],
                "relative_time": item["relative_time"],
                "post_text": item["post_text"][:700],
                "views": item["views"],
                "replies": item["replies"],
                "reposts": item["reposts"],
                "likes": item["likes"],
                "bookmarks": item["bookmarks"],
                "engagement_score": item["engagement_score"],
            }
        )
    result = chat_json_result(
        [
            {"role": "system", "content": ANALYSIS_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "从这批 feed 候选里挑出值得学习的高质量帖子",
                        "candidates": payload,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ],
        temperature=0.25,
        max_tokens=max_tokens,
    )
    return {
        "payload": result["payload"],
        "usage": result["usage"],
        "cost": result["cost"],
    }


def merged_usage(parts: list[dict]) -> dict:
    return {
        "prompt_tokens": sum(int((part.get("usage") or {}).get("prompt_tokens") or 0) for part in parts),
        "completion_tokens": sum(int((part.get("usage") or {}).get("completion_tokens") or 0) for part in parts),
        "total_tokens": sum(int((part.get("usage") or {}).get("total_tokens") or 0) for part in parts),
    }


def fallback_summary(selected: list[dict], failed_chunks: int, total_chunks: int) -> str:
    if not selected:
        if failed_chunks:
            return f"本轮分批分析时有 {failed_chunks}/{total_chunks} 个分块失败，最终没保留下值得学习的样本。"
        return "本轮没有筛出值得学习的高质量帖子。"

    format_counts = Counter(str(item.get("format_guess") or "").strip() for item in selected if str(item.get("format_guess") or "").strip())
    hook_counts = Counter(str(item.get("hook_type") or "").strip() for item in selected if str(item.get("hook_type") or "").strip())
    quality_counts = Counter(str(item.get("quality_label") or "").strip() for item in selected if str(item.get("quality_label") or "").strip())

    formats = "、".join(name for name, _ in format_counts.most_common(3)) or "混合类型"
    hooks = "、".join(name for name, _ in hook_counts.most_common(3)) or "具体观察"
    high_quality = int(quality_counts.get("high_quality") or 0)
    worth_watching = int(quality_counts.get("worth_watching") or 0)
    degraded = f"，分批兜底时有 {failed_chunks}/{total_chunks} 个分块失败" if failed_chunks else ""
    return (
        f"本轮保留了 {len(selected)} 条值得学习的帖子，其中 high_quality {high_quality} 条、worth_watching {worth_watching} 条。"
        f"主要类型是 {formats}，常见 hook 是 {hooks}{degraded}。"
    )[:180]


def dedupe_selected(selected: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for item in selected:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        prior = merged.get(url)
        if not prior or int(item.get("quality_score") or 0) > int(prior.get("quality_score") or 0):
            merged[url] = item
    rank = {"high_quality": 1, "worth_watching": 0}
    return sorted(
        merged.values(),
        key=lambda item: (
            rank.get(str(item.get("quality_label") or "").strip(), -1),
            int(item.get("quality_score") or 0),
        ),
        reverse=True,
    )[:6]


def analyze_candidates(candidates: list[dict]) -> dict:
    primary_error: Exception | None = None
    try:
        return analyze_candidates_once(candidates, max_tokens=3000)
    except Exception as exc:
        primary_error = exc
        if len(candidates) <= 4:
            raise

    chunk_results: list[dict] = []
    failed_chunks = 0
    chunks = [candidates[idx : idx + 4] for idx in range(0, len(candidates), 4)]
    for chunk in chunks:
        try:
            chunk_results.append(analyze_candidates_once(chunk, max_tokens=3000))
        except Exception:
            failed_chunks += 1

    if not chunk_results:
        if primary_error:
            raise primary_error
        raise RuntimeError("analyze_candidates failed without a captured exception")

    selected: list[dict] = []
    for part in chunk_results:
        selected.extend((part.get("payload") or {}).get("selected") or [])
    selected = dedupe_selected(selected)
    usage = merged_usage(chunk_results)
    return {
        "payload": {
            "selected": selected,
            "summary": fallback_summary(selected, failed_chunks, len(chunks)),
        },
        "usage": usage,
        "cost": estimate_cost(usage, model_name()),
    }

def get_today_follow_count() -> int:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    try:
        data = json.loads(FOLLOW_TODAY_PATH.read_text())
        if data.get("date") == today:
            return int(data.get("count", 0))
    except Exception:
        pass
    return 0


def _increment_follow_count() -> None:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    try:
        try:
            data = json.loads(FOLLOW_TODAY_PATH.read_text())
        except Exception:
            data = {}
        if data.get("date") != today:
            data = {"date": today, "count": 0}
        data["count"] = int(data.get("count", 0)) + 1
        FOLLOW_TODAY_PATH.write_text(json.dumps(data))
    except Exception:
        pass

def auto_follow_user(handle: str) -> dict:
    if not handle:
        return {"ok": False, "reason": "empty_handle"}
        
    try:
        if get_today_follow_count() >= 5:
            return {"ok": False, "reason": "daily_limit_reached", "action": "skipped"}
    except Exception as e:
        pass
        
    if handle.startswith("@"):
        handle = handle[1:]
        
    code = f'''
import json
handle = {json.dumps(handle)}
url = f'https://x.com/{{handle}}'

{harness_navigate_snippet('url')}
wait_for_load(20)
wait(3)

followed = js("""
(() => {{
  const btns = Array.from(document.querySelectorAll('[role="button"]'));
  const followBtn = btns.find(btn => btn.innerText.includes('Follow') || btn.innerText.includes('关注'));
  const followingBtn = btns.find(btn => btn.innerText.includes('Following') || btn.innerText.includes('正在关注'));

  if (followingBtn) {{
    return {{ok: true, action: 'already_following'}};
  }}
  if (followBtn) {{
    followBtn.click();
    return {{ok: true, action: 'followed'}};
  }}
  return {{ok: false, reason: 'button_not_found'}};
}})()
""") or {{}}

wait(2)
print(json.dumps({{
    'ok': followed.get('ok', False),
    'handle': handle,
    'action': followed.get('action', ''),
    'reason': followed.get('reason', '')
}}, ensure_ascii=False, indent=2))
'''
    try:
        stdout = run_harness(textwrap.dedent(code))
        result = json.loads(stdout)
        if result.get("action") == "followed":
            _increment_follow_count()
        return result
    except Exception as exc:
        return {"ok": False, "handle": handle, "reason": "exception", "error": str(exc)}



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger", default="schedule")
    args = parser.parse_args()

    load_env_file()
    ensure_state_dirs()
    ensure_learning_storage()

    lock_fh = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("observe_feed already running")
        return 3

    started = datetime.now().astimezone()
    stamp = started.strftime("%Y%m%d_%H%M%S")

    try:
        observed = collect_feed_posts()
        cleaned = clean_candidates(observed)
        for item in cleaned:
            item["observed_at"] = started.strftime("%Y-%m-%d %H:%M:%S %Z")
            item["trigger"] = args.trigger
            item["quality_label"] = "seen"
            upsert_learning_post(item)

        shortlist = shortlist_candidates(cleaned)
        analysis_result = analyze_candidates(shortlist) if shortlist else {"payload": {"selected": [], "summary": "本轮没有合适候选。"}, "usage": {}, "cost": {}}
        selected = analysis_result["payload"].get("selected") or []
        selected_map = {str(item.get("url") or "").strip(): item for item in selected if str(item.get("url") or "").strip()}

        saved_records: list[dict] = []
        high_quality_count = 0
        worth_watching_count = 0

        for item in shortlist:
            analysis = selected_map.get(item["status_url"])
            if not analysis:
                continue
            quality_label = str(analysis.get("quality_label") or "worth_watching").strip()
            quality_score = int(analysis.get("quality_score") or 0)
            if quality_label == "high_quality":
                high_quality_count += 1
            else:
                worth_watching_count += 1
                
            follow_result = None
            if quality_label == "high_quality" and item.get("author_handle"):
                follow_result = auto_follow_user(item.get("author_handle"))
                    
            record = {
                **item,
                "observed_at": started.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "trigger": args.trigger,
                "quality_label": quality_label,
                "quality_score": quality_score,
                "format_guess": str(analysis.get("format_guess") or "").strip(),
                "hook_type": str(analysis.get("hook_type") or "").strip(),
                "quality_reason": str(analysis.get("quality_reason") or "").strip(),
                "style_summary": str(analysis.get("style_summary") or "").strip(),
                "structure_pattern": str(analysis.get("structure_pattern") or "").strip(),
                "why_it_works": str(analysis.get("why_it_works") or "").strip(),
                "imitation_takeaway": str(analysis.get("imitation_takeaway") or "").strip(),
                "innovation_direction": str(analysis.get("innovation_direction") or "").strip(),
                "raw": {
                    "feed": item["raw"],
                    "analysis": analysis,
                },
            }
            if follow_result:
                record["follow_result"] = follow_result
            upsert_learning_post(record)
            saved_records.append(record)

        run_record = {
            "stamp": stamp,
            "time_beijing": started.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "date_beijing": started.strftime("%Y-%m-%d"),
            "trigger": args.trigger,
            "status": "ok",
            "scanned_count": len(cleaned),
            "analyzed_count": len(shortlist),
            "saved_count": len(saved_records),
            "high_quality_count": high_quality_count,
            "worth_watching_count": worth_watching_count,
            "total_cost_cny": float(analysis_result["cost"].get("total_cost") or 0.0),
            "analysis_usage": analysis_result["usage"],
            "analysis_cost": analysis_result["cost"],
            "summary": str(analysis_result["payload"].get("summary") or "").strip(),
            "saved_posts": [
                {
                    "status_url": item["status_url"],
                    "author_handle": item["author_handle"],
                    "post_text": item["post_text"],
                    "views": item["views"],
                    "replies": item["replies"],
                    "reposts": item["reposts"],
                    "likes": item["likes"],
                    "quality_label": item["quality_label"],
                    "quality_score": item["quality_score"],
                    "why_it_works": item["why_it_works"],
                    "imitation_takeaway": item["imitation_takeaway"],
                }
                for item in saved_records
            ],
        }
        record_learning_run(run_record)
        print(json.dumps(run_record, ensure_ascii=False, indent=2))
        return 0
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()


if __name__ == "__main__":
    raise SystemExit(main())
