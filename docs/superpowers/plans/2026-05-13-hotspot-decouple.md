# Hotspot Decouple Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 hotspot 的"发现"与"发帖"解耦：discover 只写库不入队，post_once 通过新的 `postable_pool` 服务层在发帖时实时挑选最佳热点，带 24h 新鲜度衰减与 LLM 主题去重。

**Architecture:** 新建 `src/postable_pool.py`（人工/热点/auto 优先级调度）+ `src/hotspot/selector.py`（候选打分 + LLM 去重）。`src/hotspot/store.py` 加 `posted_at` 列与 3 个新查询函数。`discover_hotspots.py` 不再写 `post_topics.json`；`post_once.py` / `thread.py` / `article.py` 通过 pool 统一取/标记 topic。`postable_pool` 内置 idempotent lazy 迁移，无需手动脚本。

**Tech Stack:** Python 3.11+ stdlib only（sqlite3、json、fcntl、urllib），pytest，无额外依赖。

**Reference:** 完整设计见 `docs/superpowers/specs/2026-05-13-hotspot-decouple-design.md`。

---

## Task 1: 扩展 `src/hotspot/store.py` — 加 `posted_at` 列与查询函数

**Files:**
- Modify: `src/hotspot/store.py`
- Test: `tests/unit/test_hotspot_store.py`

- [ ] **Step 1.1: 写失败测试 `test_posted_at_column_added_on_open`**

追加到 `tests/unit/test_hotspot_store.py` 末尾：

```python
def test_posted_at_column_added_on_open(tmp_path, monkeypatch):
    """旧库（无 posted_at 列）打开后应自动加列且不破坏数据。"""
    db_path = tmp_path / "legacy_hotspot.db"
    legacy = sqlite3.connect(str(db_path))
    legacy.executescript("""
        CREATE TABLE hotspots (
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
        INSERT INTO hotspots(id, source, relevance_score, discovered_at)
        VALUES ('hn:legacy', 'hn', 4, '2026-05-13 10:00:00 CST');
    """)
    legacy.commit()
    legacy.close()

    monkeypatch.setattr(store, "HOTSPOT_STORE_PATH", db_path)

    # First call triggers schema migration.
    assert store.is_seen("hn", "legacy") is True

    with sqlite3.connect(str(db_path)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(hotspots)")}
        assert "posted_at" in cols
        # Existing row preserved with empty posted_at default.
        row = conn.execute(
            "SELECT posted_at FROM hotspots WHERE id = 'hn:legacy'"
        ).fetchone()
        assert row[0] == ""
```

- [ ] **Step 1.2: 跑测试确认失败**

```bash
pytest tests/unit/test_hotspot_store.py::test_posted_at_column_added_on_open -v
```
Expected: FAIL — `posted_at` 列不存在。

- [ ] **Step 1.3: 在 `src/hotspot/store.py` 加列迁移**

在 `_ensure_schema` 后追加一个新函数，然后改 `_get_conn` 调用它：

```python
def _ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(hotspots)")}
    if "posted_at" not in cols:
        conn.execute("ALTER TABLE hotspots ADD COLUMN posted_at TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hotspots_posted "
        "ON hotspots(posted_at, relevance_score)"
    )
    conn.commit()
```

把现有 `_get_conn` 改成：

```python
@contextmanager
def _get_conn():
    db = HOTSPOT_STORE_PATH
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(str(db), timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        _ensure_columns(conn)
        yield conn
```

- [ ] **Step 1.4: 跑测试确认通过**

```bash
pytest tests/unit/test_hotspot_store.py::test_posted_at_column_added_on_open -v
pytest tests/unit/test_hotspot_store.py -v   # 旧测试不能挂
```
Expected: 全部 PASS。

- [ ] **Step 1.5: 写 `mark_posted` 失败测试**

继续追加：

```python
def test_mark_posted_sets_timestamp(tmp_path, monkeypatch):
    db_path = tmp_path / "hot.db"
    monkeypatch.setattr(store, "HOTSPOT_STORE_PATH", db_path)
    store.insert_hotspot("hn", "42", "title", "url", relevance_score=4)
    assert store.is_seen("hn", "42")

    _freeze(monkeypatch, datetime(2026, 5, 13, 14, 30, 0, tzinfo=BEIJING_TZ))
    store.mark_posted("hn", "42")

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT posted_at FROM hotspots WHERE id='hn:42'").fetchone()
    assert row["posted_at"].startswith("2026-05-13 14:30:00")


def test_mark_posted_missing_row_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "HOTSPOT_STORE_PATH", tmp_path / "hot.db")
    # No insert — should not raise.
    store.mark_posted("hn", "nonexistent")
```

- [ ] **Step 1.6: 跑测试确认失败**

```bash
pytest tests/unit/test_hotspot_store.py::test_mark_posted_sets_timestamp tests/unit/test_hotspot_store.py::test_mark_posted_missing_row_is_silent -v
```
Expected: FAIL — `mark_posted` 不存在。

- [ ] **Step 1.7: 实现 `mark_posted`**

追加到 `src/hotspot/store.py`：

```python
def mark_posted(source: str, hotspot_id: str) -> None:
    with _get_conn() as conn:
        now = _now_beijing().strftime("%Y-%m-%d %H:%M:%S %Z")
        conn.execute(
            "UPDATE hotspots SET posted_at = ? WHERE id = ?",
            (now, f"{source}:{hotspot_id}"),
        )
        conn.commit()
```

- [ ] **Step 1.8: 跑测试确认通过**

```bash
pytest tests/unit/test_hotspot_store.py -v
```
Expected: PASS。

- [ ] **Step 1.9: 写 `unposted_candidates_within` 失败测试**

```python
def test_unposted_candidates_within_filters_correctly(tmp_path, monkeypatch):
    db_path = tmp_path / "hot.db"
    monkeypatch.setattr(store, "HOTSPOT_STORE_PATH", db_path)

    _freeze(monkeypatch, datetime(2026, 5, 13, 12, 0, 0, tzinfo=BEIJING_TZ))

    # Fresh + high score → keep
    store.insert_hotspot("hn", "fresh", "Fresh post", "u1", relevance_score=4)
    # Fresh + low score → drop
    store.insert_hotspot("hn", "low", "Low score", "u2", relevance_score=2)
    # Older than 24h → drop (manually backdate)
    store.insert_hotspot("hn", "old", "Old post", "u3", relevance_score=5)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE hotspots SET discovered_at='2026-05-11 12:00:00 CST' WHERE id='hn:old'"
        )
        conn.commit()
    # Already posted → drop
    store.insert_hotspot("hn", "done", "Already posted", "u4", relevance_score=5)
    store.mark_posted("hn", "done")

    rows = store.unposted_candidates_within(hours=24, min_score=3)
    ids = [r["id"] for r in rows]
    assert ids == ["hn:fresh"]
    assert rows[0]["title"] == "Fresh post"
    assert rows[0]["relevance_score"] == 4
```

- [ ] **Step 1.10: 跑测试确认失败**

```bash
pytest tests/unit/test_hotspot_store.py::test_unposted_candidates_within_filters_correctly -v
```
Expected: FAIL — 函数不存在。

- [ ] **Step 1.11: 实现 `unposted_candidates_within`**

```python
def unposted_candidates_within(hours: int, min_score: int) -> list[dict]:
    """Return rows where:
      - relevance_score >= min_score
      - posted_at IS empty
      - discovered_at is within `hours` from now (Beijing time)
    """
    with _get_conn() as conn:
        cutoff = (_now_beijing() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            """\
SELECT id, source, title, url, hn_score, hn_descendants,
       relevance_score, relevance_reason, angle, cn_summary,
       discovered_at, posted_at
FROM hotspots
WHERE relevance_score >= ?
  AND COALESCE(posted_at, '') = ''
  AND discovered_at >= ?
ORDER BY relevance_score DESC, discovered_at DESC
""",
            (min_score, cutoff),
        ).fetchall()
    return [dict(row) for row in rows]
```

