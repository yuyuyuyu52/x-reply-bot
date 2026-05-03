# Reply Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让回复获得更多粉丝和浏览，方法是选择互动更高的帖子回复，并在回复生成时注入学习数据和账号 persona。

**Architecture:** 两个文件改动，互相独立。`prepare_post.py` 升级 scraping 以抓取互动数据，并重写选帖评分公式。`generate_reply.py` 在 `build_messages()` 里动态叠加三层上下文：学习库规律 + persona + 动态 hook 指令。两个改动都有完整的降级路径（数据抓不到或库为空时，行为退化为现有逻辑）。

**Tech Stack:** Python 3, SQLite (learning_store), JSON state files, browser-harness CDP JS snippets

---

## File Structure

| File | Change |
|------|--------|
| `prepare_post.py` | 新增 `import math`；`score_text()` → `score_candidate()`；`shortlist_candidates()` 传入 engagement 字段；JS snippet 加 engagement 提取；`SELECTION_PROMPT` 加互动优先规则 |
| `generate_reply.py` | 新增 `_build_learning_context()` 和 `_build_persona_context()`；`build_messages()` 动态组装 system prompt |

---

## Task 1: 升级 `prepare_post.py` — engagement scraping

**Files:**
- Modify: `prepare_post.py`

- [ ] **Step 1: 在 `collect_feed_posts()` 的 JS snippet 里加入 engagement 提取**

找到 `prepare_post.py` 里的 `posts = js("""` 这段，替换整个 JS 字符串（从 `posts = js("""` 到 `""") or []`）为：

```python
    posts = js("""
(() => {{
  function parseCount(s) {{
    if (!s) return 0;
    s = (s + '').replace(/,/g, '').trim();
    if (/^\d+(\.\d+)?[Kk]$/.test(s)) return Math.round(parseFloat(s) * 1000);
    if (/^\d+(\.\d+)?[Mm]$/.test(s)) return Math.round(parseFloat(s) * 1000000);
    return parseInt(s, 10) || 0;
  }}
  function countBtn(el, tids) {{
    for (const tid of tids) {{
      const btn = el.querySelector('[data-testid="' + tid + '"]');
      if (!btn) continue;
      for (const sp of btn.querySelectorAll('span')) {{
        const t = (sp.innerText || '').trim();
        if (/^[\d.,KkMm]+$/.test(t) && t) return parseCount(t);
      }}
    }}
    return 0;
  }}
  return Array.from(document.querySelectorAll('article')).map((el, i) => {{
    const text = el.innerText || '';
    const textBlocks = Array.from(el.querySelectorAll('[data-testid="tweetText"]'))
      .map(node => (node.innerText || '').trim())
      .filter(Boolean);
    const links = Array.from(el.querySelectorAll('a[href*="/status/"]'))
      .map(a => a.href)
      .filter(Boolean);
    return {{
      i,
      text,
      main_text: textBlocks[0] || text,
      quoted_post_text: textBlocks[1] || '',
      is_quote_tweet: textBlocks.length > 1,
      links,
      replies: countBtn(el, ['reply']),
      reposts: countBtn(el, ['retweet', 'unretweet']),
      likes: countBtn(el, ['like', 'unlike']),
      views: countBtn(el, ['analyticsButton'])
    }};
  }});
}})()
""") or []
```

- [ ] **Step 2: 在文件顶部加 `import math`**

在 `prepare_post.py` 第 7 行（`import json` 之后）加一行：

```python
import math
```

- [ ] **Step 3: 把 `score_text()` 替换成 `score_candidate()`**

删除现有的 `score_text` 函数（整个函数体），替换为：

```python
def score_candidate(candidate: dict) -> float:
    text = (candidate.get("text") or "")
    lowered = text.lower()
    score = min(len(text.strip()), 900) / 900
    for kw in TECH_KEYWORDS:
        if kw in lowered:
            score += 2
    likes = int(candidate.get("likes") or 0)
    replies = int(candidate.get("replies") or 0)
    reposts = int(candidate.get("reposts") or 0)
    views = int(candidate.get("views") or 0)
    engagement_bonus = math.log10(1 + likes * 2 + replies * 3 + reposts * 2 + views / 1000)
    return round(score + engagement_bonus, 3)
```

