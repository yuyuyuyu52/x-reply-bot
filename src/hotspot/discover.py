#!/usr/bin/env python3
"""Hotspot discovery: fetch from 10+ external sources, LLM filter, score."""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

from src.common import chat_json_result
from src.hotspot.store import is_seen, insert_hotspot
from src.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

HN_TOP_STORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{}.json"
HN_MAX_FETCH = 30

REDDIT_SUBREDDITS = [
    "ChatGPTCoding",
    "ClaudeAI",
    "artificial",
    "programming",
    "startups",
]

LOBSTERS_HOTTEST_URL = "https://lobste.rs/hottest.json"
SIMONW_ATOM_URL = "https://simonwillison.net/atom/entries/"
HF_PAPERS_URL = "https://huggingface.co/api/daily_papers"
TLDR_AI_URL = "https://tldr.tech/ai"
GH_TRENDING_URL = "https://github.com/trending?since=daily"

# Company blogs / RSS
COMPANY_SOURCES = {
    "openai": {
        "blog_url": "",
        "label": "OpenAI 动态",
    },
    "anthropic": {
        "blog_url": "",
        "label": "Anthropic 动态",
    },
    "google": {
        "blog_url": "https://blog.google/technology/ai/rss/",
        "label": "Google AI 动态",
    },
}
HN_ALGOLIA_URL = "https://hn.algolia.com/api/v1/search_by_date"

UA_GENERIC = "Mozilla/5.0 (compatible; x-reply-bot/1.0)"
UA_REDDIT = "python:x-reply-bot:v1.0 (by /u/indiedev)"


def _http_get(url: str, timeout: int = 15, retries: int = 3, ua: str | None = None) -> bytes:
    headers = {"User-Agent": ua or UA_GENERIC}
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


def _http_get_json(url: str, timeout: int = 15, ua: str | None = None) -> dict | list:
    return json.loads(_http_get(url, timeout, ua=ua).decode())


# ---------------------------------------------------------------------------
# Fetch functions
# ---------------------------------------------------------------------------