Note: `discovered_at` 在 store 里保存形如 `"2026-05-13 14:30:00 CST"`。SQLite 字符串字典序比较 `YYYY-MM-DD HH:MM:SS` 前缀单调，加 `cutoff` 不带时区后缀就足够。

- [ ] **Step 1.12: 跑测试确认通过**

```bash
pytest tests/unit/test_hotspot_store.py -v
```
Expected: PASS。

- [ ] **Step 1.13: 写 `posted_today_summaries` 失败测试**

```python
def test_posted_today_summaries_returns_only_today(tmp_path, monkeypatch):
    db_path = tmp_path / "hot.db"
    monkeypatch.setattr(store, "HOTSPOT_STORE_PATH", db_path)

    _freeze(monkeypatch, datetime(2026, 5, 13, 9, 0, 0, tzinfo=BEIJING_TZ))
    store.insert_hotspot("hn", "yest", "Yesterday hot", "u1",
                         relevance_score=4, cn_summary="昨日话题")
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE hotspots SET posted_at='2026-05-12 22:00:00 CST' WHERE id='hn:yest'"
        )
        conn.commit()

    _freeze(monkeypatch, datetime(2026, 5, 13, 12, 0, 0, tzinfo=BEIJING_TZ))
    store.insert_hotspot("hn", "today1", "Today hot 1", "u2",
                         relevance_score=4, cn_summary="今天 Claude 更新")
    store.mark_posted("hn", "today1")
    store.insert_hotspot("hn", "today2", "Today hot 2", "u3",
                         relevance_score=5, cn_summary="另一条今日热点")
    store.mark_posted("hn", "today2")

    summaries = store.posted_today_summaries()
    assert sorted(summaries) == sorted(["今天 Claude 更新", "另一条今日热点"])
```

- [ ] **Step 1.14: 跑测试确认失败**

```bash
pytest tests/unit/test_hotspot_store.py::test_posted_today_summaries_returns_only_today -v
```
Expected: FAIL — 函数不存在。

- [ ] **Step 1.15: 实现 `posted_today_summaries`**

```python
def posted_today_summaries() -> list[str]:
    """cn_summary list of hotspots posted today (Beijing date)."""
    with _get_conn() as conn:
        today = _now_beijing().strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT cn_summary FROM hotspots WHERE posted_at LIKE ?",
            (f"{today}%",),
        ).fetchall()
    return [str(r["cn_summary"] or "") for r in rows if str(r["cn_summary"] or "")]
```

- [ ] **Step 1.16: 跑全部 store 测试**

```bash
pytest tests/unit/test_hotspot_store.py -v
```
Expected: 全 PASS。

- [ ] **Step 1.17: Commit**

```bash
git add src/hotspot/store.py tests/unit/test_hotspot_store.py
git commit -m "feat(hotspot): add posted_at column + mark_posted / unposted_candidates_within / posted_today_summaries"
```

---

## Task 2: 新建 `src/hotspot/selector.py` — 本地打分 + LLM 主题去重

**Files:**
- Create: `src/hotspot/selector.py`
- Test: `tests/unit/test_hotspot_selector.py`

- [ ] **Step 2.1: 新建测试文件骨架**

创建 `tests/unit/test_hotspot_selector.py`：

```python
"""Unit tests for src.hotspot.selector.pick_best."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import src.hotspot.selector as selector  # noqa: E402

BEIJING_TZ = timezone(timedelta(hours=8))


def _row(
    *,
    source="hn",
    hotspot_id="1",
    title="t",
    url="u",
    relevance_score=4,
    relevance_reason="r",
    angle="a",
    cn_summary="cn",
    hn_score=10,
    hn_descendants=5,
    age_hours=1.0,
    now=None,
):
    base = now or datetime(2026, 5, 13, 12, 0, 0, tzinfo=BEIJING_TZ)
    discovered = (base - timedelta(hours=age_hours)).strftime("%Y-%m-%d %H:%M:%S %Z")
    return {
        "id": f"{source}:{hotspot_id}",
        "source": source,
        "title": title,
        "url": url,
        "hn_score": hn_score,
        "hn_descendants": hn_descendants,
        "relevance_score": relevance_score,
        "relevance_reason": relevance_reason,
        "angle": angle,
        "cn_summary": cn_summary,
        "discovered_at": discovered,
        "posted_at": "",
    }
```

- [ ] **Step 2.2: 写"候选 0 条 → None"测试**

```python
def test_pick_best_returns_none_when_no_candidates(monkeypatch):
    monkeypatch.setattr(selector.store, "unposted_candidates_within", lambda *a, **k: [])
    monkeypatch.setattr(selector.store, "posted_today_summaries", lambda: [])
    monkeypatch.setattr(selector, "chat_json_result", MagicMock(side_effect=AssertionError("should not call LLM")))
    assert selector.pick_best() is None
```

- [ ] **Step 2.3: 写"单条候选 + LLM 选 0 → 返回该 topic dict"测试**

```python
def test_pick_best_single_candidate_returns_topic_dict(monkeypatch):
    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=BEIJING_TZ)
    row = _row(
        source="hn", hotspot_id="42",
        title="Claude Code 3.0 announced",
        url="https://hn.x/42",
        relevance_score=5,
        relevance_reason="主流 AI 编程工具更新",
        angle="工作流变化",
        cn_summary="Claude Code 3.0 发布",
        hn_score=300, hn_descendants=120,
        age_hours=2.0, now=now,
    )
    monkeypatch.setattr(selector.store, "unposted_candidates_within", lambda *a, **k: [row])
    monkeypatch.setattr(selector.store, "posted_today_summaries", lambda: [])
    monkeypatch.setattr(
        selector, "chat_json_result",
        MagicMock(return_value={"payload": {"best_index": 0, "reason": "唯一候选"},
                                "cost": {"total_cost": 0.001}, "usage": {}})
    )

    topic = selector.pick_best(now=now)
    assert topic is not None
    assert topic["id"] == "hotspot-hn-42"
    assert topic["type"] == "news_react"
    assert topic["text"] == "Claude Code 3.0 发布"
    assert topic["source"] == "hotspot"
    assert topic["status"] == "pending"
    assert topic["subject"] == "Claude Code 3.0 announced"
    assert "今天[hn]" in topic["event_or_context"]
    assert "https://hn.x/42" in topic["event_or_context"]
    assert topic["stance"] == "工作流变化"
    assert "300↑" in topic["evidence_hint"] and "120💬" in topic["evidence_hint"]
    assert "5/5" in topic["evidence_hint"]
    assert topic["_pool"] == "hotspot"
    assert topic["_pool_ref"] == "hn:42"
```

- [ ] **Step 2.4: 写"freshness 排序"测试**

```python
def test_pick_best_orders_by_score_times_freshness(monkeypatch):
    now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=BEIJING_TZ)
    # row_a: score=4 age=1h (weight 1.0) → 4.0
    # row_b: score=5 age=20h (weight ≈ 1 - 0.7*14/18 = 0.456) → ≈ 2.28
    # row_c: score=3 age=3h (weight 1.0) → 3.0
    # Expected top 5 order: a, c, b
    row_a = _row(source="hn", hotspot_id="a", relevance_score=4, age_hours=1.0, now=now,
                 cn_summary="A summary")
    row_b = _row(source="hn", hotspot_id="b", relevance_score=5, age_hours=20.0, now=now,
                 cn_summary="B summary")
    row_c = _row(source="hn", hotspot_id="c", relevance_score=3, age_hours=3.0, now=now,
                 cn_summary="C summary")
    monkeypatch.setattr(selector.store, "unposted_candidates_within",
                        lambda *a, **k: [row_a, row_b, row_c])
    monkeypatch.setattr(selector.store, "posted_today_summaries", lambda: [])
    seen_payload = {}
    def fake_llm(messages, **kwargs):
        seen_payload["user"] = messages[-1]["content"]
        return {"payload": {"best_index": 0, "reason": "ok"},
                "cost": {"total_cost": 0.0}, "usage": {}}
    monkeypatch.setattr(selector, "chat_json_result", fake_llm)

    topic = selector.pick_best(now=now)
    assert topic["_pool_ref"] == "hn:a"  # top of local ranking
    # The LLM saw all three in order [a, c, b]
    payload = seen_payload["user"]
    pos_a = payload.find('"A summary"')
    pos_c = payload.find('"C summary"')
    pos_b = payload.find('"B summary"')
    assert 0 <= pos_a < pos_c < pos_b
```

