# 热点功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 自动发现 AI/web3/金融/创业等方向的热点话题，LLM 筛选评分后加入发帖队列，bot 自动跟随热点发帖。

**Architecture:** 新增 `src/hotspot/` 模块负责热点发现和存储；新增 `discover_hotspots.py` 入口脚本；在 `bot_daemon.py` 中加入第五个 job 类型；热点以 `news_react` 类型注入 `post_topics.json` 队列，复用现有 `post_once.py` 发送管线。HN API 作为首个数据源（免费、无需认证），后续可扩展更多源。

**Tech Stack:** Python stdlib (urllib, sqlite3, json), 复用现有 LLM client (chat_json_result), 复用现有 browser harness (后续 X Trending 源)

---

## 文件结构

```
Create:  src/hotspot/__init__.py          # 空文件，使 hotspot 成为包
Create:  src/hotspot/store.py             # SQLite 热点存储，去重 + 查询
Create:  src/hotspot/discover.py          # 热点发现：HN API 抓取 + LLM 过滤评分
Create:  discover_hotspots.py             # CLI 入口，一键发现热点并入队
Modify:  common.py                        # 添加 HOTSPOT_STORE_PATH, HOTSPOT_HISTORY_DIR, LATEST_HOTSPOT_PATH
Modify:  bot_daemon.py                    # 添加热点 job 调度、carry_over、Telegram 命令
Modify:  sync_tg_commands.py              # 添加 /hotspot_discover, /hotspot_status 命令
Modify:  .env.example                     # 添加热点相关环境变量
```

---

### Task 1: 创建热点存储模块

**Files:**
- Create: `src/hotspot/__init__.py`
- Create: `src/hotspot/store.py`

- [ ] **Step 1: 创建 `src/hotspot/__init__.py`**

```bash
touch src/hotspot/__init__.py
```

- [ ] **Step 2: 编写 `src/hotspot/store.py`**

```python
#!/usr/bin/env python3
"""Hotspot discovery storage – SQLite-backed dedup and query."""
from __future__ import annotations

import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path


def _db_path() -> Path:
    return Path(os.environ.get(
        "X_HOTSPOT_STORE_PATH",
        str(Path(__file__).resolve().parent.parent.parent / "state" / "hotspot.db"),
    ))


SCHEMA = [
    """\
CREATE TABLE IF NOT EXISTS hotspots (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    hn_score INTEGER NOT NULL DEFAULT 0,
    hn_descendants INTEGER NOT NULL DEFAULT 0,
    relevance_score INTEGER NOT NULL DEFAULT 0,
    relevance_reason TEXT NOT NULL DEFAULT '',
    angle TEXT NOT NULL DEFAULT '',
    cn_summary TEXT NOT NULL DEFAULT '',
    discovered_at TEXT NOT NULL DEFAULT '',
    added_to_queue INTEGER NOT NULL DEFAULT 0
);
""",
    "CREATE INDEX IF NOT EXISTS idx_hotspots_source ON hotspots(source);",
    "CREATE INDEX IF NOT EXISTS idx_hotspots_discovered ON hotspots(discovered_at);",
]


def _ensure_schema(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA:
        conn.execute(stmt)
    conn.commit()


def _get_conn() -> sqlite3.Connection:
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    _ensure_schema(conn)
    return conn


def is_seen(source: str, hotspot_id: str) -> bool:
    conn = _get_conn()
    cur = conn.execute("SELECT 1 FROM hotspots WHERE id = ?", (f"{source}:{hotspot_id}",))
    return cur.fetchone() is not None


def insert_hotspot(
    source: str,
    hotspot_id: str,
    title: str,
    url: str,
    hn_score: int = 0,
    hn_descendants: int = 0,
    relevance_score: int = 0,
    relevance_reason: str = "",
    angle: str = "",
    cn_summary: str = "",
) -> None:
    conn = _get_conn()
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    conn.execute(
        """\
INSERT OR IGNORE INTO hotspots
    (id, source, title, url, hn_score, hn_descendants,
     relevance_score, relevance_reason, angle, cn_summary, discovered_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""",
        (
            f"{source}:{hotspot_id}",
            source,
            title,
            url,
            hn_score,
            hn_descendants,
            relevance_score,
            relevance_reason,
            angle,
            cn_summary,
            now,
        ),
    )
    conn.commit()


def mark_added_to_queue(source: str, hotspot_id: str) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE hotspots SET added_to_queue = 1 WHERE id = ?",
        (f"{source}:{hotspot_id}",),
    )
    conn.commit()


def recent_hotspots(days: int = 1, limit: int = 20) -> list[dict]:
    conn = _get_conn()
    cutoff = (datetime.now().astimezone()).strftime("%Y-%m-%d")
    rows = conn.execute(
        """\
SELECT id, source, title, url, hn_score, hn_descendants,
       relevance_score, relevance_reason, angle, cn_summary,
       discovered_at, added_to_queue
FROM hotspots
WHERE discovered_at >= ? AND added_to_queue = 1
ORDER BY relevance_score DESC, hn_score DESC
LIMIT ?
""",
        (cutoff, limit),
    ).fetchall()
    return [
        {
            "id": row[0],
            "source": row[1],
            "title": row[2],
            "url": row[3],
            "hn_score": row[4],
            "hn_descendants": row[5],
            "relevance_score": row[6],
            "relevance_reason": row[7],
            "angle": row[8],
            "cn_summary": row[9],
            "discovered_at": row[10],
            "added_to_queue": row[11],
        }
        for row in rows
    ]


def hotspot_stats() -> dict:
    conn = _get_conn()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    total = conn.execute("SELECT COUNT(*) FROM hotspots").fetchone()[0]
    added = conn.execute(
        "SELECT COUNT(*) FROM hotspots WHERE added_to_queue = 1"
    ).fetchone()[0]
    today_discovered = conn.execute(
        "SELECT COUNT(*) FROM hotspots WHERE discovered_at LIKE ?",
        (f"{today}%",),
    ).fetchone()[0]
    today_added = conn.execute(
        "SELECT COUNT(*) FROM hotspots WHERE discovered_at LIKE ? AND added_to_queue = 1",
        (f"{today}%",),
    ).fetchone()[0]
    return {
        "total_discovered": total,
        "total_added_to_queue": added,
        "today_discovered": today_discovered,
        "today_added_to_queue": today_added,
    }
```

