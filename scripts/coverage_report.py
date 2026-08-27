#!/usr/bin/env python3
"""Generate an auditable collection coverage report from SQLite."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FIELD_GROUPS = {
    "identity": ("canonical_url", "title", "brand"),
    "commercial": ("price", "availability"),
    "content": ("bullets_json", "product_description", "specs_json", "buy_box_json"),
    "social": ("rating", "reported_review_count", "review_count"),
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _present(value: Any) -> bool:
    return value is not None and str(value).strip() not in {"", "[]", "{}"}


def _counts(rows: list[sqlite3.Row], fields: tuple[str, ...]) -> dict[str, dict[str, float | int]]:
    total = len(rows)
    result: dict[str, dict[str, float | int]] = {}
    for field in fields:
        observed = sum(1 for row in rows if _present(row[field]))
        result[field] = {"observed": observed, "total": total, "rate": round(observed / total, 4) if total else 0.0}
    return result


def build_report(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        product_rows = list(conn.execute("SELECT * FROM product_snapshot"))
        status_rows = conn.execute("SELECT status, COUNT(*) AS count FROM item_state GROUP BY status ORDER BY status").fetchall()
        source_rows = conn.execute("SELECT COALESCE(source_type, 'unknown') AS key, COUNT(*) AS count FROM collection_evidence GROUP BY source_type ORDER BY source_type").fetchall()
        block_rows = conn.execute("SELECT COALESCE(block_reason, 'none') AS key, COUNT(*) AS count FROM collection_evidence GROUP BY block_reason ORDER BY block_reason").fetchall()
        table_counts = {}
        for table in ("product_snapshot", "media_asset", "content_module", "review_summary", "review_record", "collection_evidence"):
            table_counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return {
            "schema_version": "amazon-us-coverage-v1",
            "generated_at": _now(),
            "database": str(db_path),
            "task_status": {row["status"]: row["count"] for row in status_rows},
            "product_count": len(product_rows),
            "field_coverage": {group: _counts(product_rows, fields) for group, fields in FIELD_GROUPS.items()},
            "table_counts": table_counts,
            "evidence_source": {row["key"]: row["count"] for row in source_rows},
            "evidence_block_reason": {row["key"]: row["count"] for row in block_rows},
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("state/amazon_us.sqlite3"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = build_report(args.db)
    except (OSError, sqlite3.Error) as exc:
        print(f"error: {exc}")
        return 1
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
