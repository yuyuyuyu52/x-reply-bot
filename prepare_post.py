#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import re
import textwrap
from datetime import datetime

from common import (
    REPLIED_PATH,
    SCREENSHOT_DIR,
    SELECTED_PATH,
    chat_json_result,
    ensure_state_dirs,
    load_env_file,
    load_json,
    model_name,
    run_harness,
    write_json,
)

SELECTION_PROMPT = """You select one X post from a shortlist for an autonomous reply bot.

Return strict JSON:
{"selected_url":"...", "reason":"..."}

Rules:
- Select exactly one post only if it is a normal organic post.
- Reject ads, promotions, giveaways, affiliate sales, investment solicitation, spam, engagement bait, adult content, and obvious self-promo.
- Only accept posts whose main language is Chinese, English, or a clear mix of Chinese and English.
- Prefer AI, coding, models, API, tooling, and developer topics.
- Prefer posts that can receive a short public reply without sounding forced.
- Give extra preference to posts with high engagement (likes, replies, reposts) — these are actively resonating and replies will get more eyeballs.
- If a post quotes another post, treat the main post as the reply target. The quoted post is context only.
- `reason` must be written in Chinese.
- If the shortlist has no good option, return:
{"selected_url":"", "reason":"none"}
"""

TECH_KEYWORDS = [
    "ai",
    "api",
    "openai",
    "anthropic",
    "claude",
    "gemini",
    "qwen",
    "deepseek",
    "cursor",
    "llm",
    "model",
    "prompt",
    "coding",
    "code",
    "developer",
    "sdk",
    "browser",
    "chrome",
    "模型",
    "代码",
    "编程",
    "开发",
    "接口",
    "工具",
    "自动化",
    "推理",
]

BLOCK_PATTERNS = [
    "promoted",
    "广告",
    "赞助",
    "抽奖",
    "giveaway",
    "airdrop",
    "邀请码",
    "返利",
    "affiliate",
    "discount code",
    "use my code",
    "dm me",
    "赚钱",
    "稳赚",
    "投资建议",
    "signal",
    "onlyfans",
    "成人视频",
]


def normalize_status_url(url: str) -> str:
    match = re.search(r"https://x\.com/[^/]+/status/\d+", url or "")
    return match.group(0) if match else ""


def score_candidate(candidate: dict) -> float:
    text = (candidate.get("text") or "")
    lowered = text.lower()
    score = min(len(text.strip()), 900) / 900
    for kw in TECH_KEYWORDS:
        if kw in lowered:
            score += 2
    likes = int(candidate.get("likes") or 0)
    replies = int(candidate.get("replies") or 0)
    reposts = int(candidate.get("reposts") or 0)
    views = int(candidate.get("views") or 0)
    engagement_bonus = math.log10(1 + likes * 2 + replies * 3 + reposts * 2 + views / 1000)
    return round(score + engagement_bonus, 3)


def looks_supported_language(text: str) -> bool:
    if not text:
        return False
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    other_letters = len(re.findall(r"[\u0400-\u04ff\u0600-\u06ff\u0900-\u0d7f\u3040-\u30ff\uac00-\ud7af]", text))
    meaningful = han + latin + other_letters
    if meaningful == 0:
        return False
    return (han + latin) / meaningful >= 0.8


def shortlist_candidates(posts: list[dict], replied: set[str]) -> list[dict]:
    deduped: dict[str, dict] = {}
    for post in posts:
        text = (post.get("main_text") or post.get("text") or "").strip()
        if len(text) < 25:
            continue
        lowered = text.lower()
        if any(pattern in lowered for pattern in BLOCK_PATTERNS):
            continue
        if not looks_supported_language(text):
            continue

        urls = []
        for raw_url in post.get("links") or []:
            status_url = normalize_status_url(raw_url)
            if status_url:
                urls.append(status_url)
        if not urls:
            continue

        url = urls[0]
        if url in replied:
            continue

        candidate = {
            "url": url,
            "text": text[:1800],
            "quoted_post_text": str(post.get("quoted_post_text") or "").strip()[:1200],
            "is_quote_tweet": bool(post.get("is_quote_tweet")),
            "likes": int(post.get("likes") or 0),
            "replies": int(post.get("replies") or 0),
            "reposts": int(post.get("reposts") or 0),
            "views": int(post.get("views") or 0),
            "score": 0.0,
        }
        candidate["score"] = score_candidate(candidate)
        existing = deduped.get(url)
        if not existing or candidate["score"] > existing["score"]:
            deduped[url] = candidate

    return sorted(deduped.values(), key=lambda item: item["score"], reverse=True)[:12]