- [ ] **Step 3: 验证 store 模块可导入**

```bash
cd /Users/Zhuanz/Documents/x-reply-bot && python3 -c "from src.hotspot.store import is_seen, insert_hotspot, recent_hotspots; print('store OK')"
```

- [ ] **Step 4: Commit**

```bash
git add src/hotspot/__init__.py src/hotspot/store.py
git commit -m "feat: add hotspot store module with SQLite backend"
```

---

### Task 2: 创建热点发现模块

**Files:**
- Create: `src/hotspot/discover.py`

- [ ] **Step 1: 编写 `src/hotspot/discover.py`**

```python
#!/usr/bin/env python3
"""Hotspot discovery: fetch from external sources, LLM filter, score."""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone

from src.common import chat_json_result
from src.hotspot.store import is_seen, insert_hotspot

HN_TOP_STORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{}.json"
HN_MAX_FETCH = 30

HOTSPOT_FILTER_PROMPT = """\
你在筛选与指定关注方向相关的热点新闻，用于 X 账号发帖。

输出严格 JSON：
{"relevant": true, "score": 3, "reason": "...", "angle": "...", "cn_summary": "..."}

关注方向：AI 与 LLM、web3/加密货币、金融科技、半导体、光模块/硬件、创业/startup、产品/增长、开发者工具、自媒体创作。

规则：
- relevant: 是否与上述方向相关
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


def fetch_hn_top_stories(limit: int = HN_MAX_FETCH) -> list[dict]:
    """Fetch top stories from Hacker News API."""
    req = urllib.request.Request(
        HN_TOP_STORIES_URL,
        headers={"User-Agent": "x-reply-bot/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        ids = json.loads(resp.read().decode())

    stories: list[dict] = []
    for sid in ids[:limit]:
        try:
            item_req = urllib.request.Request(
                HN_ITEM_URL.format(sid),
                headers={"User-Agent": "x-reply-bot/1.0"},
            )
            with urllib.request.urlopen(item_req, timeout=10) as resp:
                item = json.loads(resp.read().decode())
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
    return stories


def filter_hotspot(story: dict) -> dict:
    """Use LLM to filter and score a single story."""
    result, payload = chat_json_result(
        [
            {"role": "system", "content": HOTSPOT_FILTER_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "title": story["title"],
                        "hn_score": story["score"],
                        "hn_comments": story["descendants"],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        temperature=0.3,
        max_tokens=300,
    )
    return {
        "relevant": bool(payload.get("relevant")),
        "score": int(payload.get("score") or 0),
        "reason": str(payload.get("reason") or "").strip(),
        "angle": str(payload.get("angle") or "").strip(),
        "cn_summary": str(payload.get("cn_summary") or "").strip(),
        "cost": result.get("cost", {}),
        "usage": result.get("usage", {}),
    }


def discover_hotspots(sources: list[str] | None = None) -> dict:
    """Run a discovery cycle. Returns a dict with stats + discovered items.

    sources: list of source names, e.g. ["hn"]. Defaults to ["hn"].
    """
    if sources is None:
        sources = ["hn"]

    started = datetime.now().astimezone()
    all_stories: list[dict] = []
    total_cost = 0.0

    # --- Fetch ---
    for source in sources:
        if source == "hn":
            try:
                stories = fetch_hn_top_stories()
                all_stories.extend({"source": "hn", **s} for s in stories)
            except Exception as exc:
                return {
                    "ok": False,
                    "error": f"hn_fetch_failed: {exc}",
                    "discovered": 0,
                    "added": 0,
                    "skipped_seen": 0,
                    "filtered_out": 0,
                    "items": [],
                    "total_cost_cny": 0.0,
                }

    # --- Filter: dedup + LLM score ---
    discovered = 0
    added = 0
    skipped_seen = 0
    filtered_out = 0
    items: list[dict] = []

    for story in all_stories:
        source = story["source"]
        sid = story["id"]
        if is_seen(source, sid):
            skipped_seen += 1
            continue

        discovered += 1
        result = filter_hotspot(story)
        total_cost += float(result["cost"].get("total_cost") or 0.0)
        relevant = result["relevant"] and result["score"] >= 3

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

    return {
        "ok": True,
        "discovered": discovered,
        "added": added,
        "skipped_seen": skipped_seen,
        "filtered_out": filtered_out,
        "items": items,
        "total_cost_cny": round(total_cost, 8),
    }
```

