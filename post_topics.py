#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime

from src.common import ensure_state_dirs, load_env_file, load_post_topics, normalize_post_topic, save_post_topics, topic_summary_text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--add", default="")
    parser.add_argument("--source", default="manual")
    parser.add_argument("--type", default="argument")
    parser.add_argument("--subject", default="")
    parser.add_argument("--context", default="")
    parser.add_argument("--stance", default="")
    parser.add_argument("--evidence", default="")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    load_env_file()
    ensure_state_dirs()
    data = load_post_topics()

    has_structured = any(
        [
            args.add.strip(),
            args.subject.strip(),
            args.context.strip(),
            args.stance.strip(),
            args.evidence.strip(),
        ]
    )
    if has_structured:
        stamp = datetime.now().astimezone().strftime("%Y%m%d%H%M%S")
        topic = normalize_post_topic(
            {
            "id": f"topic-{stamp}",
            "text": args.add.strip(),
            "type": args.type.strip() or "argument",
            "source": args.source.strip() or "manual",
            "status": "pending",
            "subject": args.subject.strip(),
            "event_or_context": args.context.strip(),
            "stance": args.stance.strip(),
            "evidence_hint": args.evidence.strip(),
            }
        )
        if not topic_summary_text(topic):
            raise SystemExit("Need at least one of --add/--subject/--context/--stance.")
        data.setdefault("topics", []).append(topic)
        save_post_topics(data)
        print(json.dumps(topic, ensure_ascii=False, indent=2))
        return 0

    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
