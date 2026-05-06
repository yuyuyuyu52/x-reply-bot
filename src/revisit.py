#!/usr/bin/env python3
"""Engagement revisit job.

Scans `state/post_history/` (proactive posts) and `state/history/` (replies)
for items that are >= 24h old and don't yet have an `engagement_24h` metrics
block, opens each URL via the browser harness, scrapes aria-label metrics,
and writes the result back into the JSON file.

For proactive posts: open the post URL, read metrics off the primary article.
For replies: open the original post URL, scroll the thread, and locate the
article whose tweet text exactly matches our `reply_text` (with author-handle
confirmation when available). If nothing matches after a few scrolls, mark
this attempt as failed; after 3 failed attempts the record is marked
permanently failed.

Designed to run only inside the night window (23:00–07:00 Beijing) so it
doesn't compete with the reply / proactive-post / observe-feed jobs for the
single Chrome session.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.common import (
    HISTORY_DIR,
    LATEST_REVISIT_RUN_PATH,
    POST_HISTORY_DIR,
    ensure_state_dirs,
    load_env_file,
    run_harness,
    write_json,
)

ROOT = Path(__file__).resolve().parent.parent
REVISIT_DELAY_HOURS = 24
MAX_ATTEMPTS = 3
DEFAULT_MAX_PER_RUN = 20

# Reuse the proven metric extractors from observe_feed.py rather than
# duplicating regexes — those have been tuned against real X DOM.
from src.metrics import engagement_score, infer_own_handle, parse_metrics  # noqa: E402


def parse_record_time(record: dict) -> datetime | None:
    """Extract the post / reply timestamp as an aware datetime."""
    raw = record.get("time_beijing") or record.get("time") or ""
    if not raw:
        return None
    # post_history / history use "%Y-%m-%d %H:%M:%S CST" (Beijing).
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S CST"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                # CST is treated as UTC+8.
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            return dt
        except ValueError:
            pass
    # Older reply records use ISO-8601 in UTC.
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def record_url(record: dict, kind: str) -> str:
    """URL of the page where this record's metrics live.

    For proactive posts: the post's own status URL.
    For replies: the *original* post URL — our reply lives nested inside it.
    """
    return str(record.get("post_url") or "").strip()


def reply_text_of(record: dict) -> str:
    return str(record.get("reply_text") or record.get("reply") or "").strip()


def needs_revisit(record: dict, kind: str, now: datetime) -> bool:
    if kind == "post":
        # Skip dry-runs and non-posted records; there's nothing to measure.
        if record.get("dry_run") or record.get("status") != "posted":
            return False
    elif kind == "reply":
        # Reply records use send_returncode==0 + reply_text + post_url as the
        # success signal; there's no top-level "status" field in this branch.
        rc = record.get("send_returncode")
        if rc is None or int(rc) != 0:
            return False
        if not reply_text_of(record):
            return False
    else:
        return False

    url = record_url(record, kind)
    if not url:
        return False

    posted_at = parse_record_time(record)
    if posted_at is None:
        return False
    if (now - posted_at) < timedelta(hours=REVISIT_DELAY_HOURS):
        return False

    eng = record.get("engagement_24h") or {}
    if eng.get("metrics"):
        return False
    if eng.get("failed"):
        return False
    if int(eng.get("attempts") or 0) >= MAX_ATTEMPTS:
        return False
    return True


def find_pending(now: datetime) -> list[dict]:
    """Return [{kind, path, record, url}] across both histories, oldest first."""
    pending: list[dict] = []
    for kind, directory in (("post", POST_HISTORY_DIR), ("reply", HISTORY_DIR)):
        for path in sorted(directory.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not needs_revisit(record, kind, now):
                continue
            pending.append(
                {
                    "kind": kind,
                    "path": path,
                    "record": record,
                    "url": record_url(record, kind),
                }
            )
    pending.sort(key=lambda item: parse_record_time(item["record"]) or datetime.fromtimestamp(0, tz=timezone.utc))
    return pending


def _last_json_line(output: str) -> dict | None:
    last = ""
    for line in (output or "").splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            last = line
    if not last:
        return None
    try:
        return json.loads(last)
    except Exception:
        return None


_AWAIT_ARTICLE_PY = """
for _ in range(6):
    if js(r'''!!document.querySelector('article[data-testid="tweet"]')'''):
        break
    wait(1)
