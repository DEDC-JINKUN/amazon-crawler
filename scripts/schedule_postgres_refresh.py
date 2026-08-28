#!/usr/bin/env python3
"""Queue stale Amazon product snapshots in PostgreSQL."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


try:
    from postgres_worker_storage import PostgresWorkerStorage
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from postgres_worker_storage import PostgresWorkerStorage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--tenant-id", default="amazon_us_local")
    parser.add_argument("--subject-type", choices=("own", "competitor", "candidate"), default="own")
    parser.add_argument("--min-age-hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args(argv)
    try:
        dsn = os.environ.get(args.dsn_env, "").strip()
        if not dsn:
            raise ValueError(f"PostgreSQL DSN environment variable is required: {args.dsn_env}")
        storage = PostgresWorkerStorage(dsn, tenant_id=args.tenant_id, subject_type=args.subject_type)
        queued = storage.enqueue_due_refreshes(min_age_hours=args.min_age_hours, limit=args.limit)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}, ensure_ascii=False))
        return 1
    print(json.dumps({
        "ok": True,
        "tenant_id": args.tenant_id,
        "subject_type": args.subject_type,
        "min_age_hours": args.min_age_hours,
        "limit": args.limit,
        "queued": queued,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
