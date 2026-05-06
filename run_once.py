#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import os
import fcntl
import sys
from datetime import datetime
from pathlib import Path

from src.common import (
    exclusive_lock,
    LATEST_RUN_PATH,
    append_log,
    ensure_state_dirs,
    history_path_for,
    load_env_file,
    load_json,
    telegram_enabled,
    telegram_notify,
    write_json,
)

ROOT = Path(__file__).resolve().parent
RUN_LOCK_PATH = ROOT / "state" / "run_once.lock"


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(cmd, text=True, capture_output=True, cwd=str(ROOT), env=env)


def notify_text(record: dict) -> str:
    action = record.get('action', 'reply')
    action_label = {
        'reply': '💬 回复',
        'quote': '🔁 引用 (Quote)',
        'repost': '🔄 转发 (Repost)'
    }.get(action, '💬 回复')
    text = "\n".join(
        [
            action_label,
            "",
            f"🕒 时间: {record['time_beijing']}",
            f"⚙️ 触发: {record['trigger']}",
            f"🔗 帖子: {record['post_url']}",
            f"🎯 选中理由: {record['selection_reason']}",
            f"💰 Cost: {record['total_cost_cny']:.6f} 元",
            "",
            "📄 帖子内容:",
            record["post_text"],
            "",
            f"💭 {action}内容:",
            record["reply_text"],
            "",
            "🧠 理由:",
            record["reply_reason"],
        ]
    )
    if len(text) > 3800:
        return text[:3750] + "\n\n[通知过长，已截断；完整内容见本机 state/history]"
    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger", default="manual")
    args = parser.parse_args()

    load_env_file()
    ensure_state_dirs()
    lock_fh = RUN_LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("run_once already running")
        return 3

    started = datetime.now().astimezone()
    stamp = started.strftime("%Y%m%d_%H%M%S")

    try:
        prep = run([sys.executable, str(ROOT / "src/reply/prepare_post.py")])
        sys.stdout.write(prep.stdout)
        sys.stderr.write(prep.stderr)
        if prep.returncode != 0:
            selected = load_json(ROOT / "state" / "selected_post.json", {})
            prep_reason = str(selected.get("reason") or "").strip()
            if prep_reason in {"ai_rejected_all_candidates", "no_suitable_feed_candidates"}:
                append_log(
                    {
                        "time": started.isoformat(),
                        "status": "skipped",
                        "trigger": args.trigger,
                        "reason": prep_reason,
                    }
                )
                return 0
            return prep.returncode

        selected = load_json(ROOT / "state" / "selected_post.json", {})

        gen = run([sys.executable, str(ROOT / "src/reply/generate_reply.py")])
        sys.stdout.write(gen.stderr)
        if gen.returncode != 0:
            sys.stdout.write(gen.stdout)
            return gen.returncode
        reply_payload = json.loads(gen.stdout)
        reply_text = str(reply_payload.get("reply") or "").strip()
        action = str(reply_payload.get("action") or "reply").strip()
        reply_reason = str(reply_payload.get("reason") or "").strip()
        reply_source_url = str(reply_payload.get("source_post_url") or "").strip()
        reply_selection_id = str(reply_payload.get("selection_id") or "").strip()
        reply_usage = reply_payload.get("usage") or {}
        reply_cost = reply_payload.get("cost") or {}
        print(f"GENERATED_ACTION: {action}")
        print(reply_text)

        selected_url = str(selected.get("url") or "").strip()
        selected_selection_id = str(selected.get("selection_id") or "").strip()
        if reply_source_url != selected_url or reply_selection_id != selected_selection_id:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": "reply_selection_mismatch",
                        "selected_url": selected_url,
                        "reply_source_url": reply_source_url,
                        "selected_selection_id": selected_selection_id,
                        "reply_selection_id": reply_selection_id,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1

        send = run([sys.executable, str(ROOT / "src/reply/send_reply.py"), "--url", selected_url, "--reply", reply_text, "--action", action])
        sys.stdout.write(send.stdout)
        sys.stderr.write(send.stderr)

        selection_usage = selected.get("selection_usage") or {}
        selection_cost = selected.get("selection_cost") or {}
        total_cost_cny = round(
            float(selection_cost.get("total_cost") or 0.0)
            + float(reply_cost.get("total_cost") or 0.0),
            8,
        )
        record = {
            "time_beijing": started.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "date_beijing": started.strftime("%Y-%m-%d"),
            "trigger": args.trigger,
            "post_url": selected.get("url", ""),
            "selection_reason": selected.get("selector_reason", ""),
            "post_text": selected.get("main_post_text", ""),
            "action": action,
            "reply_text": reply_text,
            "reply_reason": reply_reason,
            "selection_model": selected.get("selection_model", ""),
            "selection_usage": selection_usage,
            "selection_cost": selection_cost,
            "reply_model": reply_cost.get("model", ""),
            "reply_usage": reply_usage,
            "reply_cost": reply_cost,
            "total_cost_cny": total_cost_cny,
            "send_returncode": send.returncode,
            "send_stdout": send.stdout,
        }
        write_json(LATEST_RUN_PATH, record)
        write_json(history_path_for(stamp), record)
        append_log(
            {
                "time": started.isoformat(),
                "status": "success" if send.returncode == 0 else "failed",
                "trigger": args.trigger,
                "url": record["post_url"],
                "reply": record["reply_text"],
                "reply_reason": record["reply_reason"],
                "total_cost_cny": record["total_cost_cny"],
            }
        )

        if send.returncode == 0 and telegram_enabled():
            try:
                tg_resp = telegram_notify(notify_text(record))
                record["telegram_notify"] = {
                    "ok": True,
                    "response": tg_resp,
                }
                write_json(LATEST_RUN_PATH, record)
                write_json(history_path_for(stamp), record)
            except Exception as exc:
                record["telegram_notify"] = {
                    "ok": False,
                    "error": str(exc),
                }
                write_json(LATEST_RUN_PATH, record)
                write_json(history_path_for(stamp), record)
                print(f"TELEGRAM_NOTIFY_ERROR: {exc}")

        return send.returncode
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()


if __name__ == "__main__":
    raise SystemExit(main())
