# 热点发现与发帖解耦：postable_pool 服务层

**日期**：2026-05-13
**状态**：设计待评审
**作者**：will（与 Claude 协作）

## 问题

当前架构 `discover_hotspots.py` 同时承担两个职责：

1. 扫多个外部源 → LLM 评分 → 写入 `state/hotspot.db`
2. 把 top 3 直接 prepend 到 `state/post_topics.json` 队列

这种耦合带来两个实际问题：

- **发帖窗口与发现窗口绑死**：一次 discover 跑出的 top 3 即使被发现时质量不够（连续几条 `score=3`），也会硬塞入队，结果发出来一堆边缘内容。
- **没有"发帖时再选"的机会**：discover 完成后，候选池就被截到 3 条；后续即便有更新更热的故事出现，也得等下一次 discover。

用户对该功能的预期是：「**当天**最热的 3 个相关热点」，并且发帖时根据当下时机择优。

## 目标

- 把"发现/评分"与"决定现在发哪条"拆开。
- 发现侧把所有 relevant 候选都进库，不截顶。
- 发帖侧在每次 `post_once` 时实时挑"当下最该发"的那条，结合新鲜度衰减与已发主题去重。
- 引入服务层 `postable_pool` 作为发帖时机的唯一调度入口，封装人工/Telegram 队列与热点池。

## 非目标

- 不重写发现侧的源抓取/打分逻辑（沿用现状）。
- 不引入新的发帖类型；hotspot 仍然产出 `type=news_react` 的 topic。
- 不重构 `post_topics.py` CLI 或 Telegram 命令的录入逻辑。

## 关键决策

| 决策点 | 选择 |
|--------|------|
| 选题优先级 | 人工/Telegram > 热点 > auto_topic |
| 候选门槛 | `relevance_score ≥ 3 且 relevant=true` |
| 新鲜度窗口 | 24 小时 |
| 衰减曲线 | 0–6h 满权重 1.0；6–24h 线性衰减到 0.3；>24h SQL 排除 |
| 主题去重 | 发帖时一次 LLM 调用，候选 + 当天已发 cn_summary，返回 `best_index` |
| 失败处理 | hotspot 选中但发帖失败 → 不 mark_posted，下次仍可被选；不做 attempts 计数 |
| `mark_topic_used` 状态 | hotspot pool 只识别 `"used"`，其他状态忽略（pool 只关心"成功消费"） |
| 历史数据迁移 | `postable_pool` 模块内 lazy idempotent migration，无手动步骤 |
| 旧字段 `added_to_queue` | 保留列，不再读写（最低成本回滚保险） |

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│ post_once.py                                                │
│   topic = postable_pool.next_topic_to_post()                │
│   if not topic: topic = generate_auto_topic()               │
│   ... 发帖 ...                                              │
│   postable_pool.mark_topic_used(topic, status, extra)       │
└──────────────────┬──────────────────────────────────────────┘
                   ↓
┌─────────────────────────────────────────────────────────────┐
│ src/postable_pool.py     ← 新增，唯一对外入口                  │
│   next_topic_to_post():                                      │
│     1. topics.next_pending_post_topic()    人工/TG           │
│     2. hotspot.selector.pick_best()        LLM 去重选热点    │
│     3. None → 调用方走 auto                                  │
│   mark_topic_used(topic, …):                                 │
│     按 topic["_pool"] 派发                                   │
│   pool_status() → {manual:{...}, hotspot:{...}}              │
└─────┬─────────────────────────────────┬────────────────────┘
      ↓                                 ↓
┌──────────────────┐         ┌──────────────────────────────┐
│ src/topics.py    │         │ src/hotspot/store.py         │
│ (不变, 低层 CRUD) │         │  + posted_at 列              │
│                  │         │  + mark_posted()             │
│                  │         │  + unposted_candidates(...)  │
│                  │         │  + posted_today_summaries()  │
│                  │         └──────────────────────────────┘
└──────────────────┘                     ↑
                              ┌──────────────────────────────┐
                              │ src/hotspot/selector.py      │ ← 新增
                              │  pick_best() = 本地打分      │
                              │   + LLM 主题去重一次调用     │
                              └──────────────────────────────┘
```

**核心约束**：

- `discover_hotspots.py` 不再写 `post_topics.json`，只 INSERT 到 `hotspot.db`。
- `post_topics.json` 只承载人工 / Telegram 注入的 topic。
- `post_once` 完全通过 postable_pool 获取与标记，不直接 import `src/topics.py` 或 `src/hotspot/store.py`。
- 直接 CRUD（`post_topics.py --add` CLI、Telegram 命令）继续走 `src/topics.py`，不走 pool。Pool 只管"调度选择 / 标记"，不管"录入"。

## 模块职责

### `src/postable_pool.py`（新增）

```python
def next_topic_to_post() -> dict | None:
    """按优先级返回下一个可发 topic，带 _pool / _pool_ref 内部字段。"""

