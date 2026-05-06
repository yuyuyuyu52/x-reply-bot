#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import textwrap
from datetime import datetime, timezone

from src.harness import harness_navigate_snippet, harness_compose_and_send_snippet
from src.common import (
    REPLIED_PATH,
    RUN_LOG_PATH,
    SCREENSHOT_DIR,
    append_log,
    ensure_state_dirs,
    load_json,
    run_harness,
    write_json,
)
from src.logger import get_logger

logger = get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reply", default="")
    parser.add_argument("--url", required=True)
    parser.add_argument("--action", default="reply", choices=["reply", "quote", "repost"])
    args = parser.parse_args()
    reply = args.reply.strip()
    url = args.url.strip()
    action = args.action.strip()
    
    if action in ["reply", "quote"] and not reply:
        logger.error("empty text for action=%s", action)
        return 2
    if not url:
        logger.error("empty url")
        return 2
    if len(reply) > 240:
        logger.error("reply too long; keep it short and human")
        return 2

    ensure_state_dirs()

    ready_shot = SCREENSHOT_DIR / "x_reply_ready.png"
    posted_shot = SCREENSHOT_DIR / "x_reply_posted.png"
    nav_snippet = harness_navigate_snippet('url')
    compose_reply = textwrap.indent(harness_compose_and_send_snippet(text_var='reply_text', button_order='inline_first'), '        ')
    compose_quote = textwrap.indent(harness_compose_and_send_snippet(text_var='reply_text', button_order='button_first'), '            ')

    code = f"""
import json
url = {json.dumps(url)}
reply_text = {json.dumps(reply, ensure_ascii=False)}
action = {json.dumps(action)}
ready_shot = {json.dumps(str(ready_shot))}
posted_shot = {json.dumps(str(posted_shot))}

{nav_snippet}
if not (info_before.get('url') or '').startswith(url):
    print(json.dumps({{'ok': False, 'reason': 'wrong_url', 'page_info': info_before}}, ensure_ascii=False))
else:
    if action == "reply":
{compose_reply}
        capture_screenshot(posted_shot)
        ok = ('你的帖子已发送' in body) or (reply_text in body)
        print(json.dumps({{
            'ok': ok,
            'url': url,
            'action': action,
            'reply': reply_text,
            'click_result': clicked,
            'body_start': body[:1200],
            'page_info': page_info()
        }}, ensure_ascii=False, indent=2))
    else:
        # Quote or Repost
        clicked_retweet = js('''
(() => {{
  const btn = document.querySelector('[data-testid="retweet"]');
  if (!btn) return {{ok:false, reason:'no retweet button'}};
  btn.click();
  return {{ok:true}};
}})()
''')
        wait(1)
        
        if action == "repost":
            clicked_confirm = js('''
(() => {{
  const btn = document.querySelector('[data-testid="retweetConfirm"]');
  if (!btn) return {{ok:false, reason:'no confirm button'}};
  btn.click();
  return {{ok:true}};
}})()
''')
            wait(2)
            capture_screenshot(posted_shot)
            ok = clicked_confirm and clicked_confirm.get('ok')
            print(json.dumps({{
                'ok': ok,
                'url': url,
                'action': action,
                'reply': reply_text,
                'click_result': clicked_confirm,
                'page_info': page_info()
            }}, ensure_ascii=False, indent=2))
        else: # action == "quote"
            clicked_quote = js('''
(() => {{
  const btn = Array.from(document.querySelectorAll('[role="menuitem"]')).find(el => el.innerText.includes('引用') || el.innerText.includes('Quote') || el.href?.includes('/compose/tweet'));
  if (!btn) return {{ok:false, reason:'no quote option'}};
  btn.click();
  return {{ok:true}};
}})()
''')
            wait(2)
{compose_quote}
            capture_screenshot(posted_shot)
            ok = ('你的帖子已发送' in body) or (reply_text in body)
            print(json.dumps({{
                'ok': ok,
                'url': url,
                'action': action,
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
        logger.error("%s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
