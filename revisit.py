#!/usr/bin/env python3
"""Engagement revisit job.

Scans `state/post_history/` for proactive posts that are >= 24h old and don't
yet have an `engagement_24h` metrics block, opens each post URL via the
browser harness, scrapes the primary article's aria-label metrics, and writes
the result back into the JSON file.

Reply-side feedback is intentionally out of scope for now: a reply lives
nested inside the original author's thread, so locating "my own reply" reliably
is a harder problem and warrants its own job.

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

from common import (
    LATEST_REVISIT_RUN_PATH,
    POST_HISTORY_DIR,
    ensure_state_dirs,
    load_env_file,
    run_harness,
    write_json,
)

ROOT = Path(__file__).resolve().parent
REVISIT_DELAY_HOURS = 24
MAX_ATTEMPTS = 3
DEFAULT_MAX_PER_RUN = 20

# Reuse the proven metric extractors from observe_feed.py rather than
# duplicating regexes — those have been tuned against real X DOM.
from observe_feed import engagement_score, parse_metrics  # noqa: E402


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


def record_url(record: dict) -> str:
    return str(record.get("post_url") or "").strip()


def needs_revisit(record: dict, now: datetime) -> bool:
    # Skip dry-runs and non-posted records; there's nothing to measure.
    if record.get("dry_run") or record.get("status") != "posted":
        return False

    url = record_url(record)
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
    """Return [{path, record, url}] for proactive posts due for revisit, oldest first."""
    pending: list[dict] = []
    for path in sorted(POST_HISTORY_DIR.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not needs_revisit(record, now):
            continue
        pending.append({"path": path, "record": record, "url": record_url(record)})
    pending.sort(key=lambda item: parse_record_time(item["record"]) or datetime.fromtimestamp(0, tz=timezone.utc))
    return pending


def fetch_metrics(url: str) -> dict:
    """Open the status URL and read aria-labels off the primary article."""
    code = textwrap.dedent(f"""
import json

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

# Wait for at least one article to render. X sometimes shows a skeleton
# loader for a couple of seconds after navigation.
for _ in range(6):
    has_article = js("!!document.querySelector('article[data-testid=\\"tweet\\"]')")
    if has_article:
        break
    wait(1)

aria = js(\"\"\"
(() => {{
  const article = document.querySelector('article[data-testid="tweet"]');
  if (!article) return [];
  return Array.from(article.querySelectorAll('[aria-label]'))
    .map((el) => el.getAttribute('aria-label'))
    .filter(Boolean)
    .slice(0, 60);
}})()
\"\"\") or []

body = js("document.body.innerText || ''") or ''
deleted = ('帖子被删除' in body) or ('Post unavailable' in body) or ('this post' in body.lower() and 'deleted' in body.lower())

print(json.dumps({{'aria': aria, 'deleted': bool(deleted)}}, ensure_ascii=False))
""")
    output = run_harness(code, timeout=90)
    last_line = ""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            last_line = line
    if not last_line:
        return {"aria": [], "deleted": False, "raw_output": output[-500:]}
    try:
        payload = json.loads(last_line)
        return {
            "aria": list(payload.get("aria") or []),
            "deleted": bool(payload.get("deleted")),
        }
    except Exception:
        return {"aria": [], "deleted": False, "raw_output": output[-500:]}


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
                "url": item["url"],
                "path": str(item["path"].relative_to(ROOT)),
            }
            for item in pending
        ]
        write_json(LATEST_REVISIT_RUN_PATH, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    for item in pending[: max(1, args.max)]:
        item_summary = {
            "url": item["url"],
            "path": str(item["path"].relative_to(ROOT)),
        }
        try:
            result = fetch_metrics(item["url"])
        except Exception as exc:
            eng = update_record(item, datetime.now().astimezone(), success=False, metrics=None, deleted=False, error=f"harness_error: {exc}")
            item_summary.update({"ok": False, "error": str(exc), "attempts": eng.get("attempts")})
            summary["items"].append(item_summary)
            summary["processed"] += 1
            summary["failed"] += 1
            continue

        deleted = bool(result.get("deleted"))
        aria = result.get("aria") or []
        metrics = parse_metrics(aria) if aria else None

        if metrics and any(metrics.get(k) for k in ("views", "likes", "replies", "reposts", "bookmarks")):
            eng = update_record(item, datetime.now().astimezone(), success=True, metrics=metrics, deleted=False, error=None)
            item_summary.update({"ok": True, "metrics": metrics, "score": eng.get("score"), "attempts": eng.get("attempts")})
            summary["succeeded"] += 1
        elif deleted:
            eng = update_record(item, datetime.now().astimezone(), success=False, metrics=None, deleted=True, error=None)
            item_summary.update({"ok": False, "error": "post_unavailable", "attempts": eng.get("attempts"), "failed": True})
            summary["deleted"] += 1
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