- [ ] **Step 2.5: 写"LLM 返回 -1 → None"测试**

```python
def test_pick_best_returns_none_when_llm_says_all_dup(monkeypatch):
    row = _row(source="hn", hotspot_id="1", cn_summary="重复主题")
    monkeypatch.setattr(selector.store, "unposted_candidates_within", lambda *a, **k: [row])
    monkeypatch.setattr(selector.store, "posted_today_summaries", lambda: ["重复主题"])
    monkeypatch.setattr(
        selector, "chat_json_result",
        MagicMock(return_value={"payload": {"best_index": -1, "reason": "全部跟已发重复"},
                                "cost": {"total_cost": 0.001}, "usage": {}})
    )
    assert selector.pick_best() is None
```

- [ ] **Step 2.6: 写"LLM 返回越界 → None"测试**

```python
def test_pick_best_returns_none_when_llm_index_out_of_range(monkeypatch):
    row = _row()
    monkeypatch.setattr(selector.store, "unposted_candidates_within", lambda *a, **k: [row])
    monkeypatch.setattr(selector.store, "posted_today_summaries", lambda: [])
    monkeypatch.setattr(
        selector, "chat_json_result",
        MagicMock(return_value={"payload": {"best_index": 99, "reason": "x"},
                                "cost": {}, "usage": {}})
    )
    assert selector.pick_best() is None
```

- [ ] **Step 2.7: 写"LLM 异常 → None（不退化）"测试**

```python
def test_pick_best_returns_none_when_llm_raises(monkeypatch):
    row = _row()
    monkeypatch.setattr(selector.store, "unposted_candidates_within", lambda *a, **k: [row])
    monkeypatch.setattr(selector.store, "posted_today_summaries", lambda: [])
    monkeypatch.setattr(
        selector, "chat_json_result",
        MagicMock(side_effect=RuntimeError("LLM down"))
    )
    assert selector.pick_best() is None
```

- [ ] **Step 2.8: 跑所有 selector 测试确认失败**

```bash
pytest tests/unit/test_hotspot_selector.py -v
```
Expected: 全部 FAIL（模块不存在）。

- [ ] **Step 2.9: 实现 `src/hotspot/selector.py`**

```python
#!/usr/bin/env python3
"""Pick the most-postable hotspot row from the store.

Local scoring = relevance_score × freshness_weight(age_hours).
Then one LLM call decides among top 5 vs. today's already-posted summaries.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from src.common import chat_json_result
from src.hotspot import store
from src.logger import get_logger

logger = get_logger(__name__)

BEIJING_TZ = ZoneInfo("Asia/Shanghai")

CANDIDATE_HOURS = 24
MIN_SCORE = 3
TOP_N_FOR_LLM = 5

SELECTOR_PROMPT = """\
你在为一个中文 X 账号挑选今天最该发的一条热点。

输入会给你 N 个候选热点（按本地新鲜度+相关度初排），以及今天已经发过的热点摘要列表。

输出严格 JSON：
{"best_index": 整数, "reason": "..."}

规则：
- best_index = 0..N-1，表示选中候选数组的第几个；
- 若所有候选的主题都跟"已发列表"中某条本质上是同一件事（同公司同事件同产品），
  返回 best_index = -1；
- 若多个候选都不跟已发重复，挑发帖价值更高的一个（更新更猛 / 切入角度更独特）；
- reason 用中文，30 字内。
- 只输出 JSON，不要 markdown 包裹。
"""


def _parse_discovered(raw: str) -> Optional[datetime]:
    """Parse 'YYYY-MM-DD HH:MM:SS CST' → aware datetime in Beijing tz."""
    if not raw:
        return None
    try:
        head = raw.rsplit(" ", 1)[0]  # strip trailing tz label
        return datetime.strptime(head, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TZ)
    except ValueError:
        return None


def _freshness_weight(age_hours: float) -> float:
    if age_hours <= 6:
        return 1.0
    if age_hours >= 24:
        return 0.0
    return 1.0 - 0.7 * (age_hours - 6) / 18.0


def _local_score(row: dict, now: datetime) -> float:
    disc = _parse_discovered(row.get("discovered_at", ""))
    if disc is None:
        age = 0.0
    else:
        age = max(0.0, (now - disc).total_seconds() / 3600.0)
    return float(row.get("relevance_score") or 0) * _freshness_weight(age)


def _row_to_topic(row: dict) -> dict:
    source = str(row.get("source") or "")
    raw_id = row["id"].split(":", 1)[1] if ":" in str(row.get("id") or "") else ""
    return {
        "id": f"hotspot-{source}-{raw_id}",
        "type": "news_react",
        "text": str(row.get("cn_summary") or ""),
        "source": "hotspot",
        "status": "pending",
        "subject": str(row.get("title") or ""),
        "event_or_context": (
            f"今天[{source}] {row.get('relevance_reason') or ''} "
            f"| 原链接: {row.get('url') or ''}"
        ),
        "stance": str(row.get("angle") or ""),
        "evidence_hint": (
            f"热度: {row.get('hn_score') or 0}↑ "
            f"{row.get('hn_descendants') or 0}💬 "
            f"| 相关度: {row.get('relevance_score') or 0}/5"
        ),
        "_pool": "hotspot",
        "_pool_ref": str(row.get("id") or ""),
    }


def pick_best(now: datetime | None = None) -> dict | None:
    when = now or datetime.now(tz=BEIJING_TZ)

    try:
        candidates = store.unposted_candidates_within(hours=CANDIDATE_HOURS, min_score=MIN_SCORE)
    except Exception as exc:
        logger.warning("selector: store query failed: %s", exc)
        return None

    if not candidates:
        logger.info("selector: no candidates")
        return None

    ranked = sorted(
        candidates,
        key=lambda r: (-_local_score(r, when), -int(r.get("relevance_score") or 0),
                       str(r.get("id") or "")),
    )[:TOP_N_FOR_LLM]

    try:
        posted = store.posted_today_summaries()
    except Exception as exc:
        logger.warning("selector: posted_today_summaries failed: %s", exc)
        posted = []

    llm_input = {
        "candidates": [
            {
                "index": i,
                "title": r.get("title") or "",
                "cn_summary": r.get("cn_summary") or "",
                "source": r.get("source") or "",
                "relevance_score": r.get("relevance_score") or 0,
                "angle": r.get("angle") or "",
            }
            for i, r in enumerate(ranked)
        ],
        "already_posted_today": posted,
    }

    try:
        response = chat_json_result(
            [
                {"role": "system", "content": SELECTOR_PROMPT},
                {"role": "user", "content": json.dumps(llm_input, ensure_ascii=False)},
            ],
            temperature=0.2,
            max_tokens=200,
        )
    except Exception as exc:
        logger.warning("selector: LLM call failed: %s", exc)
        return None

    payload = response.get("payload") or {}
    try:
        idx = int(payload.get("best_index"))
    except (TypeError, ValueError):
        logger.warning("selector: LLM returned non-int best_index: %r", payload)
        return None

    if idx == -1:
        logger.info("selector: LLM says all candidates duplicate today's posts (reason=%s)",
                    payload.get("reason"))
        return None
    if idx < 0 or idx >= len(ranked):
        logger.warning("selector: LLM returned out-of-range index %d (n=%d)", idx, len(ranked))
        return None

    selected = ranked[idx]
    logger.info("selector: picked %s (score=%d, reason=%s)",
                selected.get("id"), selected.get("relevance_score"), payload.get("reason"))
    return _row_to_topic(selected)
```

- [ ] **Step 2.10: 跑所有 selector 测试**

