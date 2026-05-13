#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import textwrap
from datetime import datetime, timezone

from src.harness import (
    harness_navigate_snippet,
    harness_compose_and_send_snippet,
)
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
    parser.add_argument("--like", action="store_true")
    parser.add_argument("--max-len", type=int, default=240)
    parser.add_argument("--return-reply-url", action="store_true")
    args = parser.parse_args()
    reply = args.reply.strip()
    url = args.url.strip()
    action = args.action.strip()
    do_like = args.like
    max_len = args.max_len
    return_reply_url = args.return_reply_url
    if action in ["reply", "quote"] and not reply:
        logger.error("empty text for action=%s", action)
        return 2
    if not url:
        logger.error("empty url")
        return 2
    if len(reply) > max_len:
        logger.error("reply too long; keep it short and human (%d > %d)", len(reply), max_len)
        return 2

    ensure_state_dirs()

    ready_shot = SCREENSHOT_DIR / "x_reply_ready.png"
    posted_shot = SCREENSHOT_DIR / "x_reply_posted.png"

    nav_snippet = harness_navigate_snippet("url")
    compose_reply = textwrap.indent(
        harness_compose_and_send_snippet(text_var="reply_text", button_order="inline_first"),
        "        ",
    )
    compose_quote = textwrap.indent(
        harness_compose_and_send_snippet(text_var="reply_text", button_order="button_first"),
        "            ",
    )

    # ---- Build like block ----
    if do_like:
        like_block = """
    like_result = js('''
(() => {
  const btn = document.querySelector('[data-testid="like"]');
  if (!btn) return {ok:false, reason:'no like button'};
  const label = (btn.getAttribute('aria-label') || '');
  if (label.includes('取消喜欢') || label.includes('Unlike') || btn.getAttribute('aria-pressed') === 'true') {
    return {ok:true, action:'already_liked'};
  }
  btn.click();
  return {ok:true, action:'liked'};
})()
''')
"""
    else:
        like_block = ""

    code = f"""
import json
url = {json.dumps(url)}
reply_text = {json.dumps(reply, ensure_ascii=False)}
action = {json.dumps(action)}
return_reply_url = {return_reply_url!r}
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
            body_after = js('document.body.innerText')
            ok = clicked_confirm and clicked_confirm.get('ok')
            if not ok:
                ok = ('已转发' in body_after) or ('Reposted' in body_after) or ('Undo Repost' in body_after)
            print(json.dumps({{
                'ok': ok,
                'url': url,
                'action': action,
                'reply': reply_text,
                'click_result': clicked_confirm,
                'body_start': body_after[:1200],
                'page_info': page_info()
            }}, ensure_ascii=False, indent=2))
        else: # action == "quote"
            clicked_quote = js('''
(() => {{
  const items = Array.from(document.querySelectorAll('[role="menuitem"]'));
  let btn = items.find(el => {{
    const text = (el.innerText || '').trim();
    const href = (el.getAttribute('href') || '');
    return text.includes('引用') || text.includes('Quote') || text.includes('引用ツイート') || href.includes('/compose/tweet') || href.includes('/intent/tweet');
  }});
  if (!btn) {{
    // Fallback: pick the first menuitem that has an href pointing to compose
    btn = items.find(el => (el.getAttribute('href') || '').includes('/compose/'));
  }}
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
{like_block}
    reply_url = ''
    if ok and return_reply_url:
        profile_url = js('''
document.querySelector('a[data-testid="AppTabBar_Profile_Link"]')?.href || ''
''')
        if profile_url:
            js('window.onbeforeunload = null')
            goto_url(profile_url)
            wait_for_load(20)
            wait(4)
            reply_url = js('''
(() => {{
  const snippet = %s;
  const articles = Array.from(document.querySelectorAll('article'));
  function getLinks(article) {{
    return Array.from(article.querySelectorAll('a[href*="/status/"]'))
      .map((link) => link.href || '')
      .filter(Boolean)
      .map((href) => href.replace(/\\/analytics$/, ''))
      .filter((href) => /\\/status\\/\\d+$/.test(href));
  }}
  if (articles.length && (articles[0].innerText || '').includes(snippet)) {{
    const links = getLinks(articles[0]);
    if (links.length) {{ links.sort((a, b) => a.length - b.length); return links[0]; }}
  }}
  for (const article of articles.slice(0, 10)) {{
    if ((article.innerText || '').includes(snippet)) {{
      const links = getLinks(article);
      if (links.length) {{ links.sort((a, b) => a.length - b.length); return links[0]; }}
    }}
  }}
  return '';
}})()
''' % json.dumps(reply[:40], ensure_ascii=False)) or ''
            if not reply_url:
                wait(3)
                reply_url = js('''
(() => {{
  const snippet = %s;
  const articles = Array.from(document.querySelectorAll('article'));
  function getLinks(article) {{
    return Array.from(article.querySelectorAll('a[href*="/status/"]'))
      .map((link) => link.href || '')
      .filter(Boolean)
      .map((href) => href.replace(/\\/analytics$/, ''))
      .filter((href) => /\\/status\\/\\d+$/.test(href));
  }}
  if (articles.length && (articles[0].innerText || '').includes(snippet)) {{
    const links = getLinks(articles[0]);
    if (links.length) {{ links.sort((a, b) => a.length - b.length); return links[0]; }}
  }}
  for (const article of articles.slice(0, 10)) {{
    if ((article.innerText || '').includes(snippet)) {{
      const links = getLinks(article);
      if (links.length) {{ links.sort((a, b) => a.length - b.length); return links[0]; }}
    }}
  }}
  return '';
}})()
''' % json.dumps(reply[:40], ensure_ascii=False)) or ''
    like_ok = like_result.get('ok') if 'like_result' in dir() else None
    if like_ok is not None:
        print(json.dumps({{'like': like_result}}, ensure_ascii=False))
    if reply_url:
        print(f"REPLY_URL: {{reply_url}}")
"""
    try:
        stdout = run_harness(textwrap.dedent(code))
        ok = '"ok": true' in stdout or "'ok': True" in stdout
        if ok:
            # Thread self-replies should not be deduped (they are our own posts)
            if not return_reply_url:
                replied = load_json(REPLIED_PATH, {"posts": []})
                posts = replied.setdefault("posts", [])
                if url not in posts:
                    posts.append(url)
                    if len(posts) > 2000:
                        replied["posts"] = posts[-2000:]
                    write_json(REPLIED_PATH, replied)
        append_log(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "status": "success" if ok else "uncertain",
                "action": action,
                "url": url,
                "reply": reply,
                "like": do_like,
            }
        )
        # Extract reply_url if in thread mode
        reply_url = ""
        for line in stdout.splitlines():
            if line.startswith("REPLY_URL: "):
                reply_url = line[len("REPLY_URL: "):].strip()
        if reply_url:
            print(f"REPLY_URL: {reply_url}")
        print(stdout)
        return 0 if ok else 1
    except Exception as exc:
        append_log(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "status": "exception",
                "action": action,
                "url": url,
                "reply": reply,
                "like": do_like,
                "error": str(exc),
            }
        )
        logger.error("%s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