def fetch_hn_top_stories(limit: int = HN_MAX_FETCH) -> list[dict]:
    logger.info("hn: requesting top stories")
    ids = _http_get_json(HN_TOP_STORIES_URL)
    stories: list[dict] = []
    for sid in ids[:limit]:
        try:
            item = _http_get_json(HN_ITEM_URL.format(sid))
        except Exception:
            continue
        if not item or not item.get("title"):
            continue
        stories.append({
            "id": str(item.get("id", "")),
            "title": item.get("title", ""),
            "url": item.get("url") or f"https://news.ycombinator.com/item?id={item['id']}",
            "score": item.get("score", 0),
            "descendants": item.get("descendants", 0),
        })
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
        posts.append({
            "id": url.split("/")[-2] or url.split("/")[-1] or title[:20],
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


def _fetch_company_via_hn_search(company: str, limit: int = 10) -> list[dict]:
    """Fallback: fetch company news via HN Algolia search."""
    cfg = COMPANY_SOURCES.get(company)
    if not cfg:
        return []
    params = urllib.parse.urlencode({
        "query": company,
        "tags": "story",
        "hitsPerPage": limit,
    })
    url = f"{HN_ALGOLIA_URL}?{params}"
    data = _http_get_json(url, timeout=15)
    posts = []
    for hit in (data.get("hits") or [])[:limit]:
        title = hit.get("title", "")
        object_id = hit.get("objectID", "")
        if not title:
            continue
        title = f"[{cfg['label']}] {title}"
        hn_url = f"https://news.ycombinator.com/item?id={object_id}"
        posts.append({
            "id": f"algolia-{object_id}",
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
        if not title:
            continue
        if not link:
            link = url
        posts.append({
            "id": link.split("/")[-2] or link.split("/")[-1] or title[:20],
            "title": f"[{label}] {title}",
            "url": link,
            "score": 0,
            "descendants": 0,
        })
    logger.info("company %s: RSS got %d posts", label, len(posts))
    return posts


def fetch_company_news(company: str, limit: int = 10) -> list[dict]:
    """Fetch company news: try official blog/RSS first, fall back to HN search."""
    cfg = COMPANY_SOURCES.get(company)
    if not cfg:
        return []
    blog_url = cfg.get("blog_url", "")
    label = cfg.get("label", company)

    if blog_url and ("rss" in blog_url.lower() or "feed" in blog_url.lower()):
        posts = _fetch_company_blog_rss(blog_url, label, limit)
        if posts:
            return posts

    # Try scraping blog HTML if not RSS
    if blog_url:
        try:
            body = _http_get(blog_url, timeout=15, ua=UA_GENERIC).decode()
        except Exception as exc:
            logger.warning("company %s: blog blocked (Cloudflare?), using HN search: %s", label, exc)
            return _fetch_company_via_hn_search(company, limit)

        # Extract article links from HTML
        links = re.findall(
            r'<a[^>]*href="([^"]*)"[^>]*>\s*([^<]{10,120}?)\s*</a>',
            body,
        )
        posts = []
        seen = set()
        for href, raw_title in links[:limit * 3]:
            title = re.sub(r"<[^>]+>", "", raw_title).strip()
            if len(title) < 15 or title in seen:
                continue
            seen.add(title)
            full_url = href if href.startswith("http") else f"{blog_url.rstrip('/')}{href}"
            posts.append({
                "id": full_url.split("/")[-2] or title[:20],
                "title": f"[{label}] {title}",
                "url": full_url,
                "score": 0,
                "descendants": 0,
            })
            if len(posts) >= limit:
                break
        if posts:
            logger.info("company %s: blog HTML got %d posts", label, len(posts))
            return posts

        # Fall back to HN search if blog scraping yielded nothing
        logger.info("company %s: blog HTML empty, falling back to HN search", label)

    return _fetch_company_via_hn_search(company, limit)


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
    return {
        "relevant": bool(payload.get("relevant")),
        "score": int(payload.get("score") or 0),
        "reason": str(payload.get("reason") or "").strip(),
        "angle": str(payload.get("angle") or "").strip(),
        "cn_summary": str(payload.get("cn_summary") or "").strip(),
        "cost": response.get("cost", {}),
        "usage": response.get("usage", {}),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

ALL_SOURCES = [
    # Community
    "hn",
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

SOURCE_LABELS = {
    "hn": "HN",
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


def _fetch_source(source: str) -> list[dict]:
    """Fetch stories from a single source. Returns list of {source, id, title, url, score, descendants}."""
    stories: list[dict] = []

    if source == "hn":
        stories = fetch_hn_top_stories()
        return [{"source": source, **s} for s in stories]

    elif source == "reddit":
        for sub in REDDIT_SUBREDDITS:
            try:
                items = fetch_reddit_hot(sub)
                stories.extend(items)
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

    elif source in COMPANY_SOURCES:
        stories = fetch_company_news(source)

    return [{"source": source, **s} for s in stories]


def discover_hotspots(sources: list[str] | None = None) -> dict:
    if sources is None:
        sources = ALL_SOURCES

    all_stories: list[dict] = []
    total_cost = 0.0
    source_stats: dict[str, int] = {}

    for source in sources:
        label = SOURCE_LABELS.get(source, source)
        try:
            logger.info("discover: fetching %s", label)
            items = _fetch_source(source)
            all_stories.extend(items)
            source_stats[source] = len(items)
            logger.info("discover: %s → %d items", label, len(items))
        except Exception as exc:
            logger.error("discover: %s fetch failed: %s", label, exc)
            source_stats[source] = -1
            # Continue with other sources; don't abort the whole run

    logger.info("discover: %d sources → %d total stories", len(sources), len(all_stories))

    discovered = 0
    added = 0
    skipped_seen = 0
    filtered_out = 0
    errors = 0
    items: list[dict] = []

    for story in all_stories:
        source = story["source"]
        sid = story["id"]
        if is_seen(source, sid):
            skipped_seen += 1
            continue

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
            added += 1
            items.append({
                "source": source,
                "id": sid,
                "title": story["title"],
                "url": story["url"],
                "hn_score": story.get("score", 0),
                "hn_descendants": story.get("descendants", 0),
                "relevance_score": result["score"],
                "relevance_reason": result["reason"],
                "angle": result["angle"],
                "cn_summary": result["cn_summary"],
            })
        else:
            filtered_out += 1

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
        "items": items,
        "total_cost_cny": round(total_cost, 8),
    }