```bash
pytest tests/unit/test_hotspot_selector.py -v
```
Expected: 全 PASS。

- [ ] **Step 2.11: Commit**

```bash
git add src/hotspot/selector.py tests/unit/test_hotspot_selector.py
git commit -m "feat(hotspot): add selector with freshness decay + LLM topic dedup"
```

---

## Task 3: 新建 `src/postable_pool.py` — 调度层

**Files:**
- Create: `src/postable_pool.py`
- Test: `tests/unit/test_postable_pool.py`

- [ ] **Step 3.1: 新建测试文件骨架**

```python
"""Unit tests for src.postable_pool."""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def pool(tmp_state, monkeypatch):
    """Fresh postable_pool module with state isolated to tmp_state."""
    import src.postable_pool as mod
    importlib.reload(mod)
    return mod
```

(`tmp_state` 已在 `tests/conftest.py` 提供。)

- [ ] **Step 3.2: 写"manual 命中"测试**

```python
def test_next_topic_to_post_returns_manual_first(pool, monkeypatch):
    fake_topic = {"id": "m1", "source": "manual", "status": "pending", "text": "hi"}
    monkeypatch.setattr(pool.topics, "next_pending_post_topic", lambda: fake_topic)
    monkeypatch.setattr(
        pool.selector, "pick_best",
        MagicMock(side_effect=AssertionError("should not call selector when manual present"))
    )

    topic = pool.next_topic_to_post()
    assert topic["id"] == "m1"
    assert topic["_pool"] == "manual"
    assert topic.get("_pool_ref") == ""
```

- [ ] **Step 3.3: 写"manual 为空 → hotspot 命中"测试**

```python
def test_next_topic_to_post_falls_back_to_hotspot(pool, monkeypatch):
    monkeypatch.setattr(pool.topics, "next_pending_post_topic", lambda: None)
    hotspot_topic = {
        "id": "hotspot-hn-9", "type": "news_react", "source": "hotspot",
        "status": "pending", "text": "x", "_pool": "hotspot", "_pool_ref": "hn:9",
    }
    monkeypatch.setattr(pool.selector, "pick_best", lambda: hotspot_topic)

    topic = pool.next_topic_to_post()
    assert topic["_pool"] == "hotspot"
    assert topic["_pool_ref"] == "hn:9"
```

- [ ] **Step 3.4: 写"全空 → None"测试**

```python
def test_next_topic_to_post_returns_none_when_all_empty(pool, monkeypatch):
    monkeypatch.setattr(pool.topics, "next_pending_post_topic", lambda: None)
    monkeypatch.setattr(pool.selector, "pick_best", lambda: None)
    assert pool.next_topic_to_post() is None
```

- [ ] **Step 3.5: 写"mark_topic_used 派发到 topics"测试**

```python
def test_mark_topic_used_manual_dispatches_to_topics(pool, monkeypatch):
    called = {}
    def fake_mark(topic_id, status, extra):
        called["args"] = (topic_id, status, extra)
    monkeypatch.setattr(pool.topics, "mark_post_topic_status", fake_mark)
    spy = MagicMock()
    monkeypatch.setattr(pool.hotspot_store, "mark_posted", spy)

    topic = {"id": "m1", "_pool": "manual", "_pool_ref": ""}
    pool.mark_topic_used(topic, status="used", extra={"used_at": "now"})

    assert called["args"] == ("m1", "used", {"used_at": "now"})
    spy.assert_not_called()
```

- [ ] **Step 3.6: 写"mark_topic_used 派发到 hotspot store"测试**

```python
def test_mark_topic_used_hotspot_dispatches_to_store(pool, monkeypatch):
    spy_store = MagicMock()
    monkeypatch.setattr(pool.hotspot_store, "mark_posted", spy_store)
    spy_topics = MagicMock()
    monkeypatch.setattr(pool.topics, "mark_post_topic_status", spy_topics)

    topic = {"id": "hotspot-hn-9", "_pool": "hotspot", "_pool_ref": "hn:9"}
    pool.mark_topic_used(topic, status="used")

    spy_store.assert_called_once_with("hn", "9")
    spy_topics.assert_not_called()
```

- [ ] **Step 3.7: 写"hotspot 非 used 状态被忽略"测试**

```python
def test_mark_topic_used_hotspot_ignores_non_used_status(pool, monkeypatch):
    spy_store = MagicMock()
    monkeypatch.setattr(pool.hotspot_store, "mark_posted", spy_store)
    topic = {"_pool": "hotspot", "_pool_ref": "hn:9"}
    pool.mark_topic_used(topic, status="failed")
    spy_store.assert_not_called()
```

- [ ] **Step 3.8: 写"_pool 缺失 no-op"测试**

```python
def test_mark_topic_used_missing_pool_field_warns_and_noops(pool, monkeypatch, caplog):
    spy_store = MagicMock()
    spy_topics = MagicMock()
    monkeypatch.setattr(pool.hotspot_store, "mark_posted", spy_store)
    monkeypatch.setattr(pool.topics, "mark_post_topic_status", spy_topics)

    pool.mark_topic_used({"id": "orphan"}, status="used")
    spy_store.assert_not_called()
    spy_topics.assert_not_called()
```

- [ ] **Step 3.9: 写"legacy migration 一次性 + 幂等"测试**

```python
def test_legacy_migration_marks_pending_hotspot_topics_skipped(pool, monkeypatch):
    # Seed post_topics.json with legacy pending hotspot rows.
    from src.common import POST_TOPICS_PATH
    POST_TOPICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    initial = {
        "topics": [
            {"id": "manual1", "source": "manual", "status": "pending", "text": "m"},
            {"id": "hotspot-old1", "source": "hotspot", "status": "pending", "text": "h1"},
            {"id": "hotspot-old2", "source": "hotspot", "status": "pending", "text": "h2"},
            {"id": "hotspot-used", "source": "hotspot", "status": "used", "text": "u"},
        ]
    }
    POST_TOPICS_PATH.write_text(json.dumps(initial), encoding="utf-8")

    monkeypatch.setattr(pool.topics, "next_pending_post_topic", lambda: None)
    monkeypatch.setattr(pool.selector, "pick_best", lambda: None)

    pool.next_topic_to_post()  # Triggers migration.

    after = json.loads(POST_TOPICS_PATH.read_text(encoding="utf-8"))
    by_id = {t["id"]: t for t in after["topics"]}
    assert by_id["manual1"]["status"] == "pending"
    assert by_id["hotspot-old1"]["status"] == "skipped"
    assert by_id["hotspot-old1"]["skip_reason"] == "migrated_to_db_pool"
    assert "migrated_at" in by_id["hotspot-old1"]
    assert by_id["hotspot-old2"]["status"] == "skipped"
    assert by_id["hotspot-used"]["status"] == "used"

    # Second call should not modify (idempotent + process-cached).
    mtime_before = POST_TOPICS_PATH.stat().st_mtime
    pool.next_topic_to_post()
    assert POST_TOPICS_PATH.stat().st_mtime == mtime_before
```

- [ ] **Step 3.10: 写 `pool_status` 测试**

```python
def test_pool_status_aggregates_both_stores(pool, monkeypatch):
    monkeypatch.setattr(pool.topics, "post_topic_summary",
                        lambda: {"pending": 2, "used": 5, "skipped": 1, "total": 8})
    monkeypatch.setattr(pool.hotspot_store, "unposted_candidates_within",
                        lambda *a, **k: [{"id": "hn:1"}, {"id": "hn:2"}, {"id": "hn:3"}])
    monkeypatch.setattr(pool.hotspot_store, "hotspot_stats",
                        lambda: {"total_discovered": 100, "total_added_to_queue": 0,
                                 "today_discovered": 25, "today_added_to_queue": 0})
    monkeypatch.setattr(pool.hotspot_store, "posted_today_summaries",
                        lambda: ["a", "b"])

    status = pool.pool_status()
    assert status["manual"]["pending"] == 2
    assert status["hotspot"]["pool_size_24h"] == 3
    assert status["hotspot"]["discovered_today"] == 25
    assert status["hotspot"]["posted_today"] == 2
```