def mark_topic_used(topic: dict, status: str = "used", extra: dict | None = None) -> None:
    """按 topic['_pool'] 派发到 topics 或 hotspot.store。"""

def pool_status() -> dict:
    """{ 'manual': {...}, 'hotspot': {pool_size_24h, discovered_today, posted_today} }"""
```

返回的 topic dict 在标准字段之上有两个内部字段：

- `_pool`: `"manual"` 或 `"hotspot"`
- `_pool_ref`: hotspot 时为 `"{source}:{hotspot_id}"`（store row PK），manual 时为空

这两个字段在 post_once 持久化 history 之前被 strip 掉，只用于 pool 内部派发。

模块顶部维护 `_migration_done: bool` 标志，`next_topic_to_post` 和 `pool_status` 首次调用前触发 `_migrate_legacy_hotspot_topics_once()`：扫 `post_topics.json`，把 `source=hotspot && status=pending` 改为 `status=skipped, skip_reason=migrated_to_db_pool, migrated_at=<now>`。整个迁移在 `POST_TOPICS_LOCK_PATH` 锁内执行，跨进程也安全。

### `src/hotspot/selector.py`（新增）

```python
def pick_best(now: datetime | None = None) -> dict | None:
    """
    1. store.unposted_candidates_within(hours=24, min_score=3)
    2. 本地按 relevance_score × freshness_weight(age_hours) 打分 → top 5
    3. store.posted_today_summaries() 取当天 cn_summary 列表
    4. 一次 chat_json_result: "从 5 个候选里挑一个跟已发列表主题不重复的"
       → {"best_index": 0..4 或 -1, "reason": str}
    5. 选中行转成 topic dict（字段映射见下），返回；
       best_index=-1 / 异常 / 候选 0 条 → None
    """
