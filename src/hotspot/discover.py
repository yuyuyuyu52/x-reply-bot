#!/usr/bin/env python3
"""Hotspot discovery: fetch from 10+ external sources, LLM filter, score."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Callable

from src.common import chat_json_result
from src.hotspot.store import is_seen, insert_hotspot
from src.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

HN_TOP_STORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{}.json"
HN_MAX_FETCH = 20
HOTSPOT_MAX_CANDIDATES = 3
HOTSPOT_LLM_CANDIDATES = 30
HOTSPOT_SOURCE_WORKERS = 6
HOTSPOT_HN_WORKERS = 8
HOTSPOT_HTTP_TIMEOUT = 8
PRODUCT_HUNT_GRAPHQL_URL = "https://api.producthunt.com/v2/api/graphql"
PRODUCT_HUNT_TOKEN_URL = "https://api.producthunt.com/v2/oauth/token"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")

REDDIT_SUBREDDITS = [
    "ChatGPTCoding",
    "ClaudeAI",
    "LocalLLaMA",
    "vibecoding",
]

LOBSTERS_HOTTEST_URL = "https://lobste.rs/hottest.json"
SIMONW_ATOM_URL = "https://simonwillison.net/atom/entries/"
HF_PAPERS_URL = "https://huggingface.co/api/daily_papers"
TLDR_AI_URL = "https://tldr.tech/ai"
GH_TRENDING_URL = "https://github.com/trending?since=daily"

# Company blogs / RSS
# Company X accounts for direct tweet scraping via browser-harness
COMPANY_X_ACCOUNTS = {
    "openai": {
        "handle": "OpenAI",
        "profile_url": "https://x.com/OpenAI",
        "label": "OpenAI 动态",
    },
    "anthropic": {
        "handle": "AnthropicAI",
        "profile_url": "https://x.com/AnthropicAI",
        "label": "Anthropic 动态",
    },
    "google": {
        "handle": "GoogleDeepMind",
        "profile_url": "https://x.com/GoogleDeepMind",
        "label": "Google AI 动态",
    },
}

COMPANY_BLOG_URLS: dict[str, str] = {
    "openai": "https://openai.com/index/",
    "anthropic": "https://www.anthropic.com/research",
    "google": "https://blog.google/technology/ai/rss/",
}
HN_ALGOLIA_URL = "https://hn.algolia.com/api/v1/search_by_date"

UA_GENERIC = "Mozilla/5.0 (compatible; x-reply-bot/1.0)"
UA_REDDIT = "python:x-reply-bot:v1.0 (by /u/indiedev)"


def _http_get(url: str, timeout: int = HOTSPOT_HTTP_TIMEOUT, retries: int = 1, ua: str | None = None) -> bytes:
    headers = {"User-Agent": ua or UA_GENERIC}
    if retries < 1:
        retries = 1
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                raise
    return None  # unreachable, kept for clarity


def _http_get_json(url: str, timeout: int = 15, ua: str | None = None) -> dict | list:
    return json.loads(_http_get(url, timeout, ua=ua).decode())


def _env_int(name: str, default: int, min_value: int = 1) -> int:
    try:
        return max(min_value, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Fetch functions
# ---------------------------------------------------------------------------


def fetch_hn_top_stories(limit: int = HN_MAX_FETCH) -> list[dict]:
    logger.info("hn: requesting top stories")
    ids = _http_get_json(HN_TOP_STORIES_URL)
    if not isinstance(ids, list):
        logger.warning("hn: topstories returned non-list: %r", ids)
        return []
    selected_ids = ids[:limit]

    def _fetch_item(sid) -> dict | None:
        try:
            item = _http_get_json(HN_ITEM_URL.format(sid))
        except Exception:
            return None
        if not item or not item.get("title"):
            return None
        return {
            "id": str(item.get("id", "")),
            "title": item.get("title", ""),
            "url": item.get("url") or f"https://news.ycombinator.com/item?id={item['id']}",
            "score": item.get("score", 0),
            "descendants": item.get("descendants", 0),
        }

    stories: list[dict] = []
    if selected_ids:
        with ThreadPoolExecutor(max_workers=min(HOTSPOT_HN_WORKERS, len(selected_ids))) as executor:
            futures = [executor.submit(_fetch_item, sid) for sid in selected_ids]
            for future in as_completed(futures):
                item = future.result()
                if item:
                    stories.append(item)
    logger.info("hn: got %d stories", len(stories))
    return stories


def fetch_reddit_hot(subreddit: str, limit: int = 25) -> list[dict]:
    # Try old.reddit.com first (less strict about auth), then www
    for base in ("https://old.reddit.com", "https://www.reddit.com"):
        try:
            url = f"{base}/r/{subreddit}/hot.json?limit={limit}"
            data = _http_get_json(url, timeout=15, ua=UA_REDDIT)
            break
        except Exception:
            continue
    else:
        logger.info("reddit: r/%s unavailable (needs OAuth)", subreddit)
        return []
    if not isinstance(data, dict):
        logger.warning("reddit: r/%s returned non-dict: %r", subreddit, data)
        return []
    if data.get("message") == "Too Many Requests" or data.get("error"):
        logger.warning(
            "reddit: r/%s rate-limited / error: %s",
            subreddit, data.get("message") or data.get("error"),
        )
        return []
    posts = []
    for child in (data.get("data") or {}).get("children") or []:
        d = (child.get("data") or {})
        title = d.get("title", "")
        if not title:
            continue
        sid = d.get("id", "")
        posts.append({
            "id": str(sid),
            "title": title,
            "url": f"https://www.reddit.com{d.get('permalink', '')}",
            "score": d.get("score", 0),
            "descendants": d.get("num_comments", 0),
        })
    logger.info("reddit: r/%s got %d posts", subreddit, len(posts))
    return posts


def fetch_lobsters(limit: int = 25) -> list[dict]:
    data = _http_get_json(LOBSTERS_HOTTEST_URL, timeout=15)
    if not isinstance(data, list):
        logger.warning("lobsters: returned non-list: %r", data)
        return []
    posts = []
    for item in data[:limit]:
        title = item.get("title", "")
        if not title:
            continue
        short_id = item.get("short_id", "")
        posts.append({
            "id": short_id,
            "title": title,
            "url": item.get("url") or f"https://lobste.rs/s/{short_id}",
            "score": item.get("score", 0),
            "descendants": item.get("comment_count", 0),
        })
    logger.info("lobsters: got %d posts", len(posts))
    return posts


def fetch_simonw_blog(limit: int = 10) -> list[dict]:
    try:
        body = _http_get(SIMONW_ATOM_URL, timeout=15).decode()
    except Exception as exc:
        logger.warning("simonw: fetch failed: %s", exc)
        return []
    # Simple Atom feed parsing
    entries = re.findall(r"<entry>(.*?)</entry>", body, re.DOTALL)
    posts = []
    for entry in entries[:limit]:
        title_m = re.search(r"<title>(.*?)</title>", entry, re.DOTALL)
        link_m = re.search(r'<link[^>]*href="([^"]*)"', entry)
        if not title_m:
            continue
        title = title_m.group(1).strip()
        url = link_m.group(1) if link_m else ""
        if not url:
            continue
        if url:
            entry_id = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
        else:
            entry_id = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
        posts.append({
            "id": entry_id,
            "title": title,
            "url": url,
            "score": 0,
            "descendants": 0,
        })
    logger.info("simonw: got %d posts", len(posts))
    return posts


def fetch_github_trending(limit: int = 25) -> list[dict]:
    try:
        body = _http_get(GH_TRENDING_URL, timeout=15).decode()
    except Exception as exc:
        logger.warning("github_trending: fetch failed: %s", exc)
        return []
    # Find repo blocks: <h2 class="h3 lh-condensed"><a href="/owner/repo">
    repos = re.findall(
        r'<h2[^>]*>\s*<a[^>]*href="(/[^"]+)"[^>]*>(.*?)</a>',
        body, re.DOTALL,
    )
    posts = []
    for repo_path, raw_text in repos[:limit]:
        text = re.sub(r"<[^>]+>", "", raw_text).strip()
        owner_repo = repo_path.strip("/")
        parts = owner_repo.split("/")
        if len(parts) != 2:
            continue
        owner, repo = parts
        title = f"{owner}/{repo}"
        if text and text != title.strip():
            title = f"{title} — {text}"
        posts.append({
            "id": owner_repo,
            "title": title,
            "url": f"https://github.com/{owner_repo}",
            "score": 0,
            "descendants": 0,
        })
    logger.info("github_trending: got %d repos", len(posts))
    return posts


def fetch_hf_papers(limit: int = 10) -> list[dict]:
    try:
        data = _http_get_json(HF_PAPERS_URL, timeout=15)
    except Exception as exc:
        logger.warning("hf_papers: fetch failed: %s", exc)
        return []
    if not isinstance(data, list):
        logger.warning("hf_papers: returned non-list: %r", data)
        return []
    posts = []
    for paper in data[:limit]:
        title = (paper.get("title") or "").strip()
        paper_id = paper.get("paper", {}).get("id", "")
        if not title or not paper_id:
            continue
        posts.append({
            "id": paper_id,
            "title": title,
            "url": f"https://huggingface.co/papers/{paper_id}",
            "score": paper.get("upvotes", 0),
            "descendants": 0,
        })
    logger.info("hf_papers: got %d papers", len(posts))
    return posts


def _producthunt_access_token() -> str:
    token = (os.environ.get("X_PRODUCT_HUNT_TOKEN") or os.environ.get("PRODUCT_HUNT_TOKEN") or "").strip()
    if token:
        return token
    client_id = (os.environ.get("X_PRODUCT_HUNT_API_KEY") or "").strip()
    client_secret = (os.environ.get("X_PRODUCT_HUNT_API_SECRET") or "").strip()
    if not client_id or not client_secret:
        return ""
    body = json.dumps(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        PRODUCT_HUNT_TOKEN_URL,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": UA_GENERIC,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HOTSPOT_HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("producthunt: token request failed: %s", exc)
        return ""
    return str(payload.get("access_token") or "").strip()


def fetch_producthunt_posts(limit: int = 20) -> list[dict]:
    access_token = _producthunt_access_token()
    if not access_token:
        logger.info("producthunt: skipped (missing token or client credentials)")
        return []
    query = """
