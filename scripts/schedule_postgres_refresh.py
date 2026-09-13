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


CADENCE_PROFILES = {
    "own_core": ("own", 4, ("own_core",)),
    "own_normal": ("own", 12, ("own_normal", "standard")),
    "competitor_core": ("competitor", 6, ("competitor_core",)),
    "competitor_normal": ("competitor", 24, ("competitor_normal", "standard")),
    "candidate": ("candidate", 72, ("candidate", "standard")),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--tenant-id", default="amazon_us_main")
    parser.add_argument("--subject-type", choices=("own", "competitor", "candidate"), default="own")
    parser.add_argument("--min-age-hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--all-profiles", action="store_true", help="Queue every configured cadence profile")
    args = parser.parse_args(argv)
    try:
        dsn = os.environ.get(args.dsn_env, "").strip()
        if not dsn:
            raise ValueError(f"PostgreSQL DSN environment variable is required: {args.dsn_env}")
        if args.limit < 1:
            raise ValueError("limit must be positive")
        details = []
        queued = 0
        if args.all_profiles:
            remaining = args.limit
            for profile, (subject_type, hours, policies) in CADENCE_PROFILES.items():
                if remaining <= 0:
                    break
                storage = PostgresWorkerStorage(dsn, tenant_id=args.tenant_id, subject_type=subject_type)
                count = storage.enqueue_due_refreshes(
                    min_age_hours=hours,
                    limit=remaining,
                    refresh_policies=policies,
                )
                queued += count
                remaining -= count
                details.append({"profile": profile, "subject_type": subject_type, "hours": hours, "queued": count})
        else:
            storage = PostgresWorkerStorage(dsn, tenant_id=args.tenant_id, subject_type=args.subject_type)
            queued = storage.enqueue_due_refreshes(min_age_hours=args.min_age_hours, limit=args.limit)
            details = [{"profile": "legacy", "subject_type": args.subject_type, "hours": args.min_age_hours, "queued": queued}]
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}, ensure_ascii=False))
        return 1
    payload = {
        "ok": True,
        "tenant_id": args.tenant_id,
        "scope": "all_profiles" if args.all_profiles else "single_profile",
        "limit": args.limit,
        "queued": queued,
        "profiles": details,
    }
    if not args.all_profiles:
        payload.update(subject_type=args.subject_type, min_age_hours=args.min_age_hours)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
