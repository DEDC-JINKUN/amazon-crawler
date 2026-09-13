"""Detect product-watch events from saved normalized observations; never sends alerts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from product_watch import ChangeEvent, Observation, ProductWatchRepository, detect_changes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="JSON object containing current, history, and optional rules")
    parser.add_argument("--output", required=True)
    parser.add_argument("--dsn", help="optional approved PostgreSQL target; omit for offline detection")
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    events = detect_changes((Observation.from_dict(item) for item in payload.get("current", [])), (Observation.from_dict(item) for item in payload.get("history", [])), rules=payload.get("rules"))
    if args.dsn:
        ProductWatchRepository(args.dsn).persist_events(events)
    Path(args.output).write_text(json.dumps([item.to_dict() for item in events], ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
