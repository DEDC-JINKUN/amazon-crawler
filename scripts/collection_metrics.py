#!/usr/bin/env python3
"""Summarize one crawler run from SQLite evidence without changing the DB."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TABLES = ("product_snapshot", "media_asset", "content_module", "review_summary", "review_record")


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def build_report(db_path: Path, raw_html_dir: Path | None = None, run_id: str | None = None) -> dict[str, Any]:
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        conn.row_factory = sqlite3.Row
        if run_id is None:
            row = conn.execute("SELECT run_id FROM collection_evidence ORDER BY id DESC LIMIT 1").fetchone()
            run_id = row["run_id"] if row else None
        evidence = list(conn.execute(
            "SELECT asin,source_type,http_status,retrieved_at,error_code,block_reason,raw_html_path FROM collection_evidence WHERE run_id=? ORDER BY id",
            (run_id,),
        )) if run_id else []
        asins = sorted({row["asin"] for row in evidence})
        statuses = Counter()
        if asins:
            marks = ",".join("?" for _ in asins)
            statuses.update(row["status"] for row in conn.execute(f"SELECT status FROM item_state WHERE marketplace='US' AND asin IN ({marks})", asins))
        table_counts = {}
        for table in TABLES:
            table_counts[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE marketplace='US' AND asin IN ({','.join('?' for _ in asins)})", asins
            ).fetchone()[0] if asins else 0
    finally:
        conn.close()
    started = _parse_time(evidence[0]["retrieved_at"]) if evidence else None
    finished = _parse_time(evidence[-1]["retrieved_at"]) if evidence else None
    elapsed = max(0.0, (finished - started).total_seconds()) if started and finished else None
    bytes_total = 0
    readable_files = 0
    if raw_html_dir:
        for row in evidence:
            if not row["raw_html_path"]:
                continue
            path = raw_html_dir / str(row["raw_html_path"])
            try:
                bytes_total += path.stat().st_size
                readable_files += 1
            except OSError:
                pass
    page_count = len(evidence)
    success_pages = sum(1 for row in evidence if row["http_status"] and 200 <= int(row["http_status"]) < 300 and not row["error_code"] and not row["block_reason"])
    return {
        "schema_version": "amazon-us-collection-metrics-v1",
        "run_id": run_id,
        "evidence_count": page_count,
        "unique_asin_count": len(asins),
        "source_counts": dict(Counter(row["source_type"] or "unknown" for row in evidence)),
        "http_status_counts": dict(Counter(str(row["http_status"] or "unknown") for row in evidence)),
        "error_counts": dict(Counter(row["error_code"] or row["block_reason"] or "none" for row in evidence)),
        "task_status_counts": dict(statuses),
        "success_page_count": success_pages,
        "table_counts": table_counts,
        "bytes_total": bytes_total,
        "raw_files_read": readable_files,
        "elapsed_seconds": elapsed,
        "pages_per_second": round(page_count / elapsed, 4) if elapsed and elapsed > 0 else None,
        "successful_pages_per_second": round(success_pages / elapsed, 4) if elapsed and elapsed > 0 else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("state/amazon_us.sqlite3"))
    parser.add_argument("--raw-html-dir", type=Path)
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.db, args.raw_html_dir, args.run_id)
    except (OSError, sqlite3.Error) as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