query DailyPosts($first: Int!, $postedAfter: DateTime!) {
  posts(first: $first, order: VOTES, postedAfter: $postedAfter) {
    edges {
      node {
        id
        name
        tagline
        url
        votesCount
        commentsCount
      }
    }
  }
}
"""
    posted_after = (datetime.now(tz=BEIJING_TZ) - timedelta(hours=24)).replace(microsecond=0).isoformat()
    body = json.dumps({"query": query, "variables": {"first": limit, "postedAfter": posted_after}}).encode("utf-8")
    req = urllib.request.Request(
        PRODUCT_HUNT_GRAPHQL_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": UA_GENERIC,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HOTSPOT_HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("producthunt: fetch failed: %s", exc)
        return []
    edges = (((payload or {}).get("data") or {}).get("posts") or {}).get("edges") or []
    posts = []
    for edge in edges:
        node = (edge or {}).get("node") or {}
        name = str(node.get("name") or "").strip()
        tagline = str(node.get("tagline") or "").strip()
        pid = str(node.get("id") or name).strip()
        if not name or not pid:
            continue
        title = f"{name}: {tagline}" if tagline else name
        posts.append({
            "id": pid,
            "title": title,
            "url": node.get("url") or "https://www.producthunt.com/",
            "score": node.get("votesCount", 0),
            "descendants": node.get("commentsCount", 0),
        })
    logger.info("producthunt: got %d posts", len(posts))
    return posts


def fetch_tldr_ai(limit: int = 15) -> list[dict]:
    try:
        body = _http_get(TLDR_AI_URL, timeout=15).decode()
    except Exception as exc:
        logger.warning("tldr_ai: fetch failed: %s", exc)
        return []
    # Match article listings: <a ... href="...">Title</a>
    items = re.findall(
        r'<a[^>]*href="(https?://tldr\.tech/ai/[^"]+)"[^>]*>\s*(.*?)\s*</a>',
        body,
    )
    seen = set()
    posts = []
    for url, raw_title in items:
        title = re.sub(r"<[^>]+>", "", raw_title).strip()
        if not title or url in seen or "/archives/" in url or "/podcast/" in url:
            continue
        seen.add(url)
        slug = url.rstrip("/").split("/")[-1]
        posts.append({
            "id": slug,
            "title": title,
            "url": url,
            "score": 0,
            "descendants": 0,
        })
        if len(posts) >= limit:
            break
    logger.info("tldr_ai: got %d posts", len(posts))
    return posts


def _fetch_company_via_hn_search(company: str, label: str, limit: int = 10) -> list[dict]:
    """Fallback: fetch company news via HN Algolia search."""
    params = urllib.parse.urlencode({
        "query": company,
        "tags": "story",
        "hitsPerPage": limit,
    })
    url = f"{HN_ALGOLIA_URL}?{params}"
    data = _http_get_json(url, timeout=15)
    if not isinstance(data, dict):
        logger.warning("hn_algolia: returned non-dict for %s: %r", company, data)
        return []
    posts = []
    for hit in (data.get("hits") or [])[:limit]:
        title = hit.get("title", "")
        object_id = hit.get("objectID", "")
        if not title:
            continue
        title = f"[{label}] {title}"
        hn_url = f"https://news.ycombinator.com/item?id={object_id}"
        posts.append({
            "id": f"hn-{object_id}",
            "title": title,
            "url": hit.get("url") or hn_url,
            "score": hit.get("points", 0),
            "descendants": hit.get("num_comments", 0),
        })
    return posts


def _fetch_company_blog_rss(url: str, label: str, limit: int = 10) -> list[dict]:
    """Fetch company news from an RSS/Atom feed."""
    try:
        body = _http_get(url, timeout=15).decode()
    except Exception as exc:
        logger.warning("company %s: RSS fetch failed: %s", label, exc)
        return []
    entries = re.findall(r"<entry>(.*?)</entry>", body, re.DOTALL)
    if not entries:
        entries = re.findall(r"<item>(.*?)</item>", body, re.DOTALL)
    posts = []
    for entry in entries[:limit]:
        title_m = re.search(r"<title>(.*?)</title>", entry, re.DOTALL)
        link_m = re.search(r'<link[^>]*href="([^"]*)"', entry)
        if not link_m:
            link_m = re.search(r"<link>(.*?)</link>", entry)
        if not title_m:
            continue
        title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip()
        link = (link_m.group(1) if link_m else "").strip()
        if not title or not link:
            continue
        if link:
            entry_id = hashlib.sha1(link.encode("utf-8")).hexdigest()[:12]
        else:
            entry_id = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
        posts.append({
            "id": entry_id,
            "title": f"[{label}] {title}",
            "url": link,
            "score": 0,
            "descendants": 0,
        })
    logger.info("company %s: RSS got %d posts", label, len(posts))
    return posts


def _fetch_company_x_profile(company: str, limit: int = 10) -> list[dict]:
    """Scrape recent tweets from an X company profile via browser-harness."""
    if os.environ.get("X_HOTSPOT_ENABLE_X_SCRAPE", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return []
    from src.common import run_harness
    cfg = COMPANY_X_ACCOUNTS.get(company)
    if not cfg:
        return []
    profile_url = cfg["profile_url"]
    label = cfg["label"]

    js_code = (
        "JSON.stringify("
        "Array.from(document.querySelectorAll('[data-testid=\"tweet\"]'))"
        f".slice(0, {limit})"
        ".map(tweet => {"
        "const textEl = tweet.querySelector('[data-testid=\"tweetText\"]');"
        "const timeEl = tweet.querySelector('time');"
        "const linkEl = tweet.querySelectorAll('a[href*=\"/status/\"]');"
        "const statusUrl = linkEl.length > 0 ? (linkEl[linkEl.length - 1] || {}).href || '' : '';"
        "const statusId = statusUrl ? statusUrl.split('/status/')[1].split('/')[0] : '';"
        "return {text: textEl ? textEl.innerText : '', time: timeEl ? timeEl.getAttribute('datetime') : '', id: statusId};"
        "})"
        ".filter(t => t.text && t.id)"
        ")"
    )
    code = (
        f'goto("{profile_url}")\n'
        f'wait_for_load(5)\n'
        f'wait(2)\n'
        f'tweets_json = js({json.dumps(js_code)})\n'
        f'print(tweets_json)\n'
    )
    try:
        timeout = max(10, int(os.environ.get("X_HOTSPOT_X_SCRAPE_TIMEOUT", "25")))
        stdout = run_harness(code, timeout=timeout)
        tweets = json.loads(stdout.strip())
    except Exception as exc:
        logger.warning("company %s: X scrape failed: %s", label, exc)
        return []

    posts = []
    for tweet in tweets:
        text = tweet.get("text", "")
        tid = tweet.get("id", "")
        posts.append({
            "id": f"x-{tid}",
            "title": f"[{label}] {text[:120]}",
            "url": f"https://x.com/{cfg['handle']}/status/{tid}",
            "score": 0,
            "descendants": 0,
        })
    logger.info("company %s: X got %d tweets", label, len(posts))
    return posts


def fetch_company_news(company: str, limit: int = 10) -> list[dict]:
    """Fetch company news: X profile first > blog RSS > HN search fallback."""
    label = COMPANY_X_ACCOUNTS.get(company, {}).get("label", company)

    # 1. Try X profile via browser-harness
    posts = _fetch_company_x_profile(company, limit)
    if posts:
        return posts

    # 2. Try blog RSS (Google has one)
    blog_url = COMPANY_BLOG_URLS.get(company, "")
    if blog_url and ("rss" in blog_url.lower() or "feed" in blog_url.lower()):
        posts = _fetch_company_blog_rss(blog_url, label, limit)
        if posts:
            return posts

    # 3. Fall back to HN search
    logger.info("company %s: X and blog unavailable, using HN search", label)
    return _fetch_company_via_hn_search(company, label, limit)


# ---------------------------------------------------------------------------
# LLM filter
# ---------------------------------------------------------------------------

HOTSPOT_FILTER_PROMPT = """\
你在筛选与指定关注方向相关的热点新闻，用于 X 账号发帖。

