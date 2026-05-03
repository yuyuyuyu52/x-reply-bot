# Persona Continuity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an event-driven persona memory system so proactive posts have consistent timelines, real event anchors, and less AI-structural patterning.

**Architecture:** A new `persona_store.py` module maintains `state/persona.json` (static background + rolling events + recent posts). The generation pipeline in `post_generate.py` reads this context and enforces rules: time words must be backed by recorded events, and AI summary-conclusion patterns are penalized at both generation and review stages. The daemon routes `/event <text>` Telegram commands into the event store.

**Tech Stack:** Python 3, stdlib only (json, pathlib, datetime). No new dependencies.

---

### Task 1: Add PERSONA_PATH constant to common.py

**Files:**
- Modify: `common.py:23`

- [ ] **Step 1: Add the constant**

In `common.py`, after line 23 (`POST_TOPICS_PATH = STATE_DIR / "post_topics.json"`), add:

```python
PERSONA_PATH = STATE_DIR / "persona.json"
```

The block at lines 15–26 should now look like:

```python
SELECTED_PATH = STATE_DIR / "selected_post.json"
REPLIED_PATH = STATE_DIR / "replied_posts.json"
RUN_LOG_PATH = STATE_DIR / "run_log.json"
SCREENSHOT_DIR = STATE_DIR / "screenshots"
HISTORY_DIR = STATE_DIR / "history"
LATEST_RUN_PATH = STATE_DIR / "latest_run.json"
POST_HISTORY_DIR = STATE_DIR / "post_history"
LATEST_POST_RUN_PATH = STATE_DIR / "latest_post_run.json"
POST_TOPICS_PATH = STATE_DIR / "post_topics.json"
PERSONA_PATH = STATE_DIR / "persona.json"
TELEGRAM_STATE_PATH = STATE_DIR / "telegram_state.json"
DAILY_REPORT_STATE_PATH = STATE_DIR / "daily_report_state.json"
```

- [ ] **Step 2: Verify import**

```bash
python3 -c "from common import PERSONA_PATH; print(PERSONA_PATH)"
```

Expected output: `/Users/Zhuanz/Documents/x-reply-bot/state/persona.json` (or the equivalent absolute path on the production host).

- [ ] **Step 3: Commit**

```bash
git add common.py
git commit -m "feat(persona): add PERSONA_PATH constant to common.py"
```

---

### Task 2: Create persona_store.py

**Files:**
- Create: `persona_store.py`

- [ ] **Step 1: Create the file**

Create `/Users/Zhuanz/Documents/x-reply-bot/persona_store.py` with this content:

```python
#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, date
from pathlib import Path

from common import PERSONA_PATH

_EMPTY: dict = {"static": {}, "events": [], "recent_posts": []}


def load_persona() -> dict:
    try:
        data = json.loads(PERSONA_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"static": {}, "events": [], "recent_posts": []}
    return {
        "static": data.get("static") or {},
        "events": [e for e in (data.get("events") or []) if isinstance(e, dict)],
        "recent_posts": [p for p in (data.get("recent_posts") or []) if isinstance(p, dict)],
    }


def save_persona(data: dict) -> None:
    PERSONA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PERSONA_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PERSONA_PATH)


def add_event(raw: str, source: str = "telegram") -> dict:
    persona = load_persona()
    now = datetime.now().astimezone()
    event: dict = {
        "id": f"evt-{now.strftime('%Y%m%d-%H%M%S')}",
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date": now.strftime("%Y-%m-%d"),
        "raw": raw.strip(),
        "source": source,
    }
    events = persona["events"]
    events.append(event)
    persona["events"] = events[-50:]
    save_persona(persona)
    return event


def add_recent_post(text: str, topic_type: str) -> None:
    persona = load_persona()
    now = datetime.now().astimezone()
    post: dict = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date": now.strftime("%Y-%m-%d"),
        "text": text.strip(),
        "topic_type": topic_type,
    }
    posts = persona["recent_posts"]
    posts.append(post)
    persona["recent_posts"] = posts[-15:]
    save_persona(persona)


def _relative_date(event_date_str: str) -> str:
    try:
        delta = (date.today() - date.fromisoformat(event_date_str)).days
    except (ValueError, TypeError):
        return ""
    if delta == 0:
        return "今天"
    if delta == 1:
        return "昨天"
    if delta <= 7:
        return f"{delta}天前"
    weeks = delta // 7
    return f"约{weeks}周前"


def get_generation_context() -> dict:
    persona = load_persona()
    recent_events = [
        {
            "raw": e.get("raw", ""),
            "relative_date": _relative_date(e.get("date", "")),
        }
        for e in persona["events"][-10:]
    ]
    recent_posts = [
        {
            "date": p.get("date", ""),
            "topic_type": p.get("topic_type", ""),
            "text": str(p.get("text", ""))[:100],
        }
        for p in persona["recent_posts"][-8:]
    ]
    return {
        "static": persona["static"],
        "recent_events": recent_events,
        "recent_posts": recent_posts,
    }
```

