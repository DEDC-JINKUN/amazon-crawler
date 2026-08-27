#!/usr/bin/env python3
"""Enqueue stale ASINs without making any network request."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from freshness_policy import FreshnessPolicy
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from freshness_policy import FreshnessPolicy


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue_stale(db_path: Path, field_groups: list[str], *, limit: int = 1000, now: datetime | None = None, requested_by: str = "freshness-scheduler") -> list[dict[str, str]]:
    if not field_groups:
        raise ValueError("at least one field group is required")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    policy = FreshnessPolicy()
    current = now or _now()
    conn = sqlite3.connect(str(db_path), timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT s.marketplace, s.asin, p.collected_at FROM item_state s "
            "LEFT JOIN product_snapshot p ON p.marketplace=s.marketplace AND p.asin=s.asin "
            "WHERE s.status IN ('succeeded','product_done','reviews_pending') ORDER BY s.asin"
        ).fetchall()
        queued: list[dict[str, str]] = []
        for row in rows:
            if len(queued) >= limit:
                break
            if policy.age_seconds(row["collected_at"], current) is not None and all(policy.age_seconds(row["collected_at"], current) <= policy.ttl(group) for group in field_groups):
                continue
            duplicate = conn.execute(
                "SELECT 1 FROM refresh_request WHERE marketplace=? AND asin=? AND status IN ('queued','claimed') LIMIT 1",
                (row["marketplace"], row["asin"]),
            ).fetchone()
            if duplicate:
                continue
            request = {
                "job_id": f"refresh-{uuid.uuid4().hex}",
                "marketplace": row["marketplace"],
                "asin": row["asin"],
                "requested_by": requested_by,
                "reason": f"stale:{','.join(field_groups)}",
                "status": "queued",
                "requested_at": current.isoformat(),
            }
            conn.execute(
                "INSERT INTO refresh_request(job_id,marketplace,asin,requested_by,reason,status,requested_at) VALUES(?,?,?,?,?,?,?)",
                tuple(request.values()),
            )
            queued.append(request)
        conn.commit()
        return queued
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("state/amazon_us.sqlite3"))
    parser.add_argument("--fields", default="price,availability")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--requested-by", default="freshness-scheduler")
    args = parser.parse_args(argv)
    try:
        requests = enqueue_stale(args.db, [item.strip() for item in args.fields.split(",") if item.strip()], limit=args.limit, requested_by=args.requested_by)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps({"queued": len(requests), "requests": requests}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