输出严格 JSON：
{"relevant": true, "score": 3, "reason": "...", "angle": "...", "cn_summary": "..."}

关注方向（按优先级排序，必须与用户日常使用和关注的生态直接相关）：
- 【最高优先级】AI + vibe coding：AI 编程工具（Claude Code、Cursor、Copilot、Windsurf 等主流工具）、AI 工作流自动化、AI agent、LLM 辅助开发、独立开发者 AI 工作流
- 【高优先级】AI 工作流：AI 如何改变日常工作方式、AI 自动化实践、人与 AI 协作模式
- 【中等优先级】创业/startup、产品/增长、开发者工具（仅限主流、广为人知的工具）、web3/加密货币、金融/金融科技、半导体/光模块/硬件、自媒体创作
- 【低优先级】AI 其他方面（开源模型发布、学术论文）——仅供评分参考，不优先选取
- 自动排除：小众编程语言/框架（Rust/Mojo/Zig/Elixir 等语言更新，除非是 AI 相关重大发布）、冷门开源项目、纯技术基础设施、公司融资/IPO 新闻、前端 UI 库更新

规则：
- relevant: 是否与上述方向相关，AI+vibe coding 和 AI 工作流优先判断
- 以下类型必须判为不相关（relevant=false, score=1）：
  - 纯技术基础设施（数据库、网络协议、编程语言特性、加密算法、UUID、文件系统等）
  - 纯 CS 理论/学术论文（除非与 AI 直接相关）
  - 开源项目发布（除非是 AI 相关工具）
  - 公司融资/IPO/裁员（除非是 AI 或 web3 公司）
  - 前端框架/UI 库更新（除非明确涉及 AI 集成）