- [ ] **Step 2: Smoke-test the module**

```bash
python3 -c "
from persona_store import load_persona, get_generation_context, add_event, add_recent_post
ctx = get_generation_context()
print('static keys:', list(ctx['static'].keys()))
print('recent_events count:', len(ctx['recent_events']))
print('recent_posts count:', len(ctx['recent_posts']))
evt = add_event('测试事件：今天读了一篇关于RAG的文章', source='test')
print('added event id:', evt['id'])
add_recent_post('这是一条测试帖子内容。', 'casual')
ctx2 = get_generation_context()
print('after adds — events:', len(ctx2['recent_events']), 'posts:', len(ctx2['recent_posts']))
"
```

Expected: no errors; event and post counts increment by 1 each.

- [ ] **Step 3: Commit**

```bash
git add persona_store.py
git commit -m "feat(persona): add persona_store module"
```

---

### Task 3: Create state/persona_template.json

**Files:**
- Create: `state/persona_template.json`

- [ ] **Step 1: Create the template**

Create `/Users/Zhuanz/Documents/x-reply-bot/state/persona_template.json`:

```json
{
  "static": {
    "background": "20岁 founder / indie dev，长期混 X/Twitter。主要在用 AI 工具搭产品，关注分发、工作流、工具体验和激励设计。英语过得去，中文为主。喜欢折腾新模型，习惯边用边想边发。",
    "current_projects": [
      "正在做一个基于LLM的XX工具（填上你实际在做的项目）",
      "在研究XX方向（填上你实际在研究的方向）"
    ],
    "recurring_people": [
      "朋友A（做XX的，经常聊产品）",
      "朋友B（做XX的，互相交流工具经验）"
    ],
    "reference_anchors": [
      "长期用Cursor和Claude写代码",
      "读过《某本书》，对某观点印象深刻"
    ]
  },
  "events": [],
  "recent_posts": []
}
```

**Note for user:** Copy this file to `state/persona.json` and fill in the `static` block with real details. The `events` and `recent_posts` arrays will be populated automatically at runtime.

- [ ] **Step 2: Verify file is valid JSON**

```bash
python3 -c "import json; json.load(open('state/persona_template.json')); print('valid JSON')"
```

Expected: `valid JSON`

- [ ] **Step 3: Commit**

```bash
git add state/persona_template.json
git commit -m "feat(persona): add persona_template.json for cold-start"
```

---

### Task 4: Update CANDIDATE_PROMPT and REVIEW_PROMPT in post_generate.py

**Files:**
- Modify: `post_generate.py:36-55` (CANDIDATE_PROMPT 总规则 section)
- Modify: `post_generate.py:78-89` (REVIEW_PROMPT rules)

- [ ] **Step 1: Update CANDIDATE_PROMPT**

In `post_generate.py`, replace the 总规则 block (lines 35–44):

```python
总规则：
- 一律用中文。
- 不要泛泛而谈，必须挂在具体对象、事件、场景或使用体验上。
- 不要写成金句生成器，不要只输出抽象判断。
- 不要写成公众号、周报、营销文案、鸡汤。
- 不要用 hashtag，不要 emoji。
- 禁止默认用这些句式起手：很多……、本质上……、归根结底……、真正的……、问题不在……而在……。
- 三条候选的角度必须明显不同，不能只是同一句话改写。
- `reason` 和 `angle` 都用中文。
```

