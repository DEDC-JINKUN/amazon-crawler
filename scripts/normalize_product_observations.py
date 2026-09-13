"""Normalize a saved snapshot/evidence JSON file without a network request."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from product_watch import ProductWatchRepository, normalize_keyword_rank, normalize_snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="saved JSON with snapshot/payload and evidence")
    parser.add_argument("--output", required=True)
    parser.add_argument("--dsn", help="optional approved PostgreSQL target; omit for offline normalization")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--marketplace", default="US")
    parser.add_argument("--subject-type", default="own", choices=("own", "competitor", "candidate"))
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    evidence = payload["evidence"]
    common = {"tenant_id": args.tenant_id, "marketplace": args.marketplace, "subject_type": args.subject_type, "requested_asin": payload.get("requested_asin"), "observed_at": payload.get("observed_at")}
    if payload.get("kind") == "keyword_rank" or "keyword" in payload.get("payload", {}):
        observations = [normalize_keyword_rank(payload.get("payload") or payload["snapshot"], evidence, **common)]
    else:
        observations = normalize_snapshot(payload["snapshot"], evidence, **common)
    if args.dsn:
        ProductWatchRepository(args.dsn).persist_observations(observations)
    Path(args.output).write_text(json.dumps([item.to_dict() for item in observations], ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
