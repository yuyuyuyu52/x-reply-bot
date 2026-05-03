# Reply Optimization Design
**Date:** 2026-05-04  
**Goal:** 增加回复功能获得的粉丝和浏览量，兼顾点赞和评论。

## Background

当前回复链路的核心问题：
1. `prepare_post.py` 选帖只靠关键词打分，没有传播信号（互动数据）
2. `generate_reply.py` 使用完全静态的 prompt，不感知 feed 规律
3. `observe_feed.py` 学习到的高质量帖分析（`why_it_works`、`hook_type` 等）**完全没有被回复生成使用**
4. `persona_store` 的账号 persona 也没有注入回复生成

## Goals

- 优先：粉丝增长、浏览量
- 次要：点赞、评论互动

## Design

### Part 1: 帖子选择升级（`prepare_post.py`）

Browser scraping 阶段额外抓每篇帖子的互动数字（likes、replies、reposts、views）。

**新评分公式：**
```
score = 关键词分 + engagement_bonus
engagement_bonus = log10(1 + likes*2 + replies*3 + reposts*2 + views/1000)
```

权重说明：
- `replies` 权重最高（3×）：说明帖子正在引发讨论，回复容易被看到
- `likes`、`reposts` 次之（2×）：传播信号强
- `views` 用对数压缩（/1000）：避免刷量帖主导

**AI 选帖 prompt 同步升级：** 在候选帖 JSON 中加入 `engagement` 字段，告知 LLM 各帖的互动数，让选帖倾向于"正在产生讨论"的帖子。

**边界条件：** 如果 DOM 变化导致互动数抓取失败，engagement_bonus 回退为 0，选帖流程不中断。

---

### Part 2: 回复生成三层注入（`generate_reply.py`）

在 `build_messages()` 中动态构建 system prompt，在静态规则基础上叠加三层：

**层 1 — 学习库摘要**

调用 `learning_store.recent_learning_references(limit=4)`，取最近 4 条 high_quality/worth_watching 帖的分析，提炼成：

```
【近期高互动帖规律】
- hook_type: 反直觉判断 → why_it_works: 先抛反常识结论，评论区自然来怼
- hook_type: 个人迁移体感 → why_it_works: 读者代入感强，容易引发同类经历共鸣
```

注入上限：≤ 400 字符。学习库为空时跳过此层，静默降级。

**层 2 — Persona 上下文**

调用 `persona_store.get_generation_context()`，注入：
- 账号静态 persona（身份/风格/价值观）
- 最近 3 条自己发的帖（避免回复风格和主帖风格雷同）

注入上限：≤ 200 字符。persona 未配置时跳过，静默降级。

**层 3 — 动态回复指令**

基于层 1 的规律，在 prompt 末尾加一条：
> 根据这条帖子的话题，优先使用上面规律中效果最好的 hook 类型开头，而不是写通用答案。

**接口兼容性：** `generate_reply_payload(post, system_prompt)` 签名不变。`build_messages()` 内部静默组装，外部调用方无感知。现有 `DEFAULT_PROMPT` 保留，作为 fallback 和 `--system-prompt` 参数的基础。

---

### Part 3: 整体数据流

```
observe_feed.py → learning.db (high_quality posts)
                                    ↓
persona_store.py → persona.json     ↓
                        ↓           ↓
prepare_post.py → selected_post.json (带 engagement 数据)
                        ↓
generate_reply.py ← learning_store.recent_learning_references()
                  ← persona_store.get_generation_context()
                        ↓
send_reply.py → X post
```

---

## Scope

**改动文件：**
- `prepare_post.py` — scraping 抓 engagement 数字 + 评分公式升级 + AI 选帖 prompt 加 engagement 字段
- `generate_reply.py` — `build_messages()` 动态组装三层 prompt

**不改动：**
- `run_once.py`、`send_reply.py`、`common.py`、`learning_store.py`、`persona_store.py`
- 所有 shell 脚本、daemon、Telegram 路由

## Out of Scope

- 回复后追踪互动数据（无 API，需另立项）
- 改变回复频率或调度逻辑
- 修改 learning_store schema