- 标题带 [OpenAI 动态]、[Anthropic 动态]、[Google AI 动态] 标记的，自动提一档评分（2→3, 3→4）
- score: 1-5 热度与讨论价值评分
  - 5=高热度且有独特切入角度，非常值得发帖
  - 4=相关且有观点空间
  - 3=相关但偏资讯，发帖角度有限
  - 2=弱相关
  - 1=不相关
- reason: 简短说明为什么相关或不相关，30字内
- angle: 推荐的发帖切入角度，20字内（不相关时为空）
- cn_summary: 中文摘要，60字内
- 必须用中文输出 reason、angle、cn_summary
- 只输出 JSON，不要 markdown 包裹
"""


def filter_hotspot(story: dict) -> dict:
    response = chat_json_result(
        [
            {"role": "system", "content": HOTSPOT_FILTER_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "title": story["title"],
                        "score": story["score"],
                        "descendants": story["descendants"],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        temperature=0.3,
        max_tokens=300,
    )
    payload = response["payload"]
    relevant = bool(payload.get("relevant"))
    cn_summary = str(payload.get("cn_summary") or "").strip()
    if relevant and not cn_summary:
        logger.warning(
            "filter_hotspot: relevant=true but empty cn_summary, treating as filtered_out (title=%.40s)",
            (story.get("title") or "")[:40],
        )
        relevant = False
    return {
        "relevant": relevant,
        "score": int(payload.get("score") or 0),
        "reason": str(payload.get("reason") or "").strip(),
        "angle": str(payload.get("angle") or "").strip(),
        "cn_summary": cn_summary,
        "cost": response.get("cost", {}),
        "usage": response.get("usage", {}),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

ALL_SOURCES = [
    # Community
    "hn",
    "producthunt",
    "reddit",
    "lobsters",
    "simonw",
    "github_trending",
    "hf_papers",
    "tldr_ai",
    # Companies
    "openai",
    "anthropic",
    "google",
]

DEFAULT_HOTSPOT_SOURCES = [
    "hn",
    "producthunt",
    "reddit",
    "lobsters",
    "simonw",
    "github_trending",
    "hf_papers",
    "tldr_ai",
    "openai",
    "anthropic",
    "google",
]

SOURCE_LABELS = {
    "hn": "HN",
    "producthunt": "Product Hunt",
    "reddit": "Reddit",
    "lobsters": "Lobsters",
    "simonw": "Simon Willison",
    "github_trending": "GitHub Trending",
    "hf_papers": "HuggingFace Papers",
    "tldr_ai": "TLDR AI",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google AI",
}

SOURCE_WEIGHTS = {
    "producthunt": 2.3,
    "hn": 1.2,
    "reddit": 1.15,
    "lobsters": 0.8,
    "hf_papers": 0.55,
    "tldr_ai": 0.65,
    "github_trending": 0.6,
    "simonw": 0.5,
    "openai": 1.1,
    "anthropic": 1.1,
    "google": 1.0,
}

KEYWORD_BOOSTS = [
    (
        180,
        [
            "vibe coding",
            "claude code",
            "cursor",
            "windsurf",
            "copilot",
            "ai coding",
            "agent workflow",
            "coding agent",
            "code agent",
            "developer agent",
        ],
    ),
    (
        130,
        [
            "ai agent",
            "agents",
            "workflow automation",
            "ai workflow",
            "llm app",
            "low-code",
            "nocode",
            "no-code",
        ],
    ),
    (
        80,
        [
            "llm",
            "openai",
            "anthropic",
            "claude",
            "chatgpt",
            "model",
            "automation",
            "developer tool",
            "devtool",
        ],
    ),
    (
        40,
        [
            "startup",
            "growth",
            "product",
            "crypto",
            "web3",
            "fintech",
            "semiconductor",
            "creator",
        ],
    ),
]

HIGH_PRIORITY_TOPIC_KEYWORDS = [
    "vibe coding",
    "vibe",
    "claude code",
    "cursor",
    "windsurf",
    "copilot",
    "agentic",
    "ai agent",
    "agents",
    "coding agent",
    "developer agent",
    "openai",
    "anthropic",
    "chatgpt",
    "claude",
    "gemini",
    "llm",
]


def _configured_sources() -> list[str]:
    raw = os.environ.get("X_HOTSPOT_SOURCES", "").strip()
    if not raw:
        return list(DEFAULT_HOTSPOT_SOURCES)
    allowed = set(ALL_SOURCES)
    selected: list[str] = []
    for part in raw.split(","):
        source = part.strip().lower()
        if not source or source not in allowed or source in selected:
            continue
        selected.append(source)
    return selected or list(DEFAULT_HOTSPOT_SOURCES)


def _int_metric(story: dict, key: str) -> int:
    try:
        return int(story.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _keyword_boost(story: dict) -> int:
    title = str(story.get("title") or "").lower()
    return sum(boost for boost, keywords in KEYWORD_BOOSTS if any(keyword in title for keyword in keywords))


def _high_priority_topic_keyword(story: dict) -> str:
    title = str(story.get("title") or "").lower()
    for keyword in HIGH_PRIORITY_TOPIC_KEYWORDS:
        if keyword in title:
            return keyword
    return ""


def _apply_local_relevance_floor(story: dict, result: dict) -> dict:
    keyword = _high_priority_topic_keyword(story)
    if not keyword:
        return result

    promoted = dict(result)
    score = int(promoted.get("score") or 0)
    if promoted.get("relevant") and score >= 3:
        return promoted

    title = str(story.get("title") or "").strip()
    promoted["relevant"] = True
    promoted["score"] = max(score, 3)
    if not str(promoted.get("reason") or "").strip() or score < 3:
        promoted["reason"] = f"命中高优先级方向：{keyword}"
    if not str(promoted.get("angle") or "").strip():
        promoted["angle"] = "AI工作流变化"
    if not str(promoted.get("cn_summary") or "").strip():
        promoted["cn_summary"] = title[:60]
    promoted["local_floor"] = keyword
    return promoted


def _candidate_rank_score(story: dict) -> float:
    source = str(story.get("source") or "")
    engagement = _int_metric(story, "score") + (_int_metric(story, "descendants") * 2)
    return (engagement * SOURCE_WEIGHTS.get(source, 0.7)) + _keyword_boost(story)


def _candidate_sort_key(story: dict) -> tuple[float, int, int, str, str, str]:
    return (
        -_candidate_rank_score(story),
        -_int_metric(story, "score"),
        -_int_metric(story, "descendants"),
        str(story.get("source") or ""),
        str(story.get("id") or ""),
        str(story.get("title") or ""),
    )


def _select_llm_candidates(
    stories: list[dict],
    *,
    limit: int = HOTSPOT_LLM_CANDIDATES,
    is_seen_func: Callable[[str, str], bool] = is_seen,
) -> tuple[list[dict], int]:
    unseen: list[dict] = []
    skipped_seen = 0
    for story in stories:
        source = str(story.get("source") or "")
        sid = str(story.get("id") or "")
        if is_seen_func(source, sid):
            skipped_seen += 1
            continue
        unseen.append(story)
    return sorted(unseen, key=_candidate_sort_key)[:limit], skipped_seen


def _fetch_source(source: str) -> list[dict]:
    """Fetch stories from a single source. Returns list of {source, id, title, url, score, descendants}."""
    stories: list[dict] = []

    if source == "hn":
        stories = fetch_hn_top_stories()
        return [{"source": source, **s} for s in stories]

    elif source == "producthunt":
        stories = fetch_producthunt_posts()

    elif source == "reddit":
        with ThreadPoolExecutor(max_workers=len(REDDIT_SUBREDDITS)) as executor:
            future_to_sub = {executor.submit(fetch_reddit_hot, sub, 12): sub for sub in REDDIT_SUBREDDITS}
            for future in as_completed(future_to_sub):
                sub = future_to_sub[future]
                try:
                    stories.extend(future.result())
                except Exception as exc:
                    logger.warning("reddit r/%s fetch failed: %s", sub, exc)

    elif source == "lobsters":
        try:
            stories = fetch_lobsters()
        except Exception as exc:
            logger.warning("lobsters fetch failed: %s", exc)

    elif source == "simonw":
        stories = fetch_simonw_blog()

    elif source == "github_trending":
        stories = fetch_github_trending()

    elif source == "hf_papers":
        stories = fetch_hf_papers()

    elif source == "tldr_ai":
        stories = fetch_tldr_ai()

    elif source in COMPANY_X_ACCOUNTS:
        stories = fetch_company_news(source)

    return [{"source": source, **s} for s in stories]


def discover_hotspots(sources: list[str] | None = None) -> dict:
    if sources is None:
        sources = _configured_sources()

    all_stories: list[dict] = []
    total_cost = 0.0
    source_stats: dict[str, int] = {}
    source_durations: dict[str, float] = {}

    def _fetch_timed(source: str) -> tuple[str, list[dict], float]:
        label = SOURCE_LABELS.get(source, source)
        started = time.time()
        try:
            logger.info("discover: fetching %s", label)
            items = _fetch_source(source)
            elapsed = time.time() - started
            logger.info("discover: %s → %d items in %.2fs", label, len(items), elapsed)
            return source, items, elapsed
        except Exception as exc:
            logger.error("discover: %s fetch failed: %s", label, exc)
            return source, [], time.time() - started

    with ThreadPoolExecutor(max_workers=min(HOTSPOT_SOURCE_WORKERS, max(1, len(sources)))) as executor:
        future_to_source = {executor.submit(_fetch_timed, source): source for source in sources}
        for future in as_completed(future_to_source):
            source, items, elapsed = future.result()
            source_stats[source] = len(items)
            source_durations[source] = round(elapsed, 3)
            all_stories.extend(items)

    logger.info("discover: %d sources → %d total stories", len(sources), len(all_stories))

    llm_limit = _env_int("X_HOTSPOT_LLM_CANDIDATES", HOTSPOT_LLM_CANDIDATES, min_value=HOTSPOT_MAX_CANDIDATES)
    top_stories, skipped_seen = _select_llm_candidates(all_stories, limit=llm_limit)
    logger.info(
        "discover: selected top %d unseen candidates from %d total stories",
        len(top_stories), len(all_stories),
    )

    discovered = 0
    added = 0
    filtered_out = 0
    errors = 0
    relevant_items: list[dict] = []
    filtered_items: list[dict] = []

    for story in top_stories:
        source = story["source"]
        sid = story["id"]

        discovered += 1
        try:
            result = filter_hotspot(story)
        except Exception as exc:
            errors += 1
            logger.warning(
                "discover: LLM filter failed for %s:%s title=%.40s: %s",
                source, sid, story.get("title", ""), exc,
            )
            continue
        result = _apply_local_relevance_floor(story, result)

        total_cost += float(result["cost"].get("total_cost") or 0.0)
        relevant = result["relevant"] and result["score"] >= 3
        logger.info(
            "discover: %s:%s score=%d relevant=%s reason=%s",
            source, sid, result["score"], relevant, result["reason"],
        )

        insert_hotspot(
            source=source,
            hotspot_id=sid,
            title=story["title"],
            url=story["url"],
            hn_score=story.get("score", 0),
            hn_descendants=story.get("descendants", 0),
            relevance_score=result["score"],
            relevance_reason=result["reason"],
            angle=result["angle"],
            cn_summary=result["cn_summary"],
        )

        if relevant:
            relevant_items.append({
                "source": source,
                "id": sid,
                "title": story["title"],
                "url": story["url"],
                "hn_score": story.get("score", 0),
                "hn_descendants": story.get("descendants", 0),
                "rank_score": round(_candidate_rank_score(story), 3),
                "relevance_score": result["score"],
                "relevance_reason": result["reason"],
                "angle": result["angle"],
                "cn_summary": result["cn_summary"],
            })
        else:
            filtered_out += 1
            filtered_items.append({
                "source": source,
                "id": sid,
                "title": story["title"],
                "relevance_score": result["score"],
                "relevance_reason": result["reason"],
            })

    relevant_items.sort(
        key=lambda item: (
            -int(item.get("relevance_score") or 0),
            -float(item.get("rank_score") or 0.0),
            str(item.get("source") or ""),
            str(item.get("id") or ""),
        )
    )
    items = relevant_items[:HOTSPOT_MAX_CANDIDATES]
    added = len(items)

    logger.info(
        "discover: done discovered=%d added=%d skipped=%d filtered=%d errors=%d cost=%.6f",
        discovered, added, skipped_seen, filtered_out, errors, total_cost,
    )

    return {
        "ok": True,
        "discovered": discovered,
        "added": added,
        "skipped_seen": skipped_seen,
        "filtered_out": filtered_out,
        "errors": errors,
        "source_stats": source_stats,
        "source_durations": source_durations,
        "items": items,
        "filtered_items": filtered_items[:10],
        "total_cost_cny": round(total_cost, 8),
    }
