#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import textwrap
from datetime import datetime, timezone

from common import (
    REPLIED_PATH,
    RUN_LOG_PATH,
    SCREENSHOT_DIR,
    append_log,
    ensure_state_dirs,
    load_json,
    run_harness,
    write_json,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reply", required=True)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    reply = args.reply.strip()
    url = args.url.strip()
    if not reply:
        print("ERROR: empty reply")
        return 2
    if not url:
        print("ERROR: empty url")
        return 2
    if len(reply) > 240:
        print("ERROR: reply too long; keep it short and human")
        return 2

    ensure_state_dirs()

    ready_shot = SCREENSHOT_DIR / "x_reply_ready.png"
    posted_shot = SCREENSHOT_DIR / "x_reply_posted.png"
    code = f"""
import json
url = {json.dumps(url)}
reply_text = {json.dumps(reply, ensure_ascii=False)}
ready_shot = {json.dumps(str(ready_shot))}
posted_shot = {json.dumps(str(posted_shot))}

tabs = list_tabs(include_chrome=False)
x_tab = None
for t in tabs:
    if t.get('url', '').startswith(url):
        x_tab = t
        break
if not x_tab:
    for t in tabs:
        if 'x.com' in t.get('url', ''):
            x_tab = t
            break
if x_tab:
    switch_tab(x_tab['targetId'])
else:
    fresh_tab = new_tab(url)
    switch_tab(fresh_tab)
info_before = page_info()
if info_before.get('dialog') or not (info_before.get('url') or '').startswith(url):
    js('window.onbeforeunload = null')
    goto_url(url)
    wait_for_load(20)
    wait(4)
    info_before = page_info()
if not (info_before.get('url') or '').startswith(url):
    print(json.dumps({{'ok': False, 'reason': 'wrong_url', 'page_info': info_before}}, ensure_ascii=False))
else:
    focused = js('''
(() => {{
  const el = document.querySelector('[data-testid="tweetTextarea_0"]');
  if (!el) return {{ok:false, reason:'no textarea'}};
  el.scrollIntoView({{block:'center'}});
  el.focus();
  const r = el.getBoundingClientRect();
  return {{ok:true, x:r.x, y:r.y, w:r.width, h:r.height}};
}})()
''')
    if not focused or not focused.get('ok'):
        print(json.dumps({{'ok': False, 'reason': 'focus_failed', 'focus': focused, 'page_info': page_info()}}, ensure_ascii=False))
    else:
        pos = js('''
(() => {{
  const el = document.querySelector('[data-testid="tweetTextarea_0"]');
  const r = el.getBoundingClientRect();
  return {{x:r.left + Math.min(80, r.width / 2), y:r.top + r.height / 2}};
}})()
''')
        click_at_xy(pos['x'], pos['y'])
        wait(0.5)
        type_text(reply_text)
        wait(1)
        composer = js('''
(() => {{
  const el = document.querySelector('[data-testid="tweetTextarea_0"]');
  return el ? el.innerText : '';
}})()
''') or ''
        capture_screenshot(ready_shot)
        if reply_text not in composer:
            print(json.dumps({{'ok': False, 'reason': 'composer_mismatch', 'composer': composer}}, ensure_ascii=False))
        else:
            clicked = js('''
(() => {{
  const btn = document.querySelector('[data-testid="tweetButtonInline"]') || document.querySelector('[data-testid="tweetButton"]');
  if (!btn) return {{ok:false, reason:'no button'}};
  const disabled = btn.disabled || btn.getAttribute('aria-disabled') === 'true';
  if (disabled) return {{ok:false, reason:'disabled'}};
  btn.click();
  return {{ok:true}};
}})()
''')
            wait(6)
            body = js('document.body.innerText') or ''
            capture_screenshot(posted_shot)
            ok = ('你的帖子已发送' in body) or (reply_text in body)
            print(json.dumps({{
                'ok': ok,
                'url': url,
                'reply': reply_text,
                'click_result': clicked,
                'body_start': body[:1200],
                'page_info': page_info()
            }}, ensure_ascii=False, indent=2))
"""
    try:
        stdout = run_harness(textwrap.dedent(code))
        ok = '"ok": true' in stdout or "'ok': True" in stdout
        if ok:
            replied = load_json(REPLIED_PATH, {"posts": []})
            posts = replied.setdefault("posts", [])
            if url not in posts:
                posts.append(url)
                # Cap to the most recent 2000 entries — old replies
                # never recur in feed, so unbounded growth is just bloat.
                if len(posts) > 2000:
                    replied["posts"] = posts[-2000:]
                write_json(REPLIED_PATH, replied)
        append_log(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "status": "success" if ok else "uncertain",
                "url": url,
                "reply": reply,
            }
        )
        print(stdout)
        return 0 if ok else 1
    except Exception as exc:
        append_log(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "status": "exception",
                "url": url,
                "reply": reply,
                "error": str(exc),
            }
        )
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