Replace with:

```python
总规则：
- 一律用中文。
- 不要泛泛而谈，必须挂在具体对象、事件、场景或使用体验上。
- 不要写成金句生成器，不要只输出抽象判断。
- 不要写成公众号、周报、营销文案、鸡汤。
- 不要用 hashtag，不要 emoji。
- 禁止默认用这些句式起手：很多……、本质上……、归根结底……、真正的……、问题不在……而在……。
- 禁止以"这才是X""归根结底""本质上""真正的问题""说到底"等抽象句式收尾；帖子可以没有结论，以感受、细节或疑问结束都行。
- 不要写完整三段式（背景→分析→总结），写一两段就够；句子允许不对称、不整齐。
- 时间锚规则：如果要用"上周""昨天""最近""前几天""上个月"，必须来自 persona_context.recent_events；如果该列表为空，改用无时间锚的当下观察或感受。
- 三条候选的角度必须明显不同，不能只是同一句话改写。
- `reason` 和 `angle` 都用中文。
```

- [ ] **Step 2: Update REVIEW_PROMPT**

In `post_generate.py`, replace the false-condition list (lines 80–87):

```python
- 只要出现以下任一问题，就判 false：
  1. 过于抽象，没有具体对象/事件/场景
  2. 像 AI 在总结行业观点
  3. 太工整、太完整，像文章摘要
  4. 起手就是套话或抽象判断
  5. 明显没有按 topic type 去写
```

Replace with:

```python
- 只要出现以下任一问题，就判 false：
  1. 过于抽象，没有具体对象/事件/场景
  2. 像 AI 在总结行业观点
  3. 太工整、太完整，像文章摘要
  4. 起手就是套话或抽象判断
  5. 明显没有按 topic type 去写
  6. 出现时间词（上周/昨天/最近/前几天）但 recent_events 里没有对应事件支撑
  7. 结尾是抽象总结句（"这才是X""归根结底""真正的…""说到底"类）
```

- [ ] **Step 3: Verify prompts load without error**

```bash
python3 -c "from post_generate import CANDIDATE_PROMPT, REVIEW_PROMPT; print('CANDIDATE len:', len(CANDIDATE_PROMPT)); print('REVIEW len:', len(REVIEW_PROMPT))"
```

Expected: prints two lengths, no errors.

- [ ] **Step 4: Commit**

```bash
git add post_generate.py
git commit -m "feat(persona): update CANDIDATE/REVIEW prompts with time-anchor and anti-AI-structure rules"
```

---

### Task 5: Inject persona context into post_generate.py pipeline

**Files:**
- Modify: `post_generate.py` — imports, `build_candidate_messages`, `build_review_messages`, `rewrite_selected_candidate`, `generate_post_plan`

- [ ] **Step 1: Add import**

At the top of `post_generate.py`, after the existing imports block (after line 15), add:

```python
from persona_store import get_generation_context
```

The imports block should now end:

```python
from common import (
    chat_text_result,
    ensure_state_dirs,
    load_env_file,
    next_pending_post_topic,
    normalize_post_topic,
    parse_json_object,
    topic_summary_text,
)
from learning_store import recent_learning_references
from persona_store import get_generation_context
```

- [ ] **Step 2: Update build_candidate_messages to inject persona_context**

In `build_candidate_messages`, replace the return statement (lines 175–194):

```python
    return [
        {"role": "system", "content": CANDIDATE_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "生成 3 条不同角度的主动发帖候选",
                    "topic": payload,
                    "requirements": [
                        "不要泛泛地谈 founder、AI 产品、独立开发这类大词",
                        "一定要挂在具体对象、最近观察、一次经历、一个热点或一段明确论证上",
                        "如果是 argument，必须把观点展开，不要只给一句口号",
                    ],
                    "recent_learning_references": compact_refs,
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]
```

Replace with:

```python
    return [
        {"role": "system", "content": CANDIDATE_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": "生成 3 条不同角度的主动发帖候选",
                    "topic": payload,
                    "persona_context": get_generation_context(),
                    "requirements": [
                        "不要泛泛地谈 founder、AI 产品、独立开发这类大词",
                        "一定要挂在具体对象、最近观察、一次经历、一个热点或一段明确论证上",
                        "如果是 argument，必须把观点展开，不要只给一句口号",
                    ],
                    "recent_learning_references": compact_refs,
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]
```

