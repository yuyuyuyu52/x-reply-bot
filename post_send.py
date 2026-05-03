#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import textwrap
from datetime import datetime, timezone

from common import SCREENSHOT_DIR, append_log, ensure_state_dirs, load_env_file, run_harness


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True)
    args = parser.parse_args()
    post_text = args.text.strip()
    if not post_text:
        print("ERROR: empty post text")
        return 2
    if len(post_text) > 280:
        print("ERROR: post too long")
        return 2

    ensure_state_dirs()
    ready_shot = SCREENSHOT_DIR / "x_post_ready.png"
    posted_shot = SCREENSHOT_DIR / "x_post_posted.png"
    match_snippet = post_text[:30]
    code = f'''
import json

post_text = {json.dumps(post_text, ensure_ascii=False)}
match_snippet = {json.dumps(match_snippet, ensure_ascii=False)}
ready_shot = {json.dumps(str(ready_shot))}
posted_shot = {json.dumps(str(posted_shot))}

tabs = list_tabs(include_chrome=False)
x_tab = None
for t in tabs:
    tab_url = t.get('url', '')
    if 'x.com/home' in tab_url:
        x_tab = t
        break
if x_tab:
    switch_tab(x_tab['targetId'])
else:
    tid = new_tab('https://x.com/home')
    switch_tab(tid)

current = page_info()
if current.get('dialog'):
    tid = new_tab('https://x.com/home')
    switch_tab(tid)
    current = page_info()
if 'x.com/home' not in (current.get('url') or ''):
    goto_url('https://x.com/home')

wait_for_load(20)
wait(4)

profile_url = js("""
(() => {{
  const a = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
  return a ? a.href : '';
}})()
""") or ''

inline = js("""
(() => {{
  const el = document.querySelector('[data-testid="tweetTextarea_0"]');
  if (!el) return {{ok:false}};
  el.scrollIntoView({{block:'center'}});
  el.focus();
  return {{ok:true}};
}})()
""") or {{}}

if not inline.get('ok'):
    clicked = js("""
(() => {{
  const btn = document.querySelector('[data-testid="SideNav_NewTweet_Button"]');
  if (!btn) return {{ok:false}};
  btn.click();
  return {{ok:true}};
}})()
""") or {{}}
    wait(2)

pos = js("""
(() => {{
  const el = document.querySelector('[data-testid="tweetTextarea_0"]');
  if (!el) return {{ok:false}};
  const r = el.getBoundingClientRect();
  return {{ok:true, x:r.left + Math.min(80, r.width / 2), y:r.top + r.height / 2}};
}})()
""") or {{}}

if not pos.get('ok'):
    print(json.dumps({{'ok': False, 'reason': 'no composer', 'page_info': page_info()}}, ensure_ascii=False))
else:
    click_at_xy(pos['x'], pos['y'])
    wait(0.5)
    type_text(post_text)
    wait(1)
    composer = js("""
(() => {{
  const el = document.querySelector('[data-testid="tweetTextarea_0"]');
  return el ? el.innerText : '';
}})()
""") or ''
    capture_screenshot(ready_shot)
    if post_text not in composer:
        print(json.dumps({{'ok': False, 'reason': 'composer_mismatch', 'composer': composer}}, ensure_ascii=False))
    else:
        clicked = js("""
(() => {{
  const btn = document.querySelector('[data-testid="tweetButton"]') || document.querySelector('[data-testid="tweetButtonInline"]');
  if (!btn) return {{ok:false, reason:'no button'}};
  const disabled = btn.disabled || btn.getAttribute('aria-disabled') === 'true';
  if (disabled) return {{ok:false, reason:'disabled'}};
  btn.click();
  return {{ok:true}};
}})()
""") or {{}}
        wait(6)
        body = js('document.body.innerText') or ''
        capture_screenshot(posted_shot)
        sent_ok = ('你的帖子已发送' in body) or ('Your post was sent' in body)
        posted_url = ''
        if profile_url:
            goto_url(profile_url)
            wait_for_load(20)
            wait(4)
            posted_url = js("""
(() => {{
  // Prefer the timeline's first article: the just-posted tweet is always at
  // the top of the user's own profile timeline. Falling back to a 30-char
  // text-snippet match is unreliable because X truncates timeline tweets to
  // ~100 chars + "Show more", and DOM whitespace can shift the snippet edge.
  const snippet = %s;
  const articles = Array.from(document.querySelectorAll('article'));
  function statusLinks(article) {{
    return Array.from(article.querySelectorAll('a[href*="/status/"]'))
      .map((link) => link.href || '')
      .filter(Boolean)
      .map((href) => href.replace(/\\/analytics$/, ''))
      .filter((href) => /\\/status\\/\\d+$/.test(href));
  }}
  if (articles.length) {{
    const first = statusLinks(articles[0]);
    if (first.length) {{
      first.sort((a, b) => a.length - b.length);
      return first[0];
    }}
  }}
  for (const article of articles) {{
    const text = article.innerText || '';
    if (text.includes(snippet)) {{
      const links = statusLinks(article);
      if (links.length) {{
        links.sort((a, b) => a.length - b.length);
        return links[0];
      }}
    }}
  }}
  return '';
}})()
""" % {json.dumps(match_snippet, ensure_ascii=False)}) or ''
        # Don't echo body (full timeline scrape, ~9 KB, contains other users'
        # posts/ads — privacy + bloat). Persist the boolean we actually use,
        # plus a tiny snippet bounded to the success-marker line for debugging.
        sent_marker = ''
        for marker in ('你的帖子已发送', 'Your post was sent'):
            idx = body.find(marker)
            if idx >= 0:
                sent_marker = body[max(0, idx - 20):idx + len(marker) + 40]
                break
        print(json.dumps({{
            'ok': sent_ok,
            'text': post_text,
            'url': posted_url,
            'profile_url': profile_url,
            'click_result': clicked,
            'sent_marker': sent_marker,
            'page_info': page_info()
        }}, ensure_ascii=False, indent=2))
'''
    try:
        stdout = run_harness(textwrap.dedent(code), timeout=120)
        ok = '"ok": true' in stdout or "'ok': True" in stdout
        append_log(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "status": "success" if ok else "uncertain",
                "post_text": post_text,
            }
        )
        print(stdout)
        return 0 if ok else 1
    except Exception as exc:
        append_log(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "status": "exception",
                "post_text": post_text,
                "error": str(exc),
            }
        )
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