def choose_candidate_with_ai(candidates: list[dict]) -> dict:
    result = chat_json_result(
        [
            {"role": "system", "content": SELECTION_PROMPT},
            {
                "role": "user",
                "content": json.dumps(candidates, ensure_ascii=False, indent=2),
            },
        ],
        temperature=0.1,
        max_tokens=520,
    )
    selected = result["payload"]
    return {
        "selected_url": normalize_status_url(str(selected.get("selected_url") or "").strip()),
        "reason": str(selected.get("reason") or "").strip(),
        "usage": result["usage"],
        "cost": result["cost"],
    }


def collect_feed_posts() -> dict:
    code = f'''
import json, re

selected_path = {json.dumps(str(SELECTED_PATH))}

def norm_status(url):
    match = re.search(r'https://x.com/[^/]+/status/[0-9]+', url or '')
    return match.group(0) if match else None

tabs = list_tabs(include_chrome=False)
x_tab = None
for tab in tabs:
    tab_url = tab.get('url', '')
    if 'x.com/home' in tab_url:
        x_tab = tab
        break
if x_tab:
    switch_tab(x_tab['targetId'])
else:
    tid = new_tab('https://x.com/home')
    switch_tab(tid)
current = page_info()
if current.get('dialog'):
    tid = new_tab('https://x.com/home')
    switch_tab(tid)
    current = page_info()
if 'x.com/home' not in (current.get('url') or ''):
    goto_url('https://x.com/home')

wait_for_load(20)
wait(5)
body = js('document.body.innerText') or ''
info = page_info()
body_lower = body.lower()
login_marker = ('sign in' in body_lower) or ('log in' in body_lower)

if login_marker and '主页' not in body and 'Home' not in body:
    out = {{
        'ok': False,
        'reason': 'login_required',
        'page_info': info,
        'body_start': body[:800]
    }}
    open(selected_path, 'w').write(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False))
else:
    js('window.scrollTo(0, 0)')
    wait(1)
    posts = js("""
(() => {{
  function parseCount(s) {{
    if (!s) return 0;
    s = (s + '').replace(/,/g, '').trim();
    if (/^\d+(\.\d+)?[Kk]$/.test(s)) return Math.round(parseFloat(s) * 1000);
    if (/^\d+(\.\d+)?[Mm]$/.test(s)) return Math.round(parseFloat(s) * 1000000);
    return parseInt(s, 10) || 0;
  }}
  function countBtn(el, tids) {{
    for (const tid of tids) {{
      const btn = el.querySelector('[data-testid="' + tid + '"]');
      if (!btn) continue;
      for (const sp of btn.querySelectorAll('span')) {{
        const t = (sp.innerText || '').trim();
        if (/^\d[\d.,]*[KkMm]?$/.test(t) && t) return parseCount(t);
      }}
    }}
    return 0;
  }}
  return Array.from(document.querySelectorAll('article')).map((el, i) => {{
    const text = el.innerText || '';
    const textBlocks = Array.from(el.querySelectorAll('[data-testid="tweetText"]'))
      .map(node => (node.innerText || '').trim())
      .filter(Boolean);
    const links = Array.from(el.querySelectorAll('a[href*="/status/"]'))
      .map(a => a.href)
      .filter(Boolean);
    return {{
      i,
      text,
      main_text: textBlocks[0] || text,
      quoted_post_text: textBlocks[1] || '',
      is_quote_tweet: textBlocks.length > 1,
      links,
      replies: countBtn(el, ['reply']),
      reposts: countBtn(el, ['retweet', 'unretweet']),
      likes: countBtn(el, ['like', 'unlike']),
      views: countBtn(el, ['analyticsButton'])
    }};
  }});
}})()
""") or []
    out = {{
        'ok': True,
        'reason': 'feed_collected',
        'page_info': info,
        'posts': posts[:40]
    }}
    open(selected_path, 'w').write(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False, indent=2))
'''
    run_harness(textwrap.dedent(code))
    return load_json(SELECTED_PATH, {"ok": False, "reason": "feed_collect_failed"})


