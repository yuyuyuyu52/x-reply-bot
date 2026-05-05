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

{harness_navigate_snippet('url')}
if not (info_before.get('url') or '').startswith(url):
    print(json.dumps({{'ok': False, 'reason': 'wrong_url', 'page_info': info_before}}, ensure_ascii=False))
else:
    {harness_compose_and_send_snippet(text_var='reply_text', button_order='inline_first')}
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
