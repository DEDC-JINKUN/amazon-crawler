"""Write a sanitized Operator progress/result projection from PostgreSQL."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from operator_lifecycle import batch_run_ids
from collection_console import classify_evidence_outcome


ID_RE = re.compile(r"batch-[0-9a-f]{24}")
TENANT_RE = re.compile(r"[A-Za-z0-9_.-]{1,120}")


def atomic(path: Path, value) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.02 * (attempt + 1))


def safe_error(value, fallback):
    text = str(value or "").strip().lower()
    approved = (
        "fetch_error", "agent_refresh_worker_failed", "missing_core_fields", "asin_mismatch",
        "context_mismatch", "empty_review_page", "captcha", "robot", "access_control",
        "too_many_requests", "recovery_", "capacity_", "operator_",
    )
    return text[:120] if text and re.fullmatch(r"[a-z0-9_.:-]{1,120}", text) and text.startswith(approved) else fallback


def excel_safe(value):
    """Keep JSON values intact while neutralizing spreadsheet formulas in CSV."""
    if value is None or not isinstance(value, str):
        return value
    effective = value.lstrip()
    return "'" + value if effective.startswith(("=", "+", "-", "@")) else value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("status", "finalize"))
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--subject-type", choices=("own", "competitor", "candidate"), required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    args = parser.parse_args(argv)
    batch_dir = args.batch_dir.resolve()
    manifest = args.manifest.resolve()
    if not ID_RE.fullmatch(args.batch_id) or not TENANT_RE.fullmatch(args.tenant_id):
        return 2
    if batch_dir.name != args.batch_id or manifest != batch_dir / "manifest.csv":
        return 2
    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        return 2
    try:
        import psycopg
        from psycopg.rows import dict_row
        with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
            asins = [str(row["asin"]) for row in csv.DictReader(handle)]
        with psycopg.connect(
            dsn,
            row_factory=dict_row,
            connect_timeout=3,
            options="-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=1000",
        ) as connection:
            rows = [dict(row) for row in connection.execute(
                """
                SELECT s.asin,s.status,s.last_error,s.block_reason,
                       p.title,p.price,p.availability,p.rating,p.reported_review_count,
                       EXISTS(
                         SELECT 1 FROM amazon_us.state_history h
                         WHERE h.tenant_id=s.tenant_id AND h.asin=s.asin
                           AND h.subject_type=s.subject_type AND h.reason='variant_redirect'
                       ) AS variant_redirect
                FROM amazon_us.item_state s
                LEFT JOIN amazon_us.product_latest p
                  ON p.tenant_id=s.tenant_id AND p.marketplace=s.marketplace
                 AND p.asin=s.asin AND p.subject_type=s.subject_type
                WHERE s.tenant_id=%s AND s.marketplace='US' AND s.subject_type=%s AND s.asin=ANY(%s)
                """,
                (args.tenant_id, args.subject_type, asins),
            ).fetchall()]
            run_ids = batch_run_ids(batch_dir)
            evidence = []
            if run_ids:
                evidence = [dict(row) for row in connection.execute(
                    "SELECT DISTINCT ON (asin) * FROM amazon_us.collection_evidence "
                    "WHERE tenant_id=%s AND marketplace='US' AND subject_type=%s "
                    "AND asin=ANY(%s) AND run_id=ANY(%s) ORDER BY asin,retrieved_at DESC,id DESC",
                    (args.tenant_id, args.subject_type, asins, run_ids),
                ).fetchall()]
    except Exception:
        atomic(batch_dir / "progress.json", {
            "status": "running", "requested": len(asins) if "asins" in locals() else 0,
            "claimed": 0, "completed": 0, "variant": 0, "failed": 0, "blocked": 0,
            "pending": len(asins) if "asins" in locals() else 0,
            "latest_error": "status_projection_unavailable", "items": [],
        })
        return 2
    by_asin = {row["asin"]: row for row in rows}
    evidence_by_asin = {row['asin']: row for row in evidence}
    items = []
    counts = {"claimed": 0, "completed": 0, "variant": 0, "failed": 0, "blocked": 0, "pending": 0}
    results = []
    latest_error = None
    for asin in asins:
        row = by_asin.get(asin)
        historical = row
        current = evidence_by_asin.get(asin)
        if current is None:
            counts['pending'] += 1
            items.append({'asin': asin, 'status': 'pending', 'error': None, 'evidence_scope': 'none',
                          'history_available': historical is not None})
            results.append({'asin': asin, 'status': 'pending', 'error': None,
                            'history': historical, 'evidence_scope': 'none'})
            continue
        outcome = classify_evidence_outcome(current)
        row = dict(row or {})
        row['variant_redirect'] = outcome == 'variant_redirect'
        row['status'] = {'completed':'succeeded','failed':'failed','blocked':'blocked'}.get(outcome, 'pending')
        row['last_error'] = current.get('error_code')
        row['block_reason'] = current.get('block_reason')
        state = str(row.get("status") if row else "pending")
        if row and row.get("variant_redirect"):
            public_status = "variant"
        elif state == "running":
            public_status = "claimed"
        elif state in {"succeeded", "product_done", "reviews_pending"}:
            public_status = "completed"
        elif state == "blocked":
            public_status = "blocked"
        elif state == "failed":
            public_status = "failed"
        else:
            public_status = "pending"
        counts[public_status] += 1
        error = None
        if public_status == "blocked":
            error = safe_error(row.get("block_reason"), "access_blocked")
        elif public_status == "failed":
            error = safe_error(row.get("last_error"), "collection_failed")
        if error:
            latest_error = error
        items.append({"asin": asin, "status": public_status, "error": error})
        results.append({
            "asin": asin, "status": public_status,
            "title": row.get("title") if row else None,
            "price": row.get("price") if row else None,
            "availability": row.get("availability") if row else None,
            "rating": row.get("rating") if row else None,
            "reported_review_count": row.get("reported_review_count") if row else None,
            "error": error,
        })
    progress = {
        "status": "running", "requested": len(asins), **counts,
        "latest_error": latest_error, "items": items,
    }
    if args.mode == "finalize":
        progress["status"] = "completed" if counts["pending"] == 0 and counts["claimed"] == 0 else "failed"
        progress["finished_at"] = datetime.now(timezone.utc).isoformat()
        atomic(batch_dir / "results.json", {"schema_version": "amazon-operator-results-v1", "items": results})
        with (batch_dir / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["asin", "status", "title", "price", "availability", "rating", "reported_review_count", "error"])
            writer.writeheader()
            writer.writerows([{key: excel_safe(row.get(key)) for key in writer.fieldnames} for row in results])
    atomic(batch_dir / "progress.json", progress)
    return 0 if args.mode == "status" or progress["status"] == "completed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