"""


_DELETED_PROBE_PY = """
body = js('document.body.innerText || ""') or ''
lower = body.lower()
deleted = ('帖子被删除' in body) or ('Post unavailable' in body) or ('this post' in lower and 'deleted' in lower)
"""


_PRIMARY_ARIA_JS = r"""
(() => {
  const article = document.querySelector('article[data-testid="tweet"]');
  if (!article) return [];
  return Array.from(article.querySelectorAll('[aria-label]'))
    .map((el) => el.getAttribute('aria-label'))
    .filter(Boolean)
    .slice(0, 60);
})()
"""


_REPLY_SCAN_JS_TEMPLATE = r"""
((target, ownHandle) => {
  const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
  for (const article of articles) {
    const textBlock = article.querySelector('[data-testid="tweetText"]');
    const text = (textBlock && textBlock.innerText || '').trim();
    if (!text || text !== target) continue;
    if (ownHandle) {
      const userBlock = article.querySelector('[data-testid="User-Name"]');
      const userText = (userBlock && userBlock.innerText || '').toLowerCase();
      if (!userText.includes('@' + ownHandle)) continue;
    }
    const aria = Array.from(article.querySelectorAll('[aria-label]'))
      .map((el) => el.getAttribute('aria-label'))
      .filter(Boolean)
      .slice(0, 60);
    return { matched: true, aria };
  }
  return { matched: false, aria: [] };
})
"""


def _navigate_block(url: str) -> str:
    """Return harness Python that switches to / navigates to ``url``."""
    return textwrap.dedent(f"""
tabs = list_tabs(include_chrome=False)
x_tab = None
for t in tabs:
    if t.get('url', '').startswith({url!r}):
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
    tid = new_tab({url!r})
    switch_tab(tid)

current = page_info()
if current.get('dialog') or not (current.get('url') or '').startswith({url!r}):
    js('window.onbeforeunload = null')
    goto_url({url!r})
    wait_for_load(20)
    wait(4)
""")


def fetch_post_metrics(url: str) -> dict:
    """Open the post URL and read aria-labels off the primary article."""
    code = (
        "import json\n"
        + _navigate_block(url)
        + _AWAIT_ARTICLE_PY
        + f"aria = js({_PRIMARY_ARIA_JS!r}) or []\n"
        + _DELETED_PROBE_PY
        + "print(json.dumps({'aria': aria, 'deleted': bool(deleted)}, ensure_ascii=False))\n"
    )
    output = run_harness(code, timeout=90)
    payload = _last_json_line(output)
    if payload is None:
        return {"aria": [], "deleted": False, "raw_output": output[-500:]}
    return {
        "aria": list(payload.get("aria") or []),
        "deleted": bool(payload.get("deleted")),
    }


def fetch_reply_metrics(post_url: str, reply_text: str, own_handle: str) -> dict:
    """Open the original post URL, scroll the thread, locate our nested
    reply by exact tweetText match (with author-handle confirmation when
    known), and return its aria-labels.

    Returns ``{aria, matched, deleted, scrolls_used, raw_output?}``. ``matched``
    is True iff we found an article whose tweetText equals our reply_text. If
    ``matched`` is False but ``deleted`` is False, the caller should retry on
    the next nightly window — most likely a transient miss (lazy-load, scroll
    truncation, X DOM hiccup).
    """
    target_payload = json.dumps(reply_text, ensure_ascii=False)
    handle_payload = json.dumps((own_handle or "").lower(), ensure_ascii=False)
    scan_call_js = _REPLY_SCAN_JS_TEMPLATE.strip() + "(" + target_payload + ", " + handle_payload + ")"

    code = (
        "import json\n"
        + _navigate_block(post_url)
        + _AWAIT_ARTICLE_PY
        + _DELETED_PROBE_PY
        + textwrap.dedent(f"""
result = {{'matched': False, 'aria': [], 'deleted': bool(deleted), 'scrolls_used': 0}}
scrolls = 0
max_scrolls = 8
scan_call = {scan_call_js!r}
while True:
    found = js(scan_call) or {{}}
    if found.get('matched'):
        result['matched'] = True
        result['aria'] = list(found.get('aria') or [])
        result['scrolls_used'] = scrolls
        break
    if scrolls >= max_scrolls:
        result['scrolls_used'] = scrolls
        break
    js('window.scrollBy(0, window.innerHeight * 0.9)')
    wait(2)
    scrolls += 1