- [ ] **Step 3.11: 跑所有 postable_pool 测试确认失败**

```bash
pytest tests/unit/test_postable_pool.py -v
```
Expected: 全 FAIL（模块不存在）。

- [ ] **Step 3.12: 实现 `src/postable_pool.py`**

```python
#!/usr/bin/env python3
"""Unified service layer for selecting and marking post topics.

Priority chain in next_topic_to_post():
    1. topics.next_pending_post_topic()  # manual / Telegram queue
    2. hotspot.selector.pick_best()      # hotspot pool with LLM dedup
    3. None                              # caller falls back to auto

Also handles one-time idempotent migration of legacy `source=hotspot`
pending rows in post_topics.json — they get `status=skipped` so the
old queueing semantics don't leak into the new pipeline.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.common import POST_TOPICS_LOCK_PATH, blocking_lock
from src.hotspot import selector
from src.hotspot import store as hotspot_store
from src.logger import get_logger
from src import topics

logger = get_logger(__name__)

BEIJING_TZ = ZoneInfo("Asia/Shanghai")

_migration_done = False


def _now_beijing_str() -> str:
    return datetime.now(tz=BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _migrate_legacy_hotspot_topics_once() -> None:
    global _migration_done
    if _migration_done:
        return
    try:
        with blocking_lock(POST_TOPICS_LOCK_PATH):
            data = topics.load_post_topics()
            changed = 0
            for t in data.get("topics", []):
                if t.get("source") == "hotspot" and (t.get("status") or "pending") == "pending":
                    t["status"] = "skipped"
                    t["skip_reason"] = "migrated_to_db_pool"
                    t["migrated_at"] = _now_beijing_str()
                    changed += 1
            if changed:
                topics.save_post_topics(data)
                logger.info("postable_pool: migrated %d legacy hotspot topics", changed)
    except Exception as exc:
        logger.warning("postable_pool: legacy migration failed: %s", exc)
    _migration_done = True


def next_topic_to_post() -> dict | None:
    _migrate_legacy_hotspot_topics_once()

    try:
        manual = topics.next_pending_post_topic()
    except Exception as exc:
        logger.warning("postable_pool: topics.next_pending failed: %s", exc)
        manual = None

    if manual:
        manual["_pool"] = "manual"
        manual["_pool_ref"] = ""
        return manual

    try:
        hotspot = selector.pick_best()
    except Exception as exc:
        logger.warning("postable_pool: selector.pick_best failed: %s", exc)
        hotspot = None

    return hotspot  # already has _pool/_pool_ref or is None


def mark_topic_used(topic: dict, status: str = "used", extra: dict | None = None) -> None:
    pool = str(topic.get("_pool") or "")
    if pool == "manual":
        try:
            topics.mark_post_topic_status(str(topic.get("id") or ""), status, extra)
        except Exception as exc:
            logger.error("postable_pool: mark manual failed for %s: %s", topic.get("id"), exc)
        return

    if pool == "hotspot":
        if status != "used":
            return  # hotspot store only records successful consumption.
        ref = str(topic.get("_pool_ref") or "")
        if ":" not in ref:
            logger.warning("postable_pool: bad _pool_ref %r for hotspot topic %s", ref, topic.get("id"))
            return
        source, hotspot_id = ref.split(":", 1)
        try:
            hotspot_store.mark_posted(source, hotspot_id)
        except Exception as exc:
            logger.error("postable_pool: mark hotspot %s failed: %s", ref, exc)
        return

    logger.warning("postable_pool: mark_topic_used with missing _pool on %r", topic.get("id"))


def pool_status() -> dict:
    _migrate_legacy_hotspot_topics_once()
    manual = topics.post_topic_summary()
    try:
        unposted = hotspot_store.unposted_candidates_within(hours=24, min_score=3)
        pool_size = len(unposted)
    except Exception as exc:
        logger.warning("postable_pool: pool_size query failed: %s", exc)
        pool_size = 0
    try:
        stats = hotspot_store.hotspot_stats()
        discovered_today = int(stats.get("today_discovered") or 0)
    except Exception as exc:
        logger.warning("postable_pool: hotspot_stats failed: %s", exc)
        discovered_today = 0
    try:
        posted_today = len(hotspot_store.posted_today_summaries())
    except Exception as exc:
        logger.warning("postable_pool: posted_today failed: %s", exc)
        posted_today = 0
    return {
        "manual": manual,
        "hotspot": {
            "pool_size_24h": pool_size,
            "discovered_today": discovered_today,
            "posted_today": posted_today,
        },
    }
```

- [ ] **Step 3.13: 跑所有 postable_pool 测试**

```bash
pytest tests/unit/test_postable_pool.py -v
```
Expected: 全 PASS。

- [ ] **Step 3.14: Commit**

```bash
git add src/postable_pool.py tests/unit/test_postable_pool.py
git commit -m "feat: add postable_pool service layer with priority chain + lazy legacy migration"
```

---

## Task 4: 修 `discover_hotspots.py` — 停止写入 `post_topics.json`

**Files:**
- Modify: `discover_hotspots.py:40-52, 134-160, 172` (删/改 `_queue_daily_hotspot_topics` 调用)
- Modify: `tests/unit/test_discover_hotspots_entrypoint.py:1-27`

- [ ] **Step 4.1: 改测试 — 删旧的 `_queue_daily_hotspot_topics` 测试，加新断言**

把 `tests/unit/test_discover_hotspots_entrypoint.py` 的 `test_queue_daily_hotspot_topics_replaces_stale_pending_hotspots_first` 整个删掉（包括顶部 `_queue_daily_hotspot_topics` 的 import），然后追加：

```python
def test_main_does_not_touch_post_topics_json(tmp_state, monkeypatch):
    """discover entrypoint must not read or write post_topics.json anymore."""
    import discover_hotspots as entry

    monkeypatch.setattr(entry, "telegram_enabled", lambda: False)
    monkeypatch.setattr(
        entry,
        "discover_hotspots",
        lambda: {
            "ok": True, "discovered": 5, "added": 2, "skipped_seen": 0,
            "filtered_out": 3, "source_stats": {}, "source_durations": {},
            "items": [
                {"source": "hn", "id": "1", "title": "t1", "url": "u1",
                 "hn_score": 10, "hn_descendants": 2, "rank_score": 1.0,
                 "relevance_score": 4, "relevance_reason": "r",
                 "angle": "a", "cn_summary": "s"},
            ],
            "filtered_items": [],
            "total_cost_cny": 0.0,
        },
    )

    sentinel_load = MagicMock(side_effect=AssertionError("load_post_topics must not be called"))
    sentinel_save = MagicMock(side_effect=AssertionError("save_post_topics must not be called"))
    sentinel_mark = MagicMock(side_effect=AssertionError("mark_added_to_queue must not be called"))
    monkeypatch.setattr(entry, "load_post_topics", sentinel_load, raising=False)
    monkeypatch.setattr(entry, "save_post_topics", sentinel_save, raising=False)
    monkeypatch.setattr(entry, "mark_added_to_queue", sentinel_mark, raising=False)

    rc = entry.main()
    assert rc == 0
```

记得在文件顶部加 `from unittest.mock import MagicMock`。

- [ ] **Step 4.2: 跑测试确认失败**

```bash
pytest tests/unit/test_discover_hotspots_entrypoint.py -v
```
Expected: `test_main_does_not_touch_post_topics_json` FAIL（assertion 触发或还在调）。

- [ ] **Step 4.3: 改 `discover_hotspots.py` — 删队列写入**

打开 `discover_hotspots.py`，做以下编辑：

(a) 删 `_queue_daily_hotspot_topics` 函数（约 40-52 行）。

(b) `main()` 内 134-160 区块（`items` 处理那段）替换为：

```python
        items = result.get("items", [])
        added_items: list[dict] = []
        if items and not args.dry_run:
            for item in items:
                added_items.append({
                    "source": item["source"],
                    "title": item["title"],
                    "cn_summary": item["cn_summary"],
                    "angle": item["angle"],
                    "relevance_score": item["relevance_score"],
                })
```

