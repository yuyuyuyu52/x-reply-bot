#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import textwrap
from datetime import datetime, timezone

from src.harness import harness_navigate_snippet, harness_compose_and_send_snippet, harness_upload_image_snippet
from src.common import SCREENSHOT_DIR, append_log, ensure_state_dirs, load_env_file, run_harness
from src.image_search import search_image, download_image, image_to_base64, cleanup_temp_image
from src.logger import get_logger

logger = get_logger(__name__)


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True)
    parser.add_argument("--image-query", default="")
    args = parser.parse_args()
    post_text = args.text.strip()
    image_query = args.image_query.strip()
    if not post_text:
        logger.error("empty post text")
        return 2
    if len(post_text) > 280:
        logger.error("post too long")
        return 2

    # ---- Image search & download ----
    img_base64 = ""
    img_mime = ""
    image_info = {}
    if image_query:
        logger.info("image search query=%s", image_query)
        result = search_image(image_query)
        if result:
            image_info = {
                "source": result.get("source", ""),
                "cost_cny": float(result.get("cost_cny") or 0),
                "query": image_query,
            }
            logger.info("image found source=%s cost=%.4f", result.get("source"), image_info["cost_cny"])

            b64_direct = result.get("b64_json", "")
            if b64_direct:
                img_base64 = b64_direct
                img_mime = "image/png"
                logger.info("image ready via b64_json len=%d", len(img_base64))
            else:
                image_url = result.get("url", "")
                downloaded = download_image(image_url)
                if downloaded:
                    path, mime = downloaded
                    img_base64, img_mime = image_to_base64(path)
                    cleanup_temp_image(path)
                    logger.info("image ready mime=%s bytes=%d", img_mime, len(img_base64))
                else:
                    logger.warning("image download failed, proceeding without image")
        else:
            logger.warning("no image found for query=%s, proceeding without image")

    ensure_state_dirs()
    ready_shot = SCREENSHOT_DIR / "x_post_ready.png"
    posted_shot = SCREENSHOT_DIR / "x_post_posted.png"
    match_snippet = post_text[:40]

    # ---- Harness image upload snippet ----
    upload_snippet = ""
    if img_base64:
        upload_snippet = textwrap.indent(
            harness_upload_image_snippet(img_base64, img_mime),
            "    ",
        )

    code = f'''
import json

post_text = {json.dumps(post_text, ensure_ascii=False)}
match_snippet = {json.dumps(match_snippet, ensure_ascii=False)}
ready_shot = {json.dumps(str(ready_shot))}
posted_shot = {json.dumps(str(posted_shot))}
target_url = 'https://x.com/home'
{harness_navigate_snippet('target_url')}
current = page_info()
if current.get('dialog') or 'x.com/home' not in (current.get('url') or ''):
    js('window.onbeforeunload = null')
    goto_url('https://x.com/home')
    wait_for_load(20)
    wait(2)
    current = page_info()

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
{upload_snippet}
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
        wait(8)
        body = js('document.body.innerText') or ''
        capture_screenshot(posted_shot)
        sent_ok = ('你的帖子已发送' in body) or ('Your post was sent' in body)
        posted_url = ''
        if profile_url:
            js('window.onbeforeunload = null')
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
""" % json.dumps(match_snippet, ensure_ascii=False)) or ''
            # Retry once if URL extraction failed
            if not posted_url:
                wait(3)
                posted_url = js("""
(() => {{
  const snippet = %s;
  const articles = Array.from(document.querySelectorAll('article'));
  function statusLinks(article) {{
    return Array.from(article.querySelectorAll('a[href*=\"/status/\"]'))
      .map((link) => link.href || '')
      .filter(Boolean)
      .map((href) => href.replace(/\\/analytics$/, ''))
      .filter((href) => /\\/status\\/\\d+$/.test(href));
  }}
  if (articles.length) {{
    const links = statusLinks(articles[0]);
    if (links.length) {{ links.sort((a, b) => a.length - b.length); return links[0]; }}
  }}
  for (const article of articles.slice(0, 5)) {{
    if ((article.innerText || '').includes(snippet)) {{
      const links = statusLinks(article);
      if (links.length) {{ links.sort((a, b) => a.length - b.length); return links[0]; }}
    }}
  }}
  return '';
}})()
""" % json.dumps(match_snippet, ensure_ascii=False)) or ''
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
                "image_query": image_query,
            }
        )
        print(stdout)
        if image_info:
            print(f"IMAGE_INFO: {json.dumps(image_info, ensure_ascii=False)}")
        return 0 if ok else 1
    except Exception as exc:
        append_log(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "status": "exception",
                "post_text": post_text,
                "image_query": image_query,
                "error": str(exc),
            }
        )
        logger.error("%s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