- [ ] **Step 2: Commit**

```bash
git add src/hotspot/discover.py
git commit -m "feat: add hotspot discovery module with HN source + LLM filter"
```

---

### Task 3: 创建热点发现入口脚本

**Files:**
- Create: `discover_hotspots.py`

- [ ] **Step 1: 编写 `discover_hotspots.py`**

```python
#!/usr/bin/env python3
"""Hotspot discovery entrypoint – fetch trends, filter, add to topic queue."""
from __future__ import annotations

import argparse
import fcntl
import json
import sys
from datetime import datetime
from pathlib import Path

from src.common import (
    POST_TOPICS_PATH,
    ensure_state_dirs,
    load_env_file,
    load_json,
    normalize_post_topic,
    save_post_topics,
    telegram_enabled,
    telegram_notify,
    write_json,
)
from src.hotspot.discover import discover_hotspots
from src.hotspot.store import mark_added_to_queue, hotspot_stats

ROOT = Path(__file__).resolve().parent
HOTSPOT_LOCK_PATH = ROOT / "state" / "hotspot_discover.lock"
HOTSPOT_HISTORY_DIR = ROOT / "state" / "hotspot_history"
LATEST_HOTSPOT_PATH = ROOT / "state" / "latest_hotspot_run.json"


def _persist(record: dict, stamp: str) -> None:
    write_json(LATEST_HOTSPOT_PATH, record)
    HOTSPOT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    safe = stamp.replace("/", "_").replace("\\", "_")
    write_json(HOTSPOT_HISTORY_DIR / f"{safe}.json", record)


def _notify(record: dict) -> None:
    if not telegram_enabled():
        return
    items = record.get("added_items", [])
    lines = [
        "🔥 热点发现",
        "",
        f"🕒 时间: {record['time_beijing']}",
        f"⚙️ 触发: {record['trigger']}",
        f"📊 发现: {record['discovered']} 条新热点",
        f"✅ 入库: {record['added']} 条",
        f"⏭️ 已见: {record['skipped_seen']} 条",
        f"❌ 过滤: {record['filtered_out']} 条",
        f"💰 Cost: {record['total_cost_cny']:.6f} 元",
    ]
    if items:
        lines.append("")
        lines.append("📌 新入队热点:")
        for item in items[:5]:
            stars = "⭐" * max(1, item.get("relevance_score", 3))
            lines.append(f"• {stars} [{item.get('source', '')}] {item.get('cn_summary', '')}")
            lines.append(f"  🎯 {item.get('angle', '')}")
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3750] + "\n\n[通知过长，已截断]"
    try:
        telegram_notify(text)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_env_file()
    ensure_state_dirs()

    lock_fh = HOTSPOT_LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("hotspot_discover already running")
        return 3

    started = datetime.now().astimezone()
    stamp = started.strftime("%Y%m%d_%H%M%S")

    try:
        result = discover_hotspots()
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()

    if not result.get("ok"):
        record = {
            "time_beijing": started.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "date_beijing": started.strftime("%Y-%m-%d"),
            "trigger": args.trigger,
            "status": "error",
            "error": result.get("error", ""),
            "total_cost_cny": result.get("total_cost_cny", 0.0),
        }
        _persist(record, stamp)
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 1

    # --- Add relevant hotspots to topic queue ---
    items = result.get("items", [])
    added_items: list[dict] = []
    if items and not args.dry_run:
        data = load_json(POST_TOPICS_PATH, {"topics": []})
        topics = data.get("topics", [])
        for item in items:
            topic = normalize_post_topic({
                "id": f"hotspot-{item['source']}-{item['id']}",
                "type": "news_react",
                "text": item["cn_summary"],
                "source": "hotspot",
                "status": "pending",
                "subject": item["title"],
                "event_or_context": f"[{item['source']}] {item['relevance_reason']} | 原链接: {item['url']}",
                "stance": item["angle"],
                "evidence_hint": f"热度: {item['hn_score']}↑ {item['hn_descendants']}💬 | 相关度: {item['relevance_score']}/5",
            })
            topics.append(topic)
            mark_added_to_queue(item["source"], item["id"])
            added_items.append({
                "source": item["source"],
                "title": item["title"],
                "cn_summary": item["cn_summary"],
                "angle": item["angle"],
                "relevance_score": item["relevance_score"],
            })
        save_post_topics(data)

    record = {
        "time_beijing": started.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date_beijing": started.strftime("%Y-%m-%d"),
        "trigger": args.trigger,
        "dry_run": args.dry_run,
        "status": "ok",
        "discovered": result.get("discovered", 0),
        "added": result.get("added", 0),
        "skipped_seen": result.get("skipped_seen", 0),
        "filtered_out": result.get("filtered_out", 0),
        "added_items": added_items,
        "total_cost_cny": result.get("total_cost_cny", 0.0),
    }
    _persist(record, stamp)
    _notify(record)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Commit**

```bash
git add discover_hotspots.py
git commit -m "feat: add discover_hotspots.py entrypoint"
```

---

### Task 4: 添加热点状态路径和 env helpers 到 common.py

**Files:**
- Modify: `common.py`

- [ ] **Step 1: 在 `common.py` 中添加热点状态路径常量**

在 `LATEST_REVISIT_RUN_PATH` 那行之后（第 39 行之后）插入：

```python
HOTSPOT_STORE_PATH = STATE_DIR / "hotspot.db"
HOTSPOT_HISTORY_DIR = STATE_DIR / "hotspot_history"
LATEST_HOTSPOT_RUN_PATH = STATE_DIR / "latest_hotspot_run.json"
```

- [ ] **Step 2: 验证导入**

```bash
cd /Users/Zhuanz/Documents/x-reply-bot && python3 -c "from src.common import HOTSPOT_STORE_PATH, HOTSPOT_HISTORY_DIR, LATEST_HOTSPOT_RUN_PATH; print(HOTSPOT_STORE_PATH); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add common.py
git commit -m "feat: add hotspot state paths to common.py"
```

---

### Task 5: 在 bot_daemon.py 中添加热点 job 调度

**Files:**
- Modify: `bot_daemon.py`

- [ ] **Step 1: 添加热点 env helpers（在 `bot_daemon.py` 的 helper 函数区域，`learning_guard_seconds` 之后）**

```python
def hotspot_enabled() -> bool:
    return os.environ.get("X_HOTSPOT_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}


def hotspot_interval_seconds() -> int:
    try:
        return max(600, int(os.environ.get("X_HOTSPOT_INTERVAL_SECONDS", "7200")))
    except ValueError:
        return 7200


def hotspot_guard_seconds() -> int:
    try:
        return max(60, int(os.environ.get("X_HOTSPOT_GUARD_SECONDS", "600")))
    except ValueError:
        return 600


def next_hotspot_after(now: datetime) -> datetime:
    return now + timedelta(seconds=hotspot_interval_seconds())


def count_hotspot_posts_today(date_str: str) -> int:
    # Count today's posts that originated from hotspot topics
    total = 0
    for path in sorted(POST_HISTORY_DIR.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if item.get("date_beijing") == date_str and item.get("topic_source") == "hotspot":
            total += 1
    return total


def hotspot_daily_limit() -> int:
    try:
        return max(1, int(os.environ.get("X_HOTSPOT_DAILY_LIMIT", "3")))
    except ValueError:
        return 3
```

- [ ] **Step 2: 在 `status_text` 函数中添加热点行**

在 `status_text` 函数中，`next_revisit_at` 那行之后添加：

```python
    if hotspot_enabled():
        lines.append(format_kv("🔥", "下次热点发现", next_hotspot_at.strftime('%Y-%m-%d %H:%M:%S %Z')))
```

注意：需要更新 `status_text` 的函数签名，添加 `next_hotspot_at` 参数。

- [ ] **Step 3: 添加 `hotspot_summary` 函数**

在 `revisit_summary` 函数之后添加：

```python
def hotspot_summary(next_hotspot_at: datetime) -> str:
    from src.hotspot.store import hotspot_stats
    stats = hotspot_stats()
    latest = load_json(LATEST_HOTSPOT_RUN_PATH, {})
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    lines = format_header("🔥 热点发现状态")
    lines.extend([
        format_kv("📊", "今日发现", stats["today_discovered"]),
        format_kv("✅", "今日入队", stats["today_added_to_queue"]),
        format_kv("📚", "历史总计", stats["total_discovered"]),
        format_kv("📅", "今日热点发帖", f"{count_hotspot_posts_today(today)}/{hotspot_daily_limit()}"),
        format_kv("🕒", "下次发现", next_hotspot_at.strftime('%Y-%m-%d %H:%M:%S %Z')),
    ])
    if latest:
        lines.extend([
            "",
            format_kv("🕒", "最近时间", latest.get("time_beijing", "")),
            format_kv("⚙️", "最近触发", latest.get("trigger", "")),
            format_kv("📊", "最近发现", f"{latest.get('discovered', 0)} 条 (✅{latest.get('added', 0)})"),
        ])
    return "\n".join(lines)
```

- [ ] **Step 4: 在 `handle_command` 中添加 `/hotspot_discover` 和 `/hotspot_status` 命令**

在 `/revisit_once` 命令处理之后，`/event` 之前添加：

```python
    if command.startswith("/hotspot_discover"):
        if run_proc and run_proc.poll() is None:
            _safe_notify("⏳ 当前已有任务在执行。")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
        _safe_notify("🔥 热点发现\n\n✅ 已收到 /hotspot_discover，开始执行。")
        return start_job("discover_hotspots.py", "telegram"), next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, "telegram", "discover_hotspots.py"

    if command.startswith("/hotspot_status"):
        _safe_notify(hotspot_summary(next_hotspot_at))
        return run_proc, next_run_at, next_post_run_at, next_learn_at, next_revisit_at, next_hotspot_at, run_trigger, active_label
```

注意：`handle_command` 的签名和所有返回值需要新增 `next_hotspot_at` 参数。

- [ ] **Step 5: 在 `main` 函数中添加热点调度逻辑**

在 `main` 函数中 `now = datetime.now().astimezone()` 之后添加 `next_hotspot_at` 初始化：

```python
    next_hotspot_at = next_hotspot_after(now)
```

在 main 循环的 job 调度区域（learn job 的 elif 之后）添加：

```python
            elif (
                run_proc is None
                and hotspot_enabled()
                and now >= next_hotspot_at
                and (next_run_at - now).total_seconds() > hotspot_guard_seconds()
                and (next_post_run_at - now).total_seconds() > hotspot_guard_seconds()
            ):
                run_proc = start_job("discover_hotspots.py", "schedule")
                run_trigger = "schedule"
                active_label = "discover_hotspots.py"
                next_hotspot_at = next_hotspot_after(now)
```

在 job 完成后的 carry_over 逻辑中添加 `carry_over_hotspot_slot`：

```python
                carry_over_hotspot_slot = (
                    active_label != "discover_hotspots.py"
                    and hotspot_enabled()
                    and next_hotspot_at <= finished_at
                )
```

在 carry_over 变量赋值之后，next_revisit_at 更新之后添加：

```python
                next_hotspot_at = finished_at if carry_over_hotspot_slot else next_hotspot_after(finished_at)
```

- [ ] **Step 6: 更新所有受影响的函数签名和调用**

- `status_text` 添加 `next_hotspot_at` 参数
- `handle_command` 添加 `next_hotspot_at` 参数和返回值
- `poll_updates` 添加 `next_hotspot_at` 参数和返回值
- main 循环中所有调用处添加 `next_hotspot_at` 实参

- [ ] **Step 7: Commit**

```bash
git add bot_daemon.py
git commit -m "feat: add hotspot job scheduling to bot daemon"
```

---

### Task 6: 添加 Telegram 命令注册

**Files:**
- Modify: `sync_tg_commands.py`

- [ ] **Step 1: 在 COMMANDS 列表中添加热点命令**

在 `COMMANDS` 列表末尾（`{"command": "rate", ...}` 之后）添加：

```python
    {"command": "hotspot_discover", "description": "立即发现热点并入队"},
    {"command": "hotspot_status", "description": "查看热点发现状态"},
```

- [ ] **Step 2: Commit**

```bash
git add sync_tg_commands.py
git commit -m "feat: add hotspot Telegram commands"
```

---

### Task 7: 更新 .env.example

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: 在文件末尾添加热点相关环境变量**

```bash
# 热点发现功能
# X_HOTSPOT_ENABLED="1"             # 热点发现开关 (0=禁用, 1=启用)
# X_HOTSPOT_INTERVAL_SECONDS="7200" # 热点发现间隔（秒），默认 2 小时
# X_HOTSPOT_GUARD_SECONDS="600"     # 热点发现保护间隔（秒）
# X_HOTSPOT_DAILY_LIMIT="3"         # 每天最多发几条热点帖
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "feat: add hotspot env vars to .env.example"
```

---

## Self-Review Checklist

1. **Spec coverage:**
   - 热度发现 → Task 2 (discover.py: HN fetch + LLM filter), Task 3 (入口脚本)
   - 跟随热点 → 热点自动进入 post_topics.json 队列，post_once.py 以 news_react 类型发帖
   - 反馈机制 → 复用现有 /rate /review 命令，反馈已注入 prompt (build_feedback_context)
   - 自动跟随 → 全自动，无人工审核步骤；只有 dry-run 模式用于测试

2. **No placeholders:** 所有步骤包含完整代码和命令

3. **Type consistency:**
   - `next_hotspot_at` 贯穿 bot_daemon.py 所有函数签名
   - `discover_hotspots` 返回的 dict 格式与 discover_hotspots.py 消费一致
   - hotspot store 的 id 格式 `{source}:{id}` 在整个模块中一致

---

**Plan complete. Two execution options:**

1. **Subagent-Driven (recommended)** — 每个 task 一个独立 subagent，task 间 review，快速迭代
2. **Inline Execution** — 在当前 session 中逐步执行，批量 commit

**Which approach?**