- [ ] **Step 3: Update build_review_messages signature and body**

Replace the entire `build_review_messages` function (lines 214–228):

```python
def build_review_messages(topic: dict, candidate: dict) -> list[dict]:
    return [
        {"role": "system", "content": REVIEW_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "topic": topic_payload(topic),
                    "candidate": candidate,
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]
```

Replace with:

```python
def build_review_messages(topic: dict, candidate: dict, persona_context: dict) -> list[dict]:
    return [
        {"role": "system", "content": REVIEW_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "topic": topic_payload(topic),
                    "candidate": candidate,
                    "recent_events": persona_context.get("recent_events", []),
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]
```

- [ ] **Step 4: Update rewrite_selected_candidate to accept and pass persona_context**

Replace the `rewrite_selected_candidate` function signature and its internal `build_review_messages` call (lines 266–291):

```python
def rewrite_selected_candidate(topic: dict, candidate: dict, review_reason: str, rewrite_hint: str) -> tuple[dict, dict, dict]:
    rewrite_result, rewrite_payload = chat_json_result(
        build_rewrite_messages(topic, candidate, review_reason, rewrite_hint),
        temperature=0.85,
        max_tokens=420,
    )
    rewritten = {
        "index": candidate["index"],
        "text": str(rewrite_payload.get("text") or "").strip(),
        "angle": candidate.get("angle", ""),
        "reason": str(rewrite_payload.get("reason") or "").strip().replace("\n", " "),
    }
    if not rewritten["text"]:
        raise RuntimeError(f"Rewrite returned empty text: {rewrite_payload}")
    review_result, review_payload = chat_json_result(
        build_review_messages(topic, rewritten),
        temperature=0.2,
        max_tokens=720,
    )
    return rewritten, rewrite_result, {
        "pass": bool(review_payload.get("pass")),
        "reason": str(review_payload.get("reason") or "").strip().replace("\n", " "),
        "rewrite_hint": str(review_payload.get("rewrite_hint") or "").strip().replace("\n", " "),
        "usage": review_result["usage"],
        "cost": review_result["cost"],
    }
```

Replace with:

```python
def rewrite_selected_candidate(topic: dict, candidate: dict, review_reason: str, rewrite_hint: str, persona_context: dict) -> tuple[dict, dict, dict]:
    rewrite_result, rewrite_payload = chat_json_result(
        build_rewrite_messages(topic, candidate, review_reason, rewrite_hint),
        temperature=0.85,
        max_tokens=420,
    )
    rewritten = {
        "index": candidate["index"],
        "text": str(rewrite_payload.get("text") or "").strip(),
        "angle": candidate.get("angle", ""),
        "reason": str(rewrite_payload.get("reason") or "").strip().replace("\n", " "),
    }
    if not rewritten["text"]:
        raise RuntimeError(f"Rewrite returned empty text: {rewrite_payload}")
    review_result, review_payload = chat_json_result(
        build_review_messages(topic, rewritten, persona_context),
        temperature=0.2,
        max_tokens=720,
    )
    return rewritten, rewrite_result, {
        "pass": bool(review_payload.get("pass")),
        "reason": str(review_payload.get("reason") or "").strip().replace("\n", " "),
        "rewrite_hint": str(review_payload.get("rewrite_hint") or "").strip().replace("\n", " "),
        "usage": review_result["usage"],
        "cost": review_result["cost"],
    }
```

- [ ] **Step 5: Update generate_post_plan to thread persona_context through**

In `generate_post_plan`, add `persona_context = get_generation_context()` right after `topic = normalize_post_topic(topic)` (line 295), then update both `build_review_messages` calls to pass it.

Replace the opening of `generate_post_plan` (lines 294–300):

```python
def generate_post_plan(topic: dict) -> dict:
    topic = normalize_post_topic(topic)
    if not topic_summary_text(topic):
        raise RuntimeError("Empty topic.")

    candidate_result, candidate_payload = chat_json_result(
```

Replace with:

```python
def generate_post_plan(topic: dict) -> dict:
    topic = normalize_post_topic(topic)
    if not topic_summary_text(topic):
        raise RuntimeError("Empty topic.")
    persona_context = get_generation_context()

    candidate_result, candidate_payload = chat_json_result(
```

Then replace the first `build_review_messages` call inside `generate_post_plan` (around line 330):

```python
        review_result, review_payload = chat_json_result(
            build_review_messages(topic, selected_candidate),
            temperature=0.2,
            max_tokens=720,
        )
```

Replace with:

```python
        review_result, review_payload = chat_json_result(
            build_review_messages(topic, selected_candidate, persona_context),
            temperature=0.2,
            max_tokens=720,
        )
```

Then replace the `rewrite_selected_candidate` call (around line 341):

```python
            rewritten_candidate, rewrite_model_result, rewritten_review = rewrite_selected_candidate(
                topic,
                selected_candidate,
                review_reason,
                review_rewrite_hint,
            )
```

Replace with:

```python
            rewritten_candidate, rewrite_model_result, rewritten_review = rewrite_selected_candidate(
                topic,
                selected_candidate,
                review_reason,
                review_rewrite_hint,
                persona_context,
            )
```

- [ ] **Step 6: Verify imports and function signatures**

```bash
python3 -c "
from post_generate import (
    build_candidate_messages,
    build_review_messages,
    rewrite_selected_candidate,
    generate_post_plan,
)
import inspect
print('build_review_messages params:', list(inspect.signature(build_review_messages).parameters))
print('rewrite_selected_candidate params:', list(inspect.signature(rewrite_selected_candidate).parameters))
print('ok')
"
```

Expected:
```
build_review_messages params: ['topic', 'candidate', 'persona_context']
rewrite_selected_candidate params: ['topic', 'candidate', 'review_reason', 'rewrite_hint', 'persona_context']
ok
```

- [ ] **Step 7: Commit**

```bash
git add post_generate.py
git commit -m "feat(persona): inject persona context into post generation pipeline"
```

---

### Task 6: Record post to persona after sending in post_once.py

**Files:**
- Modify: `post_once.py` — import + one call after successful send

- [ ] **Step 1: Add import**

In `post_once.py`, after the existing imports (after line 22), add:

```python
from persona_store import add_recent_post
```

The imports section should now end:

```python
from post_generate import generate_post_plan
from persona_store import add_recent_post
```

- [ ] **Step 2: Call add_recent_post after successful send**

In `post_once.py`, find the block (around lines 198–203):

```python
        if send.returncode == 0:
            mark_post_topic_status(
                str(topic.get("id") or ""),
                "used",
                topic_extra_update(record["status"], record["time_beijing"], dry_run=False),
            )
```

Replace with:

```python
        if send.returncode == 0:
            mark_post_topic_status(
                str(topic.get("id") or ""),
                "used",
                topic_extra_update(record["status"], record["time_beijing"], dry_run=False),
            )
            add_recent_post(record["post_text"], str(topic.get("type", "")))
```

- [ ] **Step 3: Verify import works**

```bash
python3 -c "from post_once import main; print('import ok')"
```

Expected: `import ok`

- [ ] **Step 4: Commit**

```bash
git add post_once.py
git commit -m "feat(persona): record sent post to persona recent_posts"
```

---

### Task 7: Add /event command to bot_daemon.py

**Files:**
- Modify: `bot_daemon.py` — import + branch in handle_command

- [ ] **Step 1: Add import**

In `bot_daemon.py`, after the existing imports (after line 32), add:

```python
from persona_store import add_event as persona_add_event
```

The bottom of the imports block should now look like:

```python
from learning_store import learning_counts, top_learning_posts
from persona_store import add_event as persona_add_event
```

- [ ] **Step 2: Add /event branch in handle_command**

In `handle_command`, after the `/learn_once` block (after line 416) and before the final `return` (line 418), add:

```python
    if command.startswith("/event"):
        body = stripped[len("/event"):].strip()
        if not body:
            _safe_notify("⚠️ 用法：/event <事件描述>，例如：/event 今天和朋友聊了关于XX的事")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label
        try:
            evt = persona_add_event(body)
            _safe_notify(f"✅ 已记录事件\n\n📅 {evt['timestamp']}\n📝 {evt['raw']}")
        except Exception as exc:
            log(f"persona_add_event failed: {exc}")
            _safe_notify(f"❌ 记录失败：{exc}")
        return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label

```