```

**freshness_weight 公式**：

```
age_hours ≤ 6      → 1.0
6 < age_hours ≤ 24 → 1.0 - 0.7 * (age_hours - 6) / 18    # 线性衰到 0.3
age_hours > 24     → SQL 已排除，不会到这一步
```

**Topic dict 字段映射**（沿用现 discover_hotspots.py 的写法）：

| topic 字段 | 来源 |
|-----------|------|
| `id` | `f"hotspot-{source}-{hotspot_id}"` |
| `type` | `"news_react"` |
| `text` | `row.cn_summary` |
| `source` | `"hotspot"` |
| `status` | `"pending"` |
| `subject` | `row.title` |
| `event_or_context` | `f"今天[{row.source}] {row.relevance_reason} \| 原链接: {row.url}"` |
| `stance` | `row.angle` |
| `evidence_hint` | `f"热度: {row.hn_score}↑ {row.hn_descendants}💬 \| 相关度: {row.relevance_score}/5"` |
| `_pool` | `"hotspot"` |
| `_pool_ref` | `f"{row.source}:{row.hotspot_id}"` |

### `src/hotspot/store.py`（增量）

Schema 演进（在 `_ensure_schema` 后追加 `_ensure_columns`）：

```python
def _ensure_columns(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(hotspots)")}
    if "posted_at" not in cols:
        conn.execute(
            "ALTER TABLE hotspots ADD COLUMN posted_at TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hotspots_posted "
        "ON hotspots(posted_at, relevance_score)"
    )
```

新增 API：

```python
def mark_posted(source: str, hotspot_id: str) -> None: ...
def unposted_candidates_within(hours: int, min_score: int) -> list[dict]: ...
def posted_today_summaries() -> list[str]: ...
```

`added_to_queue` 列保留，新代码不读不写。

### 不动的模块

- `src/topics.py`：保持原样。
- `post_topics.py` CLI：保持原样。
- `src/telegram_commands.py`：保持原样。
- `bot_daemon.py`、`src/reporters.py`：把 `post_topic_summary()` 调用替换为 `postable_pool.pool_status()`，`/status` 文案扩展为「人工待发 N 条 ｜ 热点池 M 条 / 今天已发 K 条」。

## 数据流

### 发现侧

```
discover_hotspots.py
  → discover.discover_hotspots()
      → 抓 11 源 → 本地 (engagement×weight + keyword_boost) 排
      → top 30 unseen → LLM filter → 本地 high_priority floor
      → store.insert_hotspot(...)   # 全部 30 都进库
  → record + Telegram notify
  ✗ 不再调 _queue_daily_hotspot_topics
  ✗ 不再调 mark_added_to_queue
```

### 发帖侧

```
post_once.py
  → postable_pool.next_topic_to_post()
      ├─ _migrate_legacy_hotspot_topics_once()   # 幂等
      ├─ topics.next_pending_post_topic()
      │     命中 → 设 _pool="manual" → return
      ├─ hotspot.selector.pick_best()
      │     ├─ unposted_candidates_within(24, 3)
      │     ├─ 本地 freshness 打分 → top 5
      │     ├─ posted_today_summaries()
      │     ├─ 1× LLM call → best_index
      │     └─ row → topic dict (_pool="hotspot")
      └─ 三路皆空 → return None
  → topic is None ? generate_auto_topic() : keep
  → 发帖流程（不变）
  → 成功后:
       strip(_pool, _pool_ref) 再写 history
       postable_pool.mark_topic_used(topic, status="used", extra={...})
           ├─ _pool=="manual"  → topics.mark_post_topic_status(...)
           └─ _pool=="hotspot" → store.mark_posted(source, hotspot_id)
  → 失败 / dry-run：
       hotspot 不 mark_posted（下次仍可选）
```

### `/status` 与 reporter 侧

```
bot_daemon._daily_status / reporters
  → postable_pool.pool_status()
      → { "manual": topics.post_topic_summary(),
          "hotspot": { "pool_size_24h", "discovered_today", "posted_today" } }
  → 文案: "人工待发 N ｜ 热点池 M / 今天已发 K"
```

## 错误处理

| 场景 | 行为 |
|------|------|
| `topics.next_pending_post_topic` 异常 | log warning，跳到 hotspot 分支 |
| `selector.pick_best` SQL 异常 | log warning，返回 None |
| `selector.pick_best` LLM 异常 | log warning，返回 None（不退化为本地 top 1） |
| selector 候选 0 条 | 返回 None（info log） |
| LLM 返回 `best_index=-1`（全跟已发重复） | 返回 None；info log |
| LLM 返回越界 index | 视为 -1，warning log |
| `mark_posted` 写库失败 | log error，不抛，不阻塞已发出的帖子 |
| 并发抢 `hotspot.db` | 现有 `_get_conn(timeout=10)` 已足 |
| `_pool_ref` 缺失或格式错（hotspot 分支） | warning + no-op |

## 测试

### 单测

**`tests/unit/test_postable_pool.py`**（新）：

- next_topic_to_post：manual 命中 / hotspot 命中 / 全空
- mark_topic_used：manual 派发 / hotspot 派发 / `_pool` 缺失 no-op
- pool_status：聚合输出形状
- legacy 迁移：含 source=hotspot pending 的 JSON → 一次调用后 status=skipped；二次调用无副作用

**`tests/unit/test_hotspot_selector.py`**（新）：

- 候选 0 条 → None
- 单条 + 无已发 → 必选
- 多候选 freshness 顺序
- LLM 返回 best_index 各值（0 / 越界 / -1）
- LLM 异常 → None

**`tests/unit/test_hotspot_store.py`**（扩展）：

- `posted_at` 列迁移幂等
- `mark_posted` + `unposted_candidates_within` 行为
- 旧库（无 posted_at）打开自动加列

**`tests/unit/test_discover_hotspots_entrypoint.py`**（修改）：

- 验证不再调 `load_post_topics` / `save_post_topics` / `mark_added_to_queue`

### 集成

**`tests/integration/test_post_once_postable_pool.py`**（新，mock 浏览器 + LLM）：

- 人工 + hotspot 共存 → 选人工
- 仅 hotspot → 选 hotspot，post 成功后 SQLite `posted_at` 被写
- 仅 hotspot + 全跟已发重复 → 走 auto_topic
- `--dry-run` → hotspot 不被 mark_posted
- send 失败抛错 → hotspot 不被 mark_posted

### 文档

CHANGELOG 加一段"行为变更"说明：

- hotspot 不再写 post_topics.json；新热点入库即可发
- /status 输出格式变化
- `added_to_queue` 字段废弃但保留
- 部署后首次任何 pool 调用会自动迁移历史 JSON 中的 source=hotspot pending 为 skipped

## 回滚

- 旧表列与字段保留；新增列 `posted_at` 默认空字符串，旧代码忽略不报错。
- 回滚策略：revert PR + 删除 `postable_pool.py` 与 `selector.py`，恢复 `post_once.py` 与 `discover_hotspots.py` 旧版。`hotspot.db` 中新增列保留，无数据丢失。

## 范围之外（后续可单独提）

- 重写 discover 评分（合并 30 次 LLM 调用为 1 次批量评分） —— 单独 spec。
- Hotspot 发现窗口可配置化（环境变量）。
- 发帖时段匹配（早间适合什么类型 / 晚间适合什么类型） —— 单独 spec。
