# Persona-First Autonomous Posting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the manual topic queue with LLM-driven autonomous topic selection so the bot posts as a consistent persona without human topic management.

**Architecture:** A new `topic_auto.py` module generates a topic dict by asking the LLM (as the persona) what it wants to say today, given its static background, recent events, and recent posts. `post_once.py` falls through to this auto-generator when the manual queue is empty. All downstream generation/send logic is unchanged.

**Tech Stack:** Python 3 stdlib + existing `chat_json_result` from `common.py`, `get_generation_context` from `persona_store.py`, `normalize_post_topic` from `common.py`.

---

### Task 1: Write real persona static block to state/persona.json

**Files:**
- Modify: `state/persona.json`

- [ ] **Step 1: Write the real static block**

Replace `state/persona.json` entirely with this content (preserving existing `events` and `recent_posts` arrays):

```python
# Run from repo root
python3 -c "
import json
from pathlib import Path

path = Path('state/persona.json')
data = json.loads(path.read_text(encoding='utf-8'))
data['static'] = {
    'background': '在公司打工，同时在折腾副业、自媒体和 Web3。vibe coder，主力工具是 Claude / Codex / Gemini，写代码靠 AI 推着走，想法靠自己。想挣点属于自己的钱，还没挣到，但一直在试。',
    'content_pillars': [
        'AI 工具体验：Claude、Codex、Gemini 的真实使用感受、吐槽、新发现，用过才有发言权',
        'Vibe coding：AI 辅助写代码的节奏、踩坑、意外好用的地方',
        '副业与自媒体：主业之外折腾产品和内容的过程，包括卡点、小进展、没进展',
        'Web3 观察：对链上动态、新项目、市场的直接感受，不分析只说看到什么',
        '打工人张力：上班和想出去之间的状态，偶尔一条，不多'
    ],
    'voice': '直接，偶尔有点丧但不绝望，不说教，不总结大道理，说具体的事',
    'current_projects': [
        '在用 AI 工具做一个自己想用的小产品，方向还在摸索',
        '经营 X 账号，在试自媒体这条路'
    ],
    'recurring_people': []
}
path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
print('done')
"
```

- [ ] **Step 2: Verify**

```bash
python3 -c "
import json
data = json.load(open('state/persona.json'))
s = data['static']
print('pillars:', len(s['content_pillars']))
print('voice:', s['voice'])
print('events preserved:', len(data['events']))
print('recent_posts preserved:', len(data['recent_posts']))
"
```

Expected:
```
pillars: 5
voice: 直接，偶尔有点丧但不绝望，不说教，不总结大道理，说具体的事
events preserved: 1
recent_posts preserved: 1
```

- [ ] **Step 3: Commit**

```bash
git add -f state/persona.json
git commit -m "feat(persona): write real persona static block — vibe coder / side project / web3"
```

---

### Task 2: Create topic_auto.py

**Files:**
- Create: `topic_auto.py`

- [ ] **Step 1: Create the file**

Create `/Users/Zhuanz/Documents/x-reply-bot/topic_auto.py`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime

from common import chat_json_result, normalize_post_topic
from persona_store import get_generation_context

AUTO_TOPIC_PROMPT = """你是一个真实 X 账号的主人，正在决定今天想发什么。

输出严格 JSON：
{"type":"casual|argument|story|news_react", "text":"...", "angle":"...", "reason":"..."}

选题规则：
- 从 persona_context.static.content_pillars 里选一个最近帖子（recent_posts）覆盖较少的方向。
- 如果 recent_events 不为空：从事件里找灵感，不要复述，而是"因为这件事想到了什么"。
- 如果 recent_events 为空：从 background 和 current_projects 推断这个人最近在想什么。
- 禁止选最近三条帖子已经说过的角度或相近主题。
- 禁止泛泛大话题，必须有一个具体切入点。
- type 与内容匹配：随手感受用 casual，有论点用 argument，有故事用 story，有新闻用 news_react。
- text：选题描述，一句话，50 字以内，说清楚想说什么。
- angle：具体切入角度，20 字以内。
- reason：为什么现在想说这个，20 字以内。
- 全部用中文。
- 说话风格遵循 persona_context.static.voice。
"""