print(json.dumps(result, ensure_ascii=False))
""")
    )
    output = run_harness(code, timeout=120)
    payload = _last_json_line(output)
    if payload is None:
        return {
            "aria": [],
            "matched": False,
            "deleted": False,
            "scrolls_used": 0,
            "raw_output": output[-500:],
        }
    return {
        "aria": list(payload.get("aria") or []),
        "matched": bool(payload.get("matched")),
        "deleted": bool(payload.get("deleted")),
        "scrolls_used": int(payload.get("scrolls_used") or 0),
    }


def update_record(item: dict, now: datetime, *, success: bool, metrics: dict | None, deleted: bool, error: str | None) -> dict:
    record = item["record"]
    eng = dict(record.get("engagement_24h") or {})
    attempts = int(eng.get("attempts") or 0) + 1
    eng["attempts"] = attempts
    eng["last_checked_at"] = now.strftime("%Y-%m-%d %H:%M:%S %Z")

    if success and metrics is not None:
        eng["checked_at"] = eng["last_checked_at"]
        eng["metrics"] = metrics
        eng["score"] = round(engagement_score(metrics), 4)
        eng.pop("error", None)
        eng["failed"] = False
    else:
        if deleted:
            eng["failed"] = True
            eng["error"] = "post_unavailable"
        else:
            eng["error"] = error or "no_metrics"
            if attempts >= MAX_ATTEMPTS:
                eng["failed"] = True

    record["engagement_24h"] = eng
    item["path"].write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return eng


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_PER_RUN, help="Maximum records to revisit this run.")
    parser.add_argument("--dry-run", action="store_true", help="List pending records without opening URLs.")
    args = parser.parse_args()

    load_env_file()
    ensure_state_dirs()

    now = datetime.now().astimezone()
    pending = find_pending(now)

    summary = {
        "time_beijing": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date_beijing": now.strftime("%Y-%m-%d"),
        "trigger": args.trigger,
        "pending_total": len(pending),
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "deleted": 0,
        "items": [],
    }

    if args.dry_run:
        summary["items"] = [
            {
                "kind": item["kind"],
                "url": item["url"],
                "path": str(item["path"].relative_to(ROOT)),
            }
            for item in pending
        ]
        write_json(LATEST_REVISIT_RUN_PATH, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    own_handle = ""

    for item in pending[: max(1, args.max)]:
        item_summary = {
            "kind": item["kind"],
            "url": item["url"],
            "path": str(item["path"].relative_to(ROOT)),
        }
        try:
            if item["kind"] == "post":
                result = fetch_post_metrics(item["url"])
                matched = True  # primary article is always "us" on a post URL
            else:
                # Resolve own_handle lazily so a missing latest_post_run.json
                # only matters when we actually have replies to revisit.
                if not own_handle:
                    own_handle = infer_own_handle()
                reply_text = reply_text_of(item["record"])
                result = fetch_reply_metrics(item["url"], reply_text, own_handle)
                matched = bool(result.get("matched"))
        except Exception as exc:
            eng = update_record(item, datetime.now().astimezone(), success=False, metrics=None, deleted=False, error=f"harness_error: {exc}")
            item_summary.update({"ok": False, "error": str(exc), "attempts": eng.get("attempts")})
            summary["items"].append(item_summary)
            summary["processed"] += 1
            summary["failed"] += 1
            continue

        deleted = bool(result.get("deleted"))
        aria = result.get("aria") or []
        metrics = parse_metrics(aria) if (aria and matched) else None

        if metrics and any(metrics.get(k) for k in ("views", "likes", "replies", "reposts", "bookmarks")):
            eng = update_record(item, datetime.now().astimezone(), success=True, metrics=metrics, deleted=False, error=None)
            item_summary.update({"ok": True, "metrics": metrics, "score": eng.get("score"), "attempts": eng.get("attempts")})
            summary["succeeded"] += 1
        elif deleted:
            eng = update_record(item, datetime.now().astimezone(), success=False, metrics=None, deleted=True, error=None)
            item_summary.update({"ok": False, "error": "post_unavailable", "attempts": eng.get("attempts"), "failed": True})
            summary["deleted"] += 1
            summary["failed"] += 1
        elif item["kind"] == "reply" and not matched:
            eng = update_record(
                item,
                datetime.now().astimezone(),
                success=False,
                metrics=None,
                deleted=False,
                error="reply_not_found",
            )
            item_summary.update(
                {
                    "ok": False,
                    "error": "reply_not_found",
                    "scrolls_used": result.get("scrolls_used", 0),
                    "attempts": eng.get("attempts"),
                    "failed": eng.get("failed", False),
                }
            )
            summary["failed"] += 1
        else:
            eng = update_record(item, datetime.now().astimezone(), success=False, metrics=None, deleted=False, error="no_metrics")
            item_summary.update({"ok": False, "error": "no_metrics", "attempts": eng.get("attempts"), "failed": eng.get("failed", False)})
            summary["failed"] += 1

        summary["items"].append(item_summary)
        summary["processed"] += 1

    write_json(LATEST_REVISIT_RUN_PATH, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
