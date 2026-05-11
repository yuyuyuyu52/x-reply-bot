#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import textwrap
from datetime import datetime, timezone

from src.harness import harness_upload_image_snippet, run_harness
from src.common import SCREENSHOT_DIR, append_log, ensure_state_dirs, load_env_file
from src.image_search import search_image, download_image, image_to_base64, cleanup_temp_image
from src.logger import get_logger

logger = get_logger(__name__)


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--image-query", default="")
    args = parser.parse_args()
    title = args.title.strip()
    body = args.body.strip()
    image_query = args.image_query.strip()
    if not title:
        logger.error("empty title")
        return 2
    if not body:
        logger.error("empty body")
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
    ready_shot = SCREENSHOT_DIR / "x_article_ready.png"
    posted_shot = SCREENSHOT_DIR / "x_article_posted.png"

    # Image upload snippet
    upload_snippet = ""
    if img_base64:
        upload_snippet = textwrap.indent(
            harness_upload_image_snippet(img_base64, img_mime),
            "        ",
        )

    code = f'''
import json

title = {json.dumps(title, ensure_ascii=False)}
body = {json.dumps(body, ensure_ascii=False)}
ready_shot = {json.dumps(str(ready_shot))}
posted_shot = {json.dumps(str(posted_shot))}

# Single terminal print: ``fail`` is set at the first short-circuit and the
# remaining steps are skipped. ``result`` holds the success payload. Without
# this, an early failure (e.g. no title element) used to fall through to
# body typing and publish — risking an empty-title article or publishing
# body text into whatever element had focus.
fail = None
result = None

# Navigate to articles page
tabs = list_tabs(include_chrome=False)
for t in tabs:
    if 'x.com' in t.get('url', ''):
        switch_tab(t['targetId'])
        break

js('window.onbeforeunload = null')
goto_url('https://x.com/compose/articles')
wait_for_load(20)
wait(4)

# Click "撰写" button
write_clicked = js("""
(() => {{
  const btn = document.querySelector('[data-testid="empty_state_button_text"]');
  if (!btn) return {{ok:false, reason:'no write button'}};
  btn.click();
  return {{ok:true}};
}})()
""")
if not (write_clicked and write_clicked.get('ok')):
    fail = {{'ok': False, 'reason': 'no_write_button', 'detail': write_clicked}}

if fail is None:
    wait_for_load(20)
    wait(4)
    info = page_info()
    if '/compose/articles/edit/' not in (info.get('url') or ''):
        fail = {{'ok': False, 'reason': 'article_editor_not_opened', 'url': info.get('url', '')}}

# ---- Type title ----
# X.com article title uses a hidden textarea (placeholder='添加标题')
# that appears after clicking the title area. Use CDP keyboard via
# click_at_xy + type_text for proper React event handling.
if fail is None:
    title_ta = js("""
    (() => {{
      const ta = document.querySelector('textarea[placeholder=\"添加标题\"]');
      if (ta) {{
        const r = ta.getBoundingClientRect();
        return {{ok:true, x:r.left + 50, y:r.top + 20}};
      }}
      // Fallback: click the title display div
      const titleDiv = document.querySelector('[data-testid=\"twitter-article-title\"]');
      if (!titleDiv) return {{ok:false, reason:'no title element'}};
      const r = titleDiv.getBoundingClientRect();
      return {{ok:true, x:r.left + r.width / 2, y:r.top + r.height / 2}};
    }})()
    """)
    if not (title_ta and title_ta.get('ok')):
        # Short-circuit: previous code fell through here, leaving focus on
        # whatever clicked element happened to be active. type_text would
        # then dump body characters into the wrong field.
        fail = {{'ok': False, 'reason': 'no_title_element', 'detail': title_ta}}
    else:
        click_at_xy(title_ta['x'], title_ta['y'])
        wait(0.5)
        type_text(title)
        wait(1)

# ---- Type body ----
if fail is None:
    body_focus = js("""
    (() => {{
      const el = document.querySelector('[data-testid="composer"][role="textbox"]');
      if (!el) return {{ok:false, reason:'no body editor'}};
      el.focus();
      const range = document.createRange();
      range.selectNodeContents(el);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      return {{ok:true}};
    }})()
    """)
    if not (body_focus and body_focus.get('ok')):
        fail = {{'ok': False, 'reason': 'no_body_editor', 'detail': body_focus}}
    else:
        wait(0.5)
        type_text(body)
        wait(2)
        capture_screenshot(ready_shot)

# ---- Upload image (optional) ----
if fail is None:
{upload_snippet}

if fail is None:
    # ---- Click publish (opens sheetDialog) ----
    publish_clicked = js("""
    (() => {{
      const btns = Array.from(document.querySelectorAll('button'));
      const publish = btns.find(b => (b.innerText || '').trim() === '发布');
      if (!publish) return {{ok:false, reason:'no publish button'}};
      publish.click();
      return {{ok:true}};
    }})()
    """)
    if not (publish_clicked and publish_clicked.get('ok')):
        fail = {{'ok': False, 'reason': 'no_publish_button', 'detail': publish_clicked}}
    else:
        wait(3)

if fail is None:
    # ---- Confirm publish in sheetDialog ----
    confirm_clicked = js("""
    (() => {{
      const sheet = document.querySelector('[data-testid=\"sheetDialog\"]');
      if (!sheet) return {{ok:false, reason:'no sheetDialog'}};
      const btns = Array.from(sheet.querySelectorAll('button'));
      const confirm = btns.find(b => (b.innerText || '').trim() === '发布');
      if (!confirm) return {{ok:false, reason:'no confirm button in sheet'}};
      confirm.click();
      return {{ok:true}};
    }})()
    """)
    if not (confirm_clicked and confirm_clicked.get('ok')):
        fail = {{'ok': False, 'reason': 'no_confirm_button', 'detail': confirm_clicked}}
    else:
        wait(8)
        capture_screenshot(posted_shot)

        # After publish, X redirects to the article's status URL
        final_info = page_info()
        article_url = (final_info.get('url') or '')
        sent_ok = '/status/' in article_url

        result = {{
            'ok': sent_ok,
            'sent_ok': sent_ok,
            'title': title,
            'body': body[:200],
            'url': article_url,
            'click_result': publish_clicked,
            'confirm_result': confirm_clicked,
            'page_info': final_info,
        }}

if result is not None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
else:
    if fail is None:
        fail = {{'ok': False, 'reason': 'unknown_failure'}}
    # Mirror sent_ok=False so post_once's two-key check still works for fails.
    fail.setdefault('sent_ok', False)
    print(json.dumps(fail, ensure_ascii=False, indent=2))
'''
    try:
        stdout = run_harness(textwrap.dedent(code), timeout=120)
        ok = '"ok": true' in stdout or "'ok': True" in stdout
        append_log(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "status": "success" if ok else "uncertain",
                "title": title,
                "body_preview": body[:100],
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
                "title": title,
                "body_preview": body[:100],
                "image_query": image_query,
                "error": str(exc),
            }
        )
        logger.error("%s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
