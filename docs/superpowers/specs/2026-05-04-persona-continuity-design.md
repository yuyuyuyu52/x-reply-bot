# 设计文档：事件驱动人设系统（Persona Continuity）

**日期：** 2026-05-04  
**范围：** x-reply-bot 主动发帖功能优化  
**方案：** 方案 B — 事件驱动人设系统

---

## 问题背景

当前主动发帖流水线（`post_generate.py`）存在三个根本问题：

1. **无时间线连续性**：每条帖子独立生成，LLM 会反复发明"上周发生了X"，打开主页看到多条互相矛盾的时间事件，一眼穿帮。
2. **事件纯属捏造**：帖子里的具体故事（朋友、经历、项目）没有真实锚点，读起来像编的。
3. **AI 结构特征**：帖子过于完整，惯用"这才是X的本质"类结论句，三段式背景→分析→总结过于工整。

---

## 目标

- 帖子时间轴前后一致，不出现"两条帖子都说上周发生了不同的事"的情况
- 时间锚（上周/昨天/最近）必须来自真实记录的事件
- 打破 AI 写作结构特征，允许不完整、不对称的表达
- 用户通过 Telegram `/event` 命令喂入真实事件，无事件时降级为无时间锚的观察/感受

---

## 数据模型

**新文件：`state/persona.json`**

```json
{
  "static": {
    "background": "20岁 founder / indie dev，长期在用 AI 工具搭产品...",
    "current_projects": ["项目A描述", "方向B描述"],
    "recurring_people": ["朋友X（做Y的）"],
    "reference_anchors": ["读过某本书", "长期用某工具"]
  },
  "events": [
    {
      "id": "evt-20260504-001",
      "timestamp": "2026-05-04 10:30:00 CST",
      "date": "2026-05-04",
      "raw": "今天和朋友聊了关于AI工具分发的事，他在做独立游戏",
      "source": "telegram"
    }
  ],
  "recent_posts": [
    {
      "timestamp": "2026-04-30 02:05:49 CST",
      "date": "2026-04-30",
      "text": "帖子正文...",
      "topic_type": "argument"
    }
  ]
}
```

- `events`：滚动保留最近 50 条，超出按时间删旧
- `recent_posts`：滚动保留最近 15 条
- `static`：用户一次性填写，冷启动提供 `state/persona_template.json` 模板

---

## 新模块：`persona_store.py`

对外接口：

```python
load_persona() -> dict
save_persona(data: dict)
add_event(raw: str, source: str = "telegram") -> dict
add_recent_post(text: str, topic_type: str)
get_generation_context() -> dict
```

`get_generation_context()` 返回：

```python
{
  "static": { ...static block... },
  "recent_events": [
    # 最近10条，每条含原始 raw 文本 + "X天前"相对日期字符串（基于 event.date 与今日计算）
  ],
  "recent_posts": [
    # 最近8条，每条含 date、topic_type、text 前100字
  ]
}
```

---

## `/event` Telegram 命令

- 格式：`/event 今天和朋友聊了关于XX的事`
- 处理：`bot_daemon.py` 路由 → `persona_store.add_event(text)` → 回复确认（中文）
- 与现有 `/run`、`/post_once` 等命令同一路由结构

---

## 生成管道改动

### `post_generate.py` — `build_candidate_messages()`

在 user message JSON 中增加 `"persona_context"` 字段：

```python
{
  "task": "生成3条候选...",
  "topic": payload,
  "persona_context": get_generation_context(),
  "requirements": [...]
}
```

### CANDIDATE_PROMPT 新增规则

**时间锚规则：**
- 如果帖子要用"上周""昨天""最近""前几天"等时间表达，必须来自 `persona_context.recent_events`，不能凭空编造
- 如果 `recent_events` 为空，不要发明时间事件，改写成无时间锚的观察或感受

**反 AI 结构规则：**
- 不要以"这才是X""归根结底""本质上""真正的问题是"结尾
- 帖子可以没有结论——以感受、细节、疑问结束都可以
- 不要写完整三段式（背景→分析→总结），可以只有其中一两段
- 句子可以不对称、不整齐，允许有一句多余或没说完的意思

### `build_review_messages()` 改动

`build_review_messages` 增加第三个参数 `persona_context: dict`，将 `recent_events` 注入到 user message JSON，供审稿时做时间锚校验。

### REVIEW_PROMPT 新增检查项

- 帖子出现时间锚但 `recent_events` 里找不到对应事件 → 判 false，`rewrite_hint` 要求去掉或替换时间锚
- 结尾是抽象总结句（"这才是X的本质"类）→ 判 false

---

## `post_once.py` 改动

帖子成功发出后，调用 `persona_store.add_recent_post(post_text, topic_type)`，将本次帖子记入 persona 状态，供下次生成参考。

---

## `sync_tg_commands.py` 改动

新增命令描述：

```python
{"command": "event", "description": "记录一件近期发生的事，供发帖时参考"}
```

---

## `common.py` 改动

新增常量：

```python
PERSONA_PATH = STATE_DIR / "persona.json"
PERSONA_TEMPLATE_PATH = ROOT / "state" / "persona_template.json"
```

---

## 改动文件汇总

| 文件 | 类型 | 说明 |
|---|---|---|
| `persona_store.py` | 新增 | 人设状态读写模块 |
| `state/persona_template.json` | 新增 | 冷启动模板，用户填写后复制为 persona.json |
| `bot_daemon.py` | 修改 | 加 `/event` 路由 |
| `post_generate.py` | 修改 | 注入 persona context + 更新 prompt |
| `post_once.py` | 修改 | 发帖后记录到 recent_posts |
| `sync_tg_commands.py` | 修改 | 加 `/event` 命令描述 |
| `common.py` | 微改 | 加 PERSONA_PATH 常量 |

---

## 边界与不在范围内的事

- 不自动从 `/event` 生成 topic（方案 C，后续可加）
- `static` 块不自动生成，由用户手动维护
- 不修改 reply 流水线（`run_once.py`），只改主动发帖
