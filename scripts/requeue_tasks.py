#!/usr/bin/env python3
"""Manually requeue failed collection tasks after an operator review."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def requeue(db_path: Path, *, asin: str | None, reason: str, include_blocked: bool = False, limit: int = 100, dry_run: bool = False) -> dict[str, Any]:
    reason = reason.strip()
    if not reason:
        raise ValueError("--reason is required")
    if limit < 1:
        raise ValueError("--limit must be >= 1")
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    statuses = ["failed"] + (["blocked"] if include_blocked else [])
    marks = ",".join("?" for _ in statuses)
    params: list[Any] = [*statuses]
    query = f"SELECT asin,status,next_review_url FROM item_state WHERE marketplace='US' AND status IN ({marks})"
    if asin:
        query += " AND asin=?"
        params.append(asin.upper())
    query += " ORDER BY asin LIMIT ?"
    params.append(limit)
    conn = sqlite3.connect(str(db_path), timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(query, params).fetchall()
        updated: list[str] = []
        if not dry_run:
            with conn:
                for row in rows:
                    stage = "reviews" if row["next_review_url"] else "product"
                    resume = "reviews_pending" if stage == "reviews" else "pending"
                    conn.execute(
                        "UPDATE item_state SET status='pending',attempts=0,resume_status=?,task_stage=?,block_reason=NULL,last_error=NULL,updated_at=datetime('now') WHERE marketplace='US' AND asin=?",
                        (resume, stage, row["asin"]),
                    )
                    conn.execute(
                        "INSERT INTO state_history(marketplace,asin,from_status,to_status,reason,changed_at) VALUES(?,?,?,?,?,datetime('now'))",
                        ("US", row["asin"], row["status"], "pending", f"manual_requeue:{reason}"),
                    )
                    updated.append(row["asin"])
        else:
            updated = [row["asin"] for row in rows]
        return {"schema_version": "amazon-us-requeue-v1", "reason": reason, "include_blocked": include_blocked, "dry_run": dry_run, "selected_count": len(rows), "updated_count": len(updated), "asins": updated}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("state/amazon_us.sqlite3"))
    parser.add_argument("--asin")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--include-blocked", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true", help="Preview eligible tasks without changing SQLite")
    args = parser.parse_args(argv)
    try:
        result = requeue(args.db, asin=args.asin, reason=args.reason, include_blocked=args.include_blocked, limit=args.limit, dry_run=args.dry_run)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
