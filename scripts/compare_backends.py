"""Read-only consistency check between the SQLite POC and PostgreSQL backend.

The check intentionally compares the data exposed to Collection API callers
(task status and product availability), rather than implementation-specific
row counts. It never writes to either database and never accepts a password on
the command line; psycopg reads ``PGPASSWORD`` or the user's normal libpq
configuration.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    from collection_storage import PostgresCollectionRepository, SQLiteCollectionRepository
except ImportError:  # pragma: no cover - supports package-style invocation
    from scripts.collection_storage import PostgresCollectionRepository, SQLiteCollectionRepository


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def sample_asins(db_path: Path, marketplace: str, limit: int) -> list[str]:
    """Return a deterministic sample from the source task table."""
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            "SELECT asin FROM item_state WHERE marketplace=? ORDER BY asin LIMIT ?",
            (marketplace, limit),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _product_signature(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {"present": False}
    task = value.get("task") or {}
    evidence = value.get("evidence") or {}
    return {
        "present": True,
        "task_status": task.get("status"),
        "source": value.get("source"),
        "media": (value.get("counts") or {}).get("media", 0),
        "content_modules": (value.get("counts") or {}).get("content_modules", 0),
        "transfer_bytes": evidence.get("transfer_bytes"),
    }


def compare_repositories(sqlite_repo: Any, postgres_repo: Any, asins: list[str], marketplace: str = "US") -> dict[str, Any]:
    sqlite_status = sqlite_repo.load_job_status()
    postgres_status = postgres_repo.load_job_status()
    mismatches: list[dict[str, Any]] = []
    for asin in asins:
        left = _product_signature(sqlite_repo.load_product(marketplace, asin))
        right = _product_signature(postgres_repo.load_product(marketplace, asin))
        if left != right:
            mismatches.append({"asin": asin, "sqlite": left, "postgres": right})
    return {
        "schema_version": "amazon-us-backend-compare-v1",
        "sqlite": {"counts": sqlite_status.get("counts", {}), "refresh_requests": sqlite_status.get("refresh_requests", {})},
        "postgres": {"counts": postgres_status.get("counts", {}), "refresh_requests": postgres_status.get("refresh_requests", {})},
        "task_status_match": sqlite_status.get("counts", {}) == postgres_status.get("counts", {}),
        "refresh_status_match": sqlite_status.get("refresh_requests", {}) == postgres_status.get("refresh_requests", {}),
        "sample_count": len(asins),
        "sample_mismatches": mismatches,
        "ok": (
            sqlite_status.get("counts", {}) == postgres_status.get("counts", {})
            and sqlite_status.get("refresh_requests", {}) == postgres_status.get("refresh_requests", {})
            and not mismatches
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare SQLite and PostgreSQL Collection API views")
    parser.add_argument("--sqlite", type=Path, default=Path("state/amazon_us.sqlite3"))
    parser.add_argument("--dsn", required=True, help="libpq DSN; keep credentials in PGPASSWORD or a password file")
    parser.add_argument("--marketplace", default="US")
    parser.add_argument("--sample-limit", type=int, default=20)
    args = parser.parse_args(argv)
    limit = max(1, min(args.sample_limit, 100))
    asins = sample_asins(args.sqlite, args.marketplace, limit)
    result = compare_repositories(
        SQLiteCollectionRepository(args.sqlite),
        PostgresCollectionRepository(args.dsn),
        asins,
        args.marketplace,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
