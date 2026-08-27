#!/usr/bin/env python3
"""Replay the local SQLite snapshot into the PostgreSQL production schema."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

TABLES = ("item_state", "state_history", "collection_evidence", "product_snapshot", "product_snapshot_history", "media_asset", "content_module", "review_summary", "review_record", "refresh_request")


def _json_value(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def read_sqlite(sqlite_path: Path) -> dict[str, list[dict[str, Any]]]:
    if not sqlite_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {sqlite_path}")
    conn = sqlite3.connect(str(sqlite_path), timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        available = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")] if table in available else [] for table in TABLES}
    finally:
        conn.close()


def build_payload(sqlite_path: Path, tenant_id: str = "default", subject_type: str = "candidate") -> dict[str, list[dict[str, Any]]]:
    if subject_type not in {"own", "competitor", "candidate"}:
        raise ValueError("subject_type must be own, competitor, or candidate")
    source = read_sqlite(sqlite_path)
    states = source["item_state"]
    payload: dict[str, list[dict[str, Any]]] = {"asin_master": [], **{table: [] for table in TABLES}}
    for row in states:
        common = {"tenant_id": tenant_id, "marketplace": row["marketplace"], "asin": row["asin"], "subject_type": subject_type}
        payload["asin_master"].append({**common, "source_type": "sqlite_migration", "source_url": row["url"], "priority": 50, "refresh_policy": "standard", "active_status": "active"})
        payload["item_state"].append({**common, **{key: row.get(key) for key in ("url", "status", "attempts", "max_attempts", "resume_status", "task_stage", "next_review_url", "next_review_page", "next_retry_at", "review_page_limit", "reported_rating_count", "reported_review_count", "reported_count_source", "fetched_review_count", "review_pages_fetched", "block_reason", "last_error", "updated_at")}})
    for row in source["state_history"]:
        payload["state_history"].append({"tenant_id": tenant_id, "marketplace": row["marketplace"], "asin": row["asin"], "subject_type": subject_type, "from_status": row["from_status"], "to_status": row["to_status"], "reason": row["reason"], "changed_at": row["changed_at"]})
    for row in source["collection_evidence"]:
        payload["collection_evidence"].append({"tenant_id": tenant_id, "marketplace": row["marketplace"], "asin": row["asin"], "subject_type": subject_type, **{key: row.get(key) for key in ("run_id", "url", "http_status", "transfer_bytes", "retrieved_at", "source_type", "content_hash", "raw_html_path", "block_reason", "parser_version", "error_code", "context_json")}})
    for row in source["product_snapshot"]:
        payload["product_snapshot"].append({
            "tenant_id": tenant_id, "marketplace": row["marketplace"], "asin": row["asin"], "subject_type": subject_type,
            "canonical_url": row["canonical_url"], "availability": row["availability"], "title": row["title"], "brand": row["brand"], "rating": row["rating"],
            "reported_rating_count": row["reported_rating_count"], "reported_review_count": row["reported_review_count"], "review_count": row["review_count"], "review_count_source": row["review_count_source"], "price": row["price"],
            "bullets": _json_value(row["bullets_json"], []), "product_description": row["product_description"], "specs": _json_value(row["specs_json"], {}), "buy_box": _json_value(row["buy_box_json"], {}), "top_reviews": _json_value(row["top_reviews_json"], []),
            "review_link": row["review_link"], "review_section_anchor": row["review_section_anchor"], "aplus_present": bool(row["aplus_present"]), "collected_at": row["collected_at"], "status": row["status"],
        })
    for row in source["product_snapshot_history"]:
        data = _json_value(row.get("value_json"), {})
        if not isinstance(data, dict):
            continue
        payload["product_snapshot"].append({
            "tenant_id": tenant_id, "marketplace": row["marketplace"], "asin": row["asin"], "subject_type": subject_type,
            "canonical_url": data.get("canonical_url", ""), "availability": data.get("availability", ""), "title": data.get("title", ""), "brand": data.get("brand", ""), "rating": data.get("rating", ""),
            "reported_rating_count": data.get("reported_rating_count"), "reported_review_count": data.get("reported_review_count"), "review_count": data.get("review_count", ""), "review_count_source": data.get("review_count_source", ""), "price": data.get("price", ""),
            "bullets": data.get("bullets", []), "product_description": data.get("product_description", ""), "specs": data.get("specs", {}), "buy_box": data.get("buy_box", {}), "top_reviews": data.get("top_reviews", []),
            "review_link": data.get("review_link", ""), "review_section_anchor": data.get("review_section_anchor", ""), "aplus_present": bool(data.get("aplus_present")), "collected_at": row["captured_at"], "status": "product_done",
        })
    for table in ("media_asset", "content_module", "review_summary", "review_record"):
        for row in source[table]:
            item = {"tenant_id": tenant_id, "marketplace": row["marketplace"], "asin": row["asin"], "subject_type": subject_type}
            item.update({key: value for key, value in row.items() if key not in {"marketplace", "asin"}})
            if table == "review_record":
                item["review_images"] = _json_value(item.pop("review_images_json", None), [])
            payload[table].append(item)
    for row in source["refresh_request"]:
        payload["refresh_request"].append({
            "job_id": row["job_id"], "tenant_id": tenant_id, "marketplace": row["marketplace"],
            "asin": row["asin"], "subject_type": subject_type, "requested_by": row["requested_by"],
            "reason": row["reason"], "status": row["status"], "requested_at": row["requested_at"],
        })
    return payload


def _connect_postgres(dsn: str):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("PostgreSQL migration requires optional dependency psycopg") from exc
    return psycopg.connect(dsn)


BOOLEAN_COLUMNS = {"is_primary", "verified", "body_truncated", "aplus_present"}
INTEGER_COLUMNS = {"priority", "next_review_page", "review_page_limit", "reported_rating_count", "reported_review_count", "fetched_review_count", "review_pages_fetched", "position", "order_index", "ordinal", "page", "transfer_bytes"}


def _adapt_postgres_value(value: Any, column: str | None = None) -> Any:
    if column in INTEGER_COLUMNS and value == "":
        return None
    if column == "context_json" and value in (None, ""):
        value = {}
    if column in BOOLEAN_COLUMNS:
        return None if value is None else bool(value)
    if not isinstance(value, (dict, list)):
        return value
    try:
        from psycopg.types.json import Jsonb
    except ImportError:
        return json.dumps(value, ensure_ascii=False)
    return Jsonb(value)


def migrate(sqlite_path: Path, dsn: str, schema_path: Path | None = None, tenant_id: str = "default", subject_type: str = "candidate", connect=None) -> dict[str, int]:
    payload = build_payload(sqlite_path, tenant_id, subject_type)
    schema = schema_path.read_text(encoding="utf-8") if schema_path else ""
    connection_factory = connect or (lambda: _connect_postgres(dsn))
    with connection_factory() as connection:
        with connection.cursor() as cursor:
            if schema:
                cursor.execute(schema)
            for table, rows in payload.items():
                if not rows:
                    continue
                columns = list(rows[0])
                placeholders = ",".join(["%s"] * len(columns))
                sql = f"INSERT INTO amazon_us.{table} ({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
                cursor.executemany(sql, [[_adapt_postgres_value(row.get(column), column) for column in columns] for row in rows])
        connection.commit()
    return {table: len(rows) for table, rows in payload.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, default=Path("state/amazon_us.sqlite3"))
    parser.add_argument("--dsn", default="")
    parser.add_argument("--schema", type=Path, default=Path("schema/postgres_schema.sql"))
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--subject-type", choices=("own", "competitor", "candidate"), default="candidate")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = build_payload(args.sqlite, args.tenant_id, args.subject_type)
        counts = {table: len(rows) for table, rows in payload.items()}
        if not args.dry_run:
            if not args.dsn:
                raise ValueError("--dsn is required unless --dry-run is used")
            counts = migrate(args.sqlite, args.dsn, args.schema, args.tenant_id, args.subject_type)
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(counts, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