(c) 顶部 imports 里删掉这几个不再用的名字：

```python
load_post_topics,
normalize_post_topic,
save_post_topics,
```

以及：

```python
from src.hotspot.store import mark_added_to_queue
```

最终顶部 `from src.common import (...)` 保留 `HOTSPOT_HISTORY_DIR`、`HOTSPOT_LOCK_PATH`、`LATEST_HOTSPOT_RUN_PATH`、`ensure_state_dirs`、`load_env_file`、`telegram_enabled`、`telegram_notify`、`write_json`。

- [ ] **Step 4.4: 跑 entrypoint 测试**

```bash
pytest tests/unit/test_discover_hotspots_entrypoint.py -v
```
Expected: 两条都 PASS。

- [ ] **Step 4.5: 跑 hotspot discover 单测确认没有连带挂掉**

```bash
pytest tests/unit/test_hotspot_discover.py tests/unit/test_hotspot_store.py tests/unit/test_hotspot_selector.py tests/unit/test_postable_pool.py tests/unit/test_discover_hotspots_entrypoint.py -v
```
Expected: 全 PASS。

- [ ] **Step 4.6: Commit**

```bash
git add discover_hotspots.py tests/unit/test_discover_hotspots_entrypoint.py
git commit -m "refactor(discover): stop writing to post_topics.json — hotspot pool now drives post selection"
```

---

## Task 5: 让 `post_once.py` / `thread.py` / `article.py` 改走 postable_pool

**Files:**
- Modify: `post_once.py:11-21, 100, 169-174, 227-233`
- Modify: `src/post/thread.py:13-22, 215-225`
- Modify: `src/post/article.py:12-21, 170-180`

> 关键不变量：
> - `post_once.py` 不再 `from src.common import mark_post_topic_status, next_pending_post_topic`。
> - `thread.py` / `article.py` 内的 `mark_post_topic_status` 调用全部走 `postable_pool.mark_topic_used`。
> - **判断条件从 `topic.get("source") != "auto"` 改为 `topic.get("_pool") in ("manual", "hotspot")`** — auto-generated topic 没有 `_pool` 字段，所以自然跳过。

- [ ] **Step 5.1: 改 `post_once.py` 的 imports**

把 `post_once.py` 顶部 import block 改为：

```python
from src.common import (
    LATEST_POST_RUN_PATH,
    POST_LOCK_PATH,
    ensure_state_dirs,
    load_env_file,
    parse_json_object,
    post_history_path_for,
    write_json,
)
from src import postable_pool
```

（删 `mark_post_topic_status`、`next_pending_post_topic`。）

- [ ] **Step 5.2: 改 `post_once.py:100` 取 topic 的调用**

把：

```python
        topic = next_pending_post_topic()
```

改为：

```python
        topic = postable_pool.next_topic_to_post()
```

- [ ] **Step 5.3: 改 `post_once.py:169-174` 的 skipped 分支 mark**

把：

```python
            if not args.dry_run and topic.get("source") != "auto":
                mark_post_topic_status(
                    str(topic.get("id") or ""),
                    "skipped",
                    topic_extra_update(record["status"], record["time_beijing"], dry_run=False),
                )
```

改为：

```python
            if not args.dry_run and topic.get("_pool") in ("manual", "hotspot"):
                postable_pool.mark_topic_used(
                    topic,
                    status="skipped",
                    extra=topic_extra_update(record["status"], record["time_beijing"], dry_run=False),
                )
```

- [ ] **Step 5.4: 改 `post_once.py:227-233` 的 used 分支 mark**

把：

```python
        if send.returncode == 0:
            if topic.get("source") != "auto":
                mark_post_topic_status(
                    str(topic.get("id") or ""),
                    "used",
                    topic_extra_update(record["status"], record["time_beijing"], dry_run=False),
                )
            add_recent_post(record["post_text"], str(topic.get("type", "")))
```

改为：

```python
        if send.returncode == 0:
            if topic.get("_pool") in ("manual", "hotspot"):
                postable_pool.mark_topic_used(
                    topic,
                    status="used",
                    extra=topic_extra_update(record["status"], record["time_beijing"], dry_run=False),
                )
            add_recent_post(record["post_text"], str(topic.get("type", "")))
```

- [ ] **Step 5.5: 在写 history 前 strip 内部字段**

`post_once.py` 在 130 行附近构造 `record` 时，`record["topic_source"] = topic.get("source", "")` 这行不变。但 `record` 不应包含 `_pool`/`_pool_ref`。当前 record 是用单独 key 构造的（不是 `**topic`），所以**没有自动泄漏**。

但 `add_recent_post` 之类的下游不读这俩字段，也安全。

无需额外改动；加一条注释（**可选**）：

```python
# topic may have _pool/_pool_ref injected by postable_pool — they are
# never written to history because we copy fields explicitly above.
```

- [ ] **Step 5.6: 改 `src/post/thread.py` 的 imports + mark**

`src/post/thread.py:13-22` 把 `mark_post_topic_status` 从 `from src.common import (...)` 里删掉，加：

```python
from src import postable_pool
```

`src/post/thread.py:215-225` 区块替换为：

```python
    if all_ok:
        # Mark topic used even when URL is unresolved — the post landed,
        # so we must NOT leave the topic pending or a follow-up run will
        # re-post the same content.
        if topic.get("_pool") in ("manual", "hotspot"):
            postable_pool.mark_topic_used(
                topic,
                status="used",
                extra=topic_extra_update(record["status"], record["time_beijing"], dry_run=False),
            )
        add_recent_post(segments[0]["text"], "thread")
```

- [ ] **Step 5.7: 改 `src/post/article.py` 的 imports + mark**

类似 5.6，导入改 `from src import postable_pool`，删 `mark_post_topic_status`。

`src/post/article.py:170-180` 区块（搜 `mark_post_topic_status(`）替换为：

```python
        if topic.get("_pool") in ("manual", "hotspot"):
            postable_pool.mark_topic_used(
                topic,
                status="used",
                extra=topic_extra_update(record["status"], record["time_beijing"], dry_run=False),
            )
```

- [ ] **Step 5.8: 跑全部受影响测试**

```bash
pytest tests/unit -v
```
Expected: 全 PASS（注意有些测试可能用过 `next_pending_post_topic` 老接口，需要修；如果挂了，记录每一条挂的 test 路径并逐个修）。

如有遗漏的旧测试用到 `next_pending_post_topic` / `mark_post_topic_status`：保持原测试不动，只修测试中 mock 的目标路径（例如 `monkeypatch.setattr("post_once.next_pending_post_topic", ...)` → `monkeypatch.setattr("post_once.postable_pool.next_topic_to_post", ...)`）。

- [ ] **Step 5.9: Commit**

```bash
git add post_once.py src/post/thread.py src/post/article.py
git commit -m "refactor(post): route topic selection and marking through postable_pool"
```

---

## Task 6: 让 `bot_daemon.py` / `src/reporters.py` 用 `pool_status`

**Files:**
- Modify: `bot_daemon.py:18, 194`
- Modify: `src/reporters.py:25, 127-141`
- Test: `tests/unit/test_reporters_post_summary.py`（新建）

- [ ] **Step 6.1: 新建 reporter 单测**

```python
"""Sanity test for reporters.post_summary using pool_status."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def test_post_summary_shows_manual_and_hotspot_pool(tmp_state, monkeypatch):
    import src.reporters as reporters
    monkeypatch.setattr(
        reporters.postable_pool, "pool_status",
        lambda: {
            "manual": {"pending": 2, "used": 5, "skipped": 1, "total": 8},
            "hotspot": {"pool_size_24h": 7, "discovered_today": 25, "posted_today": 1},
        },
    )
    monkeypatch.setattr(reporters, "count_scheduled_posts", lambda day: 1)

    out = reporters.post_summary(datetime(2026, 5, 13, 19, 0, 0, tzinfo=timezone(timedelta(hours=8))))
    assert "人工待发" in out and "2" in out
    assert "热点池" in out and "7" in out
    assert "今日定时已发" in out
```