def generate_auto_topic() -> dict:
    ctx = get_generation_context()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    result = chat_json_result(
        [
            {"role": "system", "content": AUTO_TOPIC_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "today": today,
                        "persona_context": ctx,
                        "task": "决定今天最想发什么，输出一个选题",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ],
        temperature=0.9,
        max_tokens=300,
    )
    payload = result["payload"]
    now_stamp = datetime.now().astimezone().strftime("%Y%m%d%H%M%S")
    raw_text = str(payload.get("text") or "").strip()
    topic = normalize_post_topic(
        {
            "id": f"auto-{now_stamp}",
            "type": str(payload.get("type") or "argument").strip(),
            "text": raw_text,
            "angle": str(payload.get("angle") or "").strip(),
            "source": "auto",
            "status": "pending",
            "subject": "",
            "event_or_context": "",
            "stance": raw_text,
            "evidence_hint": "",
        }
    )
    if not topic.get("text"):
        raise RuntimeError(f"Auto-topic returned empty text: {payload}")
    return topic
```

- [ ] **Step 2: Smoke-test**

```bash
python3 -c "
from common import load_env_file
load_env_file()
from topic_auto import generate_auto_topic
topic = generate_auto_topic()
print('type:', topic['type'])
print('text:', topic['text'])
print('angle:', topic.get('angle', ''))
print('source:', topic['source'])
assert topic['source'] == 'auto'
assert topic['text']
print('ok')
"
```

Expected: prints a type, a non-empty text, and `ok`. No exceptions.

- [ ] **Step 3: Commit**

```bash
git add topic_auto.py
git commit -m "feat(persona): add topic_auto.py — LLM-driven autonomous topic generation"
```

---

### Task 3: Wire generate_auto_topic into post_once.py

**Files:**
- Modify: `post_once.py:24-25` (imports) and `post_once.py:111-121` (no_pending_topic branch)

- [ ] **Step 1: Add import**

In `post_once.py`, after `from post_generate import generate_post_plan`, add:

```python
from topic_auto import generate_auto_topic
```

The imports block bottom should now read:

```python
from post_generate import generate_post_plan
from topic_auto import generate_auto_topic
from persona_store import add_recent_post
```

- [ ] **Step 2: Replace no_pending_topic branch**

Find this block (lines 111–121):

```python
        topic = next_pending_post_topic()
        if not topic:
            payload = {
                "time_beijing": started.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "trigger": args.trigger,
                "dry_run": args.dry_run,
                "status": "no_pending_topic",
            }
            write_json(LATEST_POST_RUN_PATH, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 2
```

Replace with:

```python
        topic = next_pending_post_topic()
        if not topic:
            try:
                topic = generate_auto_topic()
            except Exception as exc:
                payload = {
                    "time_beijing": started.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    "trigger": args.trigger,
                    "dry_run": args.dry_run,
                    "status": "auto_topic_failed",
                    "error": str(exc),
                }
                write_json(LATEST_POST_RUN_PATH, payload)
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 1
```

- [ ] **Step 3: Verify import and dry-run**

```bash
python3 -c "from post_once import main; print('import ok')"
```

Expected: `import ok`

Then run a dry-run (requires `.env` and connectivity to verify topic generation works end-to-end):

```bash
# First ensure queue is empty so auto-topic fires
python3 -c "
from common import load_env_file, load_post_topics
load_env_file()
data = load_post_topics()
pending = [t for t in data['topics'] if t.get('status') == 'pending']
print('pending topics:', len(pending))
"
```

If pending is 0, run:

```bash
PATH="$HOME/.local/bin:$PATH" python3 post_once.py --dry-run --trigger manual
```

Then read the result:

```bash
python3 -c "
import json
d = json.load(open('state/latest_post_run.json'))
print('status:', d.get('status'))
print('topic_source:', d.get('topic_source'))
print('post_text:', d.get('post_text', '')[:80])
"
```

Expected:
```
status: dry_run_ready
topic_source: auto
post_text: <non-empty Chinese text>
```

- [ ] **Step 4: Commit**

```bash
git add post_once.py
git commit -m "feat(persona): wire auto topic generation into post_once — queue-empty now self-serves"
```

---

### Post-implementation checklist

- [ ] Confirm `topic_source: auto` appears in a dry-run output (queue empty case)
- [ ] Confirm `topic_source: manual` still appears when a manual topic is pending (add one via `python3 post_topics.py --add "test" --type casual`, run dry-run, verify it uses the manual topic, then remove it)
- [ ] Send one real post with `PATH="$HOME/.local/bin:$PATH" python3 post_once.py --trigger manual` and confirm it posts and records to `recent_posts`