- [ ] **Step 4: 更新 `shortlist_candidates()` 里的 candidate 构造逻辑**

在 `shortlist_candidates()` 函数里，找到这段代码：

```python
        candidate = {
            "url": url,
            "text": text[:1800],
            "quoted_post_text": str(post.get("quoted_post_text") or "").strip()[:1200],
            "is_quote_tweet": bool(post.get("is_quote_tweet")),
            "score": round(score_text(text), 3),
        }
```

替换为：

```python
        candidate = {
            "url": url,
            "text": text[:1800],
            "quoted_post_text": str(post.get("quoted_post_text") or "").strip()[:1200],
            "is_quote_tweet": bool(post.get("is_quote_tweet")),
            "likes": int(post.get("likes") or 0),
            "replies": int(post.get("replies") or 0),
            "reposts": int(post.get("reposts") or 0),
            "views": int(post.get("views") or 0),
            "score": 0.0,
        }
        candidate["score"] = score_candidate(candidate)
```

- [ ] **Step 5: 验证语法正确**

```bash
cd /path/to/x-reply-bot && python3 -c "import prepare_post; print('OK')"
```

期望输出：`OK`（无 ImportError / SyntaxError）

- [ ] **Step 6: Commit**

```bash
git add prepare_post.py
git commit -m "feat(reply): scrape engagement metrics and use in candidate scoring"
```

---

## Task 2: 升级 `prepare_post.py` — AI 选帖 prompt

**Files:**
- Modify: `prepare_post.py`

- [ ] **Step 1: 在 `SELECTION_PROMPT` 里加互动优先规则**

找到 `SELECTION_PROMPT` 里这一行：

```python
- Prefer posts that can receive a short public reply without sounding forced.
```

在它之后加一行（保持同样缩进）：

```python
- Give extra preference to posts with high engagement (likes, replies, reposts) — these are actively resonating and replies will get more eyeballs.
```

- [ ] **Step 2: 验证 candidate JSON 包含 engagement 字段**

`choose_candidate_with_ai` 接收的是 `shortlist_candidates()` 返回的 list，现在每个候选已经有 `likes`、`replies`、`reposts`、`views` 字段，AI 会自动看到。不需要额外改动。

确认方式（在有 Chrome CDP 的机器上运行）：

```bash
python3 prepare_post.py 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
cands = d.get('selection_candidates', [])
print('candidates:', len(cands))
if cands:
    print('first candidate keys:', list(cands[0].keys()))
    print('first likes/replies:', cands[0].get('likes'), cands[0].get('replies'))
"
```

期望输出包含 `likes` 和 `replies` 字段（数字，可能为 0 如果 X.com DOM 没有暴露）。

- [ ] **Step 3: Commit**

```bash
git add prepare_post.py
git commit -m "feat(reply): prefer high-engagement posts in AI selection prompt"
```

---

## Task 3: 三层 prompt 注入 `generate_reply.py`

**Files:**
- Modify: `generate_reply.py`

- [ ] **Step 1: 添加 import**

在 `generate_reply.py` 的 import 块（`from common import ...` 之后）加：

```python
from learning_store import recent_learning_references
from persona_store import get_generation_context
```

- [ ] **Step 2: 添加 `_build_learning_context()` 辅助函数**

在 `DEFAULT_PROMPT` 常量定义之后、`build_messages()` 之前插入：

```python
def _build_learning_context() -> str:
    try:
        refs = recent_learning_references(limit=4)
        if not refs:
            return ""
        lines = ["【近期高互动帖规律】"]
        for r in refs:
            hook = (r.get("hook_type") or "").strip()
            why = (r.get("why_it_works") or "").strip()
            if hook and why:
                lines.append(f"- {hook} → {why}")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)[:400]
    except Exception:
        return ""
```