- [ ] **Step 6.2: 跑测试确认失败**

```bash
pytest tests/unit/test_reporters_post_summary.py -v
```
Expected: FAIL（`postable_pool` 还没引到 reporters）。

- [ ] **Step 6.3: 改 `src/reporters.py`**

(a) 替换顶部 `post_topic_summary` 的 import：把 `post_topic_summary,` 从 `from src.common import (...)` 里删掉。文件其他位置加：

```python
from src import postable_pool
```

(b) `src/reporters.py:127-141`（`post_summary` 函数体的 `queue` 部分）替换为：

```python
def post_summary(next_post_run_at: datetime) -> str:
    latest = load_json(LATEST_POST_RUN_PATH, {})
    pool = postable_pool.pool_status()
    manual = pool["manual"]
    hotspot = pool["hotspot"]
    today = _beijing_now().strftime("%Y-%m-%d")
    lines = format_header("📝 主动发帖状态")
    lines.extend(
        [
            format_kv("📥", "人工待发", manual["pending"]),
            format_kv("✅", "人工已用", manual["used"]),
            format_kv("⏭️", "人工跳过", manual["skipped"]),
            format_kv("🔥", "热点池(24h)", hotspot["pool_size_24h"]),
            format_kv("🌱", "今日新发现热点", hotspot["discovered_today"]),
            format_kv("📤", "今日已发热点", hotspot["posted_today"]),
            format_kv("📅", "今日定时已发", f"{count_scheduled_posts(today)}/{post_daily_limit()}"),
            format_kv("🕒", "下次主动发帖", next_post_run_at.strftime('%Y-%m-%d %H:%M:%S %Z')),
        ]
    )
    if latest:
```

(剩下的 `if latest:` 部分保持原状。)

- [ ] **Step 6.4: 改 `bot_daemon.py:194`**

`bot_daemon.py:18` 里删除 `post_topic_summary,` 的 import。`bot_daemon.py:194` 把 `queue = post_topic_summary()` 这行整行删掉（这是个未使用的局部变量，验证过 grep 没别处引用）。

- [ ] **Step 6.5: 跑测试**

```bash
pytest tests/unit -v
```
Expected: 全 PASS。

- [ ] **Step 6.6: Commit**

```bash
git add src/reporters.py bot_daemon.py tests/unit/test_reporters_post_summary.py
git commit -m "feat(status): /status now shows manual queue + hotspot pool + today's posted via postable_pool"
```

---

## Task 7: 集成测试 — `post_once` 与 hotspot 池的端到端路径

**Files:**
- Create: `tests/integration/test_post_once_postable_pool.py`

> 这些测试不真正跑浏览器；mock `subprocess.run` / `chat_*` / `add_recent_post` / `notify_telegram`，只验证选择 / 标记 / 持久化的串联。

- [ ] **Step 7.1: 新建集成测试文件**

```python
"""Integration: post_once.main() picks from postable_pool and marks correctly."""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

BEIJING_TZ = timezone(timedelta(hours=8))


def _stub_run_success(monkeypatch):
    """Stub post_send subprocess to a success result."""
    from src.post import handlers_common
    fake_run = MagicMock(return_value=types.SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"url": "https://x.com/me/status/12345"}) + "\n",
        stderr="",
    ))
    monkeypatch.setattr(handlers_common, "run", fake_run)
    return fake_run


def _stub_post_plan(monkeypatch):
    """Stub generate_post_plan so it returns a deterministic plan."""
    from src.post import post_generate
    monkeypatch.setattr(post_generate, "generate_post_plan",
                        lambda topic: {
                            "candidates": [{"text": "hello", "image_query": ""}],
                            "selected_candidate": {"text": "hello", "image_query": ""},
                            "best_candidate": {"text": "hello", "image_query": ""},
                            "total_cost_cny": 0.0,
                        })


def _hotspot_row(tmp_state, **kw):
    """Seed a hotspot row directly into the SQLite store."""
    from src.hotspot import store
    store.insert_hotspot(
        source=kw.get("source", "hn"),
        hotspot_id=kw.get("hotspot_id", "1"),
        title=kw.get("title", "Some hot story"),
        url=kw.get("url", "https://hn.x/1"),
        hn_score=kw.get("hn_score", 100),
        hn_descendants=kw.get("hn_descendants", 50),
        relevance_score=kw.get("relevance_score", 4),
        relevance_reason=kw.get("relevance_reason", "AI agent 重大更新"),
        angle=kw.get("angle", "工作流变化"),
        cn_summary=kw.get("cn_summary", "AI agent 框架更新"),
    )


def test_hotspot_path_marks_posted_on_success(tmp_state, monkeypatch):
    _hotspot_row(tmp_state)
    _stub_post_plan(monkeypatch)
    _stub_run_success(monkeypatch)
    # Selector LLM picks index 0.
    from src.hotspot import selector
    monkeypatch.setattr(selector, "chat_json_result", lambda *a, **k: {
        "payload": {"best_index": 0, "reason": "ok"},
        "cost": {"total_cost": 0.0}, "usage": {},
    })
    # Silence telegram + recent-post side effects.
    from src.post import handlers_common
    monkeypatch.setattr(handlers_common, "notify_telegram", lambda *a, **k: None)
    monkeypatch.setattr("src.persona_store.add_recent_post", lambda *a, **k: None)

    import post_once
    monkeypatch.setattr(sys, "argv", ["post_once.py", "--trigger", "test"])

    rc = post_once.main()
    assert rc == 0

    from src.hotspot import store
    with __import__("sqlite3").connect(str(store.HOTSPOT_STORE_PATH)) as conn:
        row = conn.execute("SELECT posted_at FROM hotspots WHERE id='hn:1'").fetchone()
    assert row[0] != ""


def test_dry_run_does_not_mark_posted(tmp_state, monkeypatch):
    _hotspot_row(tmp_state)
    _stub_post_plan(monkeypatch)
    from src.hotspot import selector
    monkeypatch.setattr(selector, "chat_json_result", lambda *a, **k: {
        "payload": {"best_index": 0, "reason": "ok"},
        "cost": {"total_cost": 0.0}, "usage": {},
    })
    from src.post import handlers_common
    monkeypatch.setattr(handlers_common, "notify_telegram", lambda *a, **k: None)
    monkeypatch.setattr(handlers_common, "run", MagicMock(side_effect=AssertionError("dry-run must not call send")))

    import post_once
    monkeypatch.setattr(sys, "argv", ["post_once.py", "--dry-run", "--trigger", "test"])
    rc = post_once.main()
    assert rc == 0

    from src.hotspot import store
    with __import__("sqlite3").connect(str(store.HOTSPOT_STORE_PATH)) as conn:
        row = conn.execute("SELECT posted_at FROM hotspots WHERE id='hn:1'").fetchone()
    assert row[0] == ""


def test_send_failure_does_not_mark_posted(tmp_state, monkeypatch):
    _hotspot_row(tmp_state)
    _stub_post_plan(monkeypatch)
    from src.hotspot import selector
    monkeypatch.setattr(selector, "chat_json_result", lambda *a, **k: {
        "payload": {"best_index": 0, "reason": "ok"},
        "cost": {"total_cost": 0.0}, "usage": {},
    })
    from src.post import handlers_common
    monkeypatch.setattr(handlers_common, "notify_telegram", lambda *a, **k: None)
    monkeypatch.setattr(handlers_common, "run", MagicMock(return_value=types.SimpleNamespace(
        returncode=2, stdout="", stderr="boom",
    )))
    monkeypatch.setattr("src.persona_store.add_recent_post", lambda *a, **k: None)

    import post_once
    monkeypatch.setattr(sys, "argv", ["post_once.py", "--trigger", "test"])
    rc = post_once.main()
    assert rc != 0

    from src.hotspot import store
    with __import__("sqlite3").connect(str(store.HOTSPOT_STORE_PATH)) as conn:
        row = conn.execute("SELECT posted_at FROM hotspots WHERE id='hn:1'").fetchone()
    assert row[0] == ""


def test_manual_takes_priority_over_hotspot(tmp_state, monkeypatch):
    _hotspot_row(tmp_state)
    # Seed a pending manual topic.
    from src.common import POST_TOPICS_PATH
    POST_TOPICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    POST_TOPICS_PATH.write_text(json.dumps({
        "topics": [{"id": "manual-1", "source": "manual", "status": "pending",
                    "text": "manual override", "type": "argument"}]
    }), encoding="utf-8")

    _stub_post_plan(monkeypatch)
    _stub_run_success(monkeypatch)
    from src.post import handlers_common
    monkeypatch.setattr(handlers_common, "notify_telegram", lambda *a, **k: None)
    monkeypatch.setattr("src.persona_store.add_recent_post", lambda *a, **k: None)
    # Selector should NOT be reached.
    from src.hotspot import selector
    monkeypatch.setattr(selector, "pick_best",
                        MagicMock(side_effect=AssertionError("manual must short-circuit")))

    import post_once
    monkeypatch.setattr(sys, "argv", ["post_once.py", "--trigger", "test"])
    rc = post_once.main()
    assert rc == 0

    # Manual marked used; hotspot row untouched.
    after = json.loads(POST_TOPICS_PATH.read_text(encoding="utf-8"))
    assert after["topics"][0]["status"] == "used"
    from src.hotspot import store
    with __import__("sqlite3").connect(str(store.HOTSPOT_STORE_PATH)) as conn:
        row = conn.execute("SELECT posted_at FROM hotspots WHERE id='hn:1'").fetchone()
    assert row[0] == ""


def test_all_duplicate_falls_through_to_auto(tmp_state, monkeypatch):
    """selector returns None when LLM says all dup → post_once tries auto_topic."""
    _hotspot_row(tmp_state)
    from src.hotspot import selector
    monkeypatch.setattr(selector, "chat_json_result", lambda *a, **k: {
        "payload": {"best_index": -1, "reason": "all dup"},
        "cost": {"total_cost": 0.0}, "usage": {},
    })
    # auto topic generator triggered → return a stub topic.
    from src.post import topic_auto
    monkeypatch.setattr(topic_auto, "generate_auto_topic",
                        lambda: {"id": "auto", "type": "argument", "text": "fallback",
                                 "source": "auto", "status": "pending"})
    _stub_post_plan(monkeypatch)
    _stub_run_success(monkeypatch)
    from src.post import handlers_common
    monkeypatch.setattr(handlers_common, "notify_telegram", lambda *a, **k: None)
    monkeypatch.setattr("src.persona_store.add_recent_post", lambda *a, **k: None)

    import post_once
    monkeypatch.setattr(sys, "argv", ["post_once.py", "--trigger", "test"])
    rc = post_once.main()
    assert rc == 0

    # hotspot row NOT marked posted.
    from src.hotspot import store
    with __import__("sqlite3").connect(str(store.HOTSPOT_STORE_PATH)) as conn:
        row = conn.execute("SELECT posted_at FROM hotspots WHERE id='hn:1'").fetchone()
    assert row[0] == ""
```