The full end of `handle_command` should now look like:

```python
    if command.startswith("/learn_once"):
        if run_proc and run_proc.poll() is None:
            _safe_notify("⏳ 当前已有任务在执行。")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label
        _safe_notify("👀 观察学习\n\n✅ 已收到 /learn_once，开始执行。")
        return start_job("observe_feed.py", "telegram"), next_run_at, next_post_run_at, next_learn_at, "telegram", "observe_feed.py"

    if command.startswith("/event"):
        body = stripped[len("/event"):].strip()
        if not body:
            _safe_notify("⚠️ 用法：/event <事件描述>，例如：/event 今天和朋友聊了关于XX的事")
            return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label
        try:
            evt = persona_add_event(body)
            _safe_notify(f"✅ 已记录事件\n\n📅 {evt['timestamp']}\n📝 {evt['raw']}")
        except Exception as exc:
            log(f"persona_add_event failed: {exc}")
            _safe_notify(f"❌ 记录失败：{exc}")
        return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label

    return run_proc, next_run_at, next_post_run_at, next_learn_at, run_trigger, active_label
```

- [ ] **Step 3: Verify import works**

```bash
python3 -c "from bot_daemon import handle_command; print('import ok')"
```

Expected: `import ok`

- [ ] **Step 4: Commit**

```bash
git add bot_daemon.py
git commit -m "feat(persona): add /event Telegram command to bot_daemon"
```

---

### Task 8: Add /event to sync_tg_commands.py

**Files:**
- Modify: `sync_tg_commands.py:13-21`

- [ ] **Step 1: Add command entry**

In `sync_tg_commands.py`, replace the `COMMANDS` list (lines 13–21):

```python
COMMANDS = [
    {"command": "run", "description": "立即跑一轮回复"},
    {"command": "status", "description": "查看回复机器人状态"},
    {"command": "post_once", "description": "立即主动发帖"},
    {"command": "post_dry_run", "description": "生成主动发帖草稿"},
    {"command": "post_status", "description": "查看主动发帖状态"},
    {"command": "learn_once", "description": "立即观察学习一轮"},
    {"command": "learn_status", "description": "查看观察学习状态"},
]
```

Replace with:

```python
COMMANDS = [
    {"command": "run", "description": "立即跑一轮回复"},
    {"command": "status", "description": "查看回复机器人状态"},
    {"command": "post_once", "description": "立即主动发帖"},
    {"command": "post_dry_run", "description": "生成主动发帖草稿"},
    {"command": "post_status", "description": "查看主动发帖状态"},
    {"command": "learn_once", "description": "立即观察学习一轮"},
    {"command": "learn_status", "description": "查看观察学习状态"},
    {"command": "event", "description": "记录一件近期发生的事，供发帖时参考"},
]
```

- [ ] **Step 2: Verify the list is valid**

```bash
python3 -c "from sync_tg_commands import COMMANDS; print([c['command'] for c in COMMANDS])"
```

Expected: `['run', 'status', 'post_once', 'post_dry_run', 'post_status', 'learn_once', 'learn_status', 'event']`

- [ ] **Step 3: Commit**

```bash
git add sync_tg_commands.py
git commit -m "feat(persona): add /event to Telegram command list"
```

- [ ] **Step 4: Push command list to Telegram (run on production host)**

```bash
python3 sync_tg_commands.py
```

Expected: JSON output showing `"event"` in the commands list, `"ok": true` in results.

---

### Post-implementation checklist

- [ ] Copy `state/persona_template.json` → `state/persona.json` on the production host and fill in the `static` block with real details.
- [ ] Send `/event 今天测试人设系统，确认工作正常` via Telegram; confirm bot replies with the recorded event.
- [ ] Run `python3 post_once.py --dry-run` and check that `post_text` in the output no longer has fabricated "上周" time references (or that any time reference in the text corresponds to a recorded event).
- [ ] After a real post succeeds, verify `state/persona.json` has a new entry in `recent_posts`.