def open_selected_post(url: str) -> dict:
    shot_path = SCREENSHOT_DIR / "x_reply_prepare.png"
    detail_js = r"""
(() => {
  const article = document.querySelector('article');
  if (!article) return null;
  const textBlocks = Array.from(article.querySelectorAll('[data-testid="tweetText"]'))
    .map((node, i) => ({
      i,
      text: (node.innerText || '').trim()
    }))
    .filter(item => item.text);
  const links = Array.from(article.querySelectorAll('a')).map(a => a.href).filter(Boolean);
  const fullText = article.innerText || '';
  return {
    full_text: fullText,
    main_post_text: textBlocks[0] ? textBlocks[0].text : fullText,
    quoted_post_text: textBlocks[1] ? textBlocks[1].text : '',
    is_quote_tweet: textBlocks.length > 1,
    text_blocks: textBlocks.slice(0, 4),
    links: links,
    articles: [{
      i: 0,
      text: fullText,
      links: links
    }]
  };
})()
"""
    code = f'''
import json

selected_path = {json.dumps(str(SELECTED_PATH))}
shot_path = {json.dumps(str(shot_path))}
url = {json.dumps(url)}
detail_js = {json.dumps(detail_js)}

tabs = list_tabs(include_chrome=False)
x_tab = None
for tab in tabs:
    tab_url = tab.get('url', '')
    if tab_url.startswith(url):
        x_tab = tab
        break
if x_tab:
    switch_tab(x_tab['targetId'])
else:
    tid = new_tab(url)
    switch_tab(tid)

current = page_info()
if current.get('dialog'):
    tid = new_tab(url)
    switch_tab(tid)
    current = page_info()
if not (current.get('url') or '').startswith(url):
    goto_url(url)
    wait_for_load(20)
    wait(5)

detail_info = page_info()
detail = js(detail_js) or []
shot = capture_screenshot(shot_path)
out = {{
    'ok': True,
    'url': url,
    'page_info': detail_info,
    'main_post_text': (detail or {{}}).get('main_post_text', ''),
    'quoted_post_text': (detail or {{}}).get('quoted_post_text', ''),
    'is_quote_tweet': bool((detail or {{}}).get('is_quote_tweet')),
    'text_blocks': (detail or {{}}).get('text_blocks', []),
    'articles': (detail or {{}}).get('articles', [])[:3],
    'screenshot': shot
}}
open(selected_path, 'w').write(json.dumps(out, ensure_ascii=False, indent=2))
print(json.dumps(out, ensure_ascii=False, indent=2))
'''
    run_harness(textwrap.dedent(code))
    return load_json(SELECTED_PATH, {"ok": False, "reason": "selected_post_not_written"})


def main() -> int:
    load_env_file()
    ensure_state_dirs()

    replied = set(load_json(REPLIED_PATH, {"posts": []}).get("posts", []))
    feed = collect_feed_posts()
    if not feed.get("ok"):
        print(json.dumps(feed, ensure_ascii=False, indent=2))
        return 1

    candidates = shortlist_candidates(feed.get("posts") or [], replied)
    if not candidates:
        payload = {
            "ok": False,
            "reason": "no_suitable_feed_candidates",
            "page_info": feed.get("page_info"),
        }
        write_json(SELECTED_PATH, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    try:
        selection = choose_candidate_with_ai(candidates)
    except Exception as exc:
        payload = {
            "ok": False,
            "reason": "ai_selection_exception",
            "error": str(exc),
            "candidates": candidates,
        }
        write_json(SELECTED_PATH, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    chosen_url = selection["selected_url"]
    reason = selection["reason"]
    if not chosen_url:
        payload = {
            "ok": False,
            "reason": "ai_rejected_all_candidates",
            "selection_model": model_name(),
            "selector_reason": reason,
            "selection_usage": selection["usage"],
            "selection_cost": selection["cost"],
            "candidates": candidates,
        }
        write_json(SELECTED_PATH, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    selected = open_selected_post(chosen_url)
    if not selected.get("ok"):
        print(json.dumps(selected, ensure_ascii=False, indent=2))
        return 1

    selected["selection_model"] = model_name()
    selected["selection_id"] = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    selected["selector_reason"] = reason
    selected["selection_usage"] = selection["usage"]
    selected["selection_cost"] = selection["cost"]
    selected["selection_candidates"] = candidates
    write_json(SELECTED_PATH, selected)
    print(json.dumps(selected, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