- [ ] **Step 3: 添加 `_build_persona_context()` 辅助函数**

紧接着 `_build_learning_context()` 之后插入：

```python
def _build_persona_context() -> str:
    try:
        ctx = get_generation_context()
        static = ctx.get("static") or {}
        if not static:
            return ""
        parts = []
        for k, v in list(static.items())[:6]:
            if isinstance(v, str) and v.strip():
                parts.append(f"{k}: {v.strip()}")
        recent_posts = ctx.get("recent_posts") or []
        if recent_posts:
            samples = [p["text"][:60] for p in recent_posts[-3:] if p.get("text")]
            if samples:
                parts.append("近期发帖: " + " / ".join(samples))
        if not parts:
            return ""
        return ("【账号人设】\n" + "\n".join(parts))[:200]
    except Exception:
        return ""
```

- [ ] **Step 4: 替换 `build_messages()` 函数体**

把整个 `build_messages` 函数替换为：

```python
def build_messages(post: dict, system_prompt: str) -> list[dict]:
    learning_ctx = _build_learning_context()
    persona_ctx = _build_persona_context()

    system_parts = [system_prompt]
    if learning_ctx:
        system_parts.append(learning_ctx)
    if persona_ctx:
        system_parts.append(persona_ctx)
    if learning_ctx:
        system_parts.append(
            "根据这条帖子的话题，优先使用上面规律中效果最好的 hook 类型开头，而不是写通用答案。"
        )

    full_system = "\n\n".join(system_parts)

    post_text = (post.get("main_post_text") or "").strip()
    quoted_post_text = (post.get("quoted_post_text") or "").strip()
    url = (post.get("url") or "").strip()
    quote_note = ""
    if quoted_post_text:
        quote_note = (
            "\n\nThis post quotes another post.\n"
            "Reply target: the main post author and main post text.\n"
            "Quoted post: context only. Do not write as if you are replying to the quoted author.\n\n"
            "Quoted post text:\n"
            f"{quoted_post_text}"
        )
    return [
        {"role": "system", "content": full_system},
        {
            "role": "user",
            "content": (
                "Write a reply for this X post.\n\n"
                f"URL: {url}\n\n"
                "Main post (this is the post you are replying to):\n"
                f"{post_text}"
                f"{quote_note}"
            ),
        },
    ]
```

- [ ] **Step 5: 验证语法和降级路径**

```bash
cd /path/to/x-reply-bot && python3 -c "import generate_reply; print('OK')"
```

期望：`OK`

如果学习库为空，验证不报错：

```bash
python3 -c "
from generate_reply import _build_learning_context, _build_persona_context
print('learning:', repr(_build_learning_context()))
print('persona:', repr(_build_persona_context()))
"
```

期望：两者各打印字符串（可能是空字符串 `''`，不报错）。

- [ ] **Step 6: 端到端验证（需要 Chrome CDP）**

```bash
python3 generate_reply.py --text-only 2>&1
```

期望：打印一条回复文本（或 "No prepared post found" 如果没有先运行 prepare_post.py）。无 ImportError / AttributeError。

- [ ] **Step 7: Commit**

```bash
git add generate_reply.py
git commit -m "feat(reply): inject learning context and persona into reply prompt"
```

---

## Self-Review Notes

- Task 1 步骤 4 里的 `score_candidate` 名字在 Task 1 步骤 3 定义，Task 1 步骤 4 调用 — 一致 ✓
- `recent_learning_references` 在 `learning_store.py:239` 已存在，接受 `limit` 参数 ✓
- `get_generation_context` 在 `persona_store.py:76` 已存在 ✓
- 两个辅助函数都有 `except Exception: return ""` 降级 ✓
- `score_text` 没有被其他文件引用（仅 `prepare_post.py` 内部使用）✓
- engagement 字段加入 candidate dict 后，`choose_candidate_with_ai` 会把它们原样传给 LLM，与 prompt 升级配合 ✓