- [ ] **Step 7.2: 跑集成测试**

```bash
pytest tests/integration/test_post_once_postable_pool.py -v
```
Expected: 5 个测试全 PASS。

> 若某条挂了，常见原因：
>   - `_stub_post_plan` mock 路径与 `post_once.py` import 路径不一致 — 检查 `post_once.py` 里实际怎么 import `generate_post_plan`，按相同路径 monkeypatch。
>   - `handlers_common.run` 的实际 import 来自 `src.post.handlers_common` 但被 `post_once.py` 中其他模块直接调用 — 必要时同时 patch `src.post.thread.run`、`src.post.article.run`。

- [ ] **Step 7.3: Commit**

```bash
git add tests/integration/test_post_once_postable_pool.py
git commit -m "test: integration coverage for post_once + postable_pool happy/dry-run/fail paths"
```

---

## Task 8: CHANGELOG + 全量测试

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 8.1: 跑全量测试**

```bash
pytest -v
```
Expected: 全 PASS。若有挂的非 hotspot/postable 测试，**停下查根因**，不要盲改。

- [ ] **Step 8.2: 更新 `CHANGELOG.md`**

在最新条目下面追加：

```markdown
## Hotspot 发现与发帖解耦

- 新增 `src/postable_pool.py` 服务层：post_once 通过它按优先级 `人工 > 热点 > auto` 取下一个 topic。
- 新增 `src/hotspot/selector.py`：候选 24h 内、按 `relevance_score × freshness_decay` 排序取本地 top 5，再用一次 LLM 调用判主题去重，避免同一天发同件事。
- `hotspot.db` 新增 `posted_at` 列；旧字段 `added_to_queue` 保留不再读写。schema 在打开 DB 时幂等迁移。
- `discover_hotspots.py` 不再写入 `post_topics.json`；只往 `hotspot.db` 写入候选。新热点入库后由 post_once 在发帖时挑选。
- `post_topics.json` 中遗留的 `source=hotspot && status=pending` 条目，会在 postable_pool 首次被调用时自动改为 `status=skipped, skip_reason=migrated_to_db_pool`（幂等、自动、无需手动脚本）。
- `/status` 文案变更：原"队列 pending/used/skipped"扩展为"人工待发/已用/跳过 + 热点池(24h) + 今日新发现/已发热点"。

### 行为变更总结

- 发现侧：每次跑 `discover_hotspots`，所有 relevant 候选都进库，**不**截顶到 3 条；旧的"discover 写 3 条到 JSON 队列"行为消失。
- 发帖侧：每次跑 `post_once`，若人工/Telegram 队列为空，从 hotspot.db 实时选当下最佳；若主题与当天已发重复，回落到 auto_topic。
```

- [ ] **Step 8.3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for hotspot decouple"
```

- [ ] **Step 8.4: 验收**

```bash
# 全测试
pytest -v
# 干运行一次 discover_hotspots
python3 discover_hotspots.py --dry-run
# 看 post_topics.json 是否新增过 source=hotspot pending（应当没有）
python3 -c "import json; d=json.load(open('state/post_topics.json')); print([t for t in d.get('topics',[]) if t.get('source')=='hotspot' and t.get('status')=='pending'])"
# 应输出 []
```

---

## 自检（plan 写完后我做的对照）

**Spec 覆盖：**
- 优先级 `人工 > 热点 > auto`：Task 3 + Task 5 ✓
- score≥3 且 relevant=true：Task 2 (`MIN_SCORE = 3`)，Task 1 测试断言 ✓
- 24h 窗口 + freshness 衰减：Task 2 (`CANDIDATE_HOURS = 24`、`_freshness_weight`) ✓
- LLM 主题去重：Task 2 (`SELECTOR_PROMPT` + best_index) ✓
- 失败不 mark_posted：Task 5 (`if send.returncode == 0` 才 mark)；Task 7 集成测试 ✓
- hotspot 只识别 `"used"`：Task 3 (`if status != "used": return`)，Task 3 测试 ✓
- 自动 idempotent 迁移：Task 3 (`_migrate_legacy_hotspot_topics_once`) ✓
- `added_to_queue` 保留不读写：Task 4（删 mark_added_to_queue 调用）+ Task 1（不动旧列）✓
- pool_status 输出形状：Task 3 + Task 6 ✓

**Placeholder 扫描：** 无 TBD / TODO / "implement later"，所有 step 都给了代码或确切命令。

**类型 / 命名一致性：** `_pool` / `_pool_ref` / `pick_best` / `pool_status` 在 Task 2/3/5/6 一致；`mark_posted` / `unposted_candidates_within` / `posted_today_summaries` 在 Task 1/2/3 一致。

**Scope：** 单 PR 可完成；8 个 task 按依赖顺序排列，每个有独立的测试与提交点；中途任何一处挂掉可单独回滚到上一个 commit。
