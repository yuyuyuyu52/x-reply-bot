# 设计文档：Persona-First 自主发帖

**日期：** 2026-05-04  
**范围：** x-reply-bot 主动发帖功能重构  
**方案：** 方案 B — 两步生成（先决定说什么，再写怎么说）

---

## 问题背景

当前发帖逻辑是 topic-first：人工维护选题队列 → 生成 → 发。选题与账号整体人设无关，帖子之间没有人格连贯性，内容随机且需要人工干预。

用户目标：bot 能像一个真实的人一样经营自己的 X 账号，有清晰的身份认同，让陌生人看到账号就知道"这人是做什么的"。

---

## 目标

- 队列为空时，bot 自主决定发什么，不依赖人工加题
- 每条帖子有人格连贯性：不重复上条的角度，在多个内容支柱间自然轮转
- 有真实事件（`/event`）时优先从事件取灵感；无事件时从 persona 档案推断
- 手动加题仍然有效（队列非空时优先用）

---

## 第一节：Persona 档案扩展

在 `state/persona.json` 的 `static` 块写入以下内容，替换模板占位符：

```json
{
  "static": {
    "background": "在公司打工，同时在折腾副业、自媒体和 Web3。vibe coder，主力工具是 Claude / Codex / Gemini，写代码靠 AI 推着走，想法靠自己。想挣点属于自己的钱，还没挣到，但一直在试。",
    "content_pillars": [
      "AI 工具体验：Claude、Codex、Gemini 的真实使用感受、吐槽、新发现，用过才有发言权",
      "Vibe coding：AI 辅助写代码的节奏、踩坑、意外好用的地方",
      "副业与自媒体：主业之外折腾产品和内容的过程，包括卡点、小进展、没进展",
      "Web3 观察：对链上动态、新项目、市场的直接感受，不分析只说看到什么",
      "打工人张力：上班和想出去之间的状态，偶尔一条，不多"
    ],
    "voice": "直接，偶尔有点丧但不绝望，不说教，不总结大道理，说具体的事",
    "current_projects": [
      "在用 AI 工具做一个自己想用的小产品，方向还在摸索",
      "经营 X 账号，在试自媒体这条路"
    ],
    "recurring_people": []
  }
}
```

`current_projects` 和 `recurring_people` 可随时通过 `/event` 更新，persona_store 会自动记录演化。

---

## 第二节：自主选题生成（`topic_auto.py`）

新增模块，唯一职责：让 LLM 以角色身份决定"今天最想说什么"。

### 对外接口

```python
generate_auto_topic() -> dict
```

返回的 dict 与现有 `normalize_post_topic()` 兼容：

```python
{
    "id": "auto-<timestamp>",
    "type": "casual|argument|story|news_react",
    "text": "选题一句话描述",
    "angle": "具体切入角度",
    "source": "auto",
    "status": "pending",
    "subject": "",
    "event_or_context": "",
    "stance": "",
    "evidence_hint": "",
}
```

### 生成 Prompt 要点

输入给 LLM 的上下文（来自 `get_generation_context()`）：
- `static.background` + `static.content_pillars` + `static.voice`
- `recent_events`（最近 10 条，带相对日期）
- `recent_posts`（最近 8 条，带日期 + 类型 + 摘要）
- 今日日期

LLM 输出严格 JSON：
```json
{
  "type": "casual|argument|story|news_react",
  "text": "...",
  "angle": "...",
  "reason": "..."
}
```

### Prompt 核心约束

- 必须从 content_pillars 里选一个最近帖子覆盖较少的方向
- 有 recent_events 时：从事件里找灵感（不复述，而是"因为这件事想到了什么"）
- 无 recent_events 时：从 background 和 current_projects 推断这个人最近在想什么
- 禁止选泛泛的大话题，必须有一个具体切入点
- type 选择与内容匹配：随手感受用 casual，有论点用 argument，有故事用 story，有新闻用 news_react

### 异常处理

- LLM 返回非法 JSON：最多重试 2 次（复用 `parse_json_object`）
- 重试后仍失败：抛出异常，`post_once.py` 捕获后记录 `status: "auto_topic_failed"` 并返回 1

---

## 第三节：集成到 `post_once.py`

仅改动 `next_pending_post_topic()` 后的分支：

**改前：**
```python
topic = next_pending_post_topic()
if not topic:
    payload = {"status": "no_pending_topic", ...}
    write_json(LATEST_POST_RUN_PATH, payload)
    return 2
```

**改后：**
```python
topic = next_pending_post_topic()
if not topic:
    topic = generate_auto_topic()   # 自主生成，失败会抛异常
```

异常由 `main()` 的外层 try/except 捕获，写入 `status: "auto_topic_failed"` 记录后返回 1。

`generate_post_plan(topic)` 及其后的所有流程完全不变。

---

## 改动文件汇总

| 文件 | 类型 | 说明 |
|---|---|---|
| `topic_auto.py` | 新增 | `generate_auto_topic()` 实现 |
| `state/persona.json` | 修改 | 写入真实 static 块，替换模板 |
| `post_once.py` | 微改 | 队列空时调 `generate_auto_topic()` |

不改动：`post_generate.py`、`persona_store.py`、`bot_daemon.py`、`common.py`

---

## 边界与不在范围内的事

- 不实现"bot 审稿后等用户确认再发"（方案 C）
- 不修改 reply 流水线
- `topic_auto.py` 不写入 post_topics.json，选题用完即丢，不持久化
- `recurring_people` 不自动提取，由用户手动维护
