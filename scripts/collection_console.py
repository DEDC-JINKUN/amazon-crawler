#!/usr/bin/env python3
"""Loopback-only, read-only PostgreSQL operations console across crawler tenants."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "console"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
TENANT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
ITEM_PATH = re.compile(r"^/api/items/([A-Za-z0-9]{10})$")
RUN_PATH = re.compile(r"^/api/runs/([A-Za-z0-9_-]+)$")
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
}
SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
RUNTIME_FINGERPRINT = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _context_value(row: dict[str, Any]) -> dict[str, Any]:
    context = row.get("context_json") or {}
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return context if isinstance(context, dict) else {}


def summarize_context_quality(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"full": 0, "partial": 0, "invalid": 0, "unknown": 0}
    for row in rows:
        quality = str(_context_value(row).get("context_quality") or "unknown")
        counts[quality if quality in counts else "unknown"] += 1
    return counts


def latest_proxy_session_pool(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in reversed(rows):
        value = _context_value(row).get("proxy_session_pool")
        if isinstance(value, dict):
            return dict(value)
    return None


def latest_capacity_authorization(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in reversed(rows):
        value = _context_value(row).get("capacity_authorization")
        if isinstance(value, dict):
            return dict(value)
    return None


def project_proxy_connectivity(authorization: dict[str, Any] | None) -> dict[str, Any]:
    authorization = authorization or {}
    snapshot = authorization.get("capacity_snapshot") or {}
    return {
        "egress_profile": "proxy_sessions" if authorization else None,
        "canary_operation_id": authorization.get("canary_operation_id"),
        "canary_status": snapshot.get("canary_status"),
        "tested_slots": snapshot.get("tested_slots"),
        "available_slots": snapshot.get("available_slots"),
        "unique_egress_count": snapshot.get("unique_egress_count"),
        "slot_capacity": snapshot.get("slot_capacity"),
        "gate_status": snapshot.get("capacity_gate_status"),
        "gate_reason": snapshot.get("capacity_gate_reason"),
        "fact_expires_at": authorization.get("fact_expires_at"),
    }


def project_amazon_business(
    items: list[dict[str, Any]],
    *,
    requested_actions: int | None,
    recorded_actions: int | None,
    unrequested_actions: int | None,
) -> dict[str, Any]:
    counts = {
        outcome: sum(item.get("outcome") == outcome for item in items)
        for outcome in ("completed", "variant_redirect", "failed", "blocked")
    }
    access_control_rate = (
        round(counts["blocked"] / recorded_actions, 4)
        if recorded_actions not in {None, 0} else None
    )
    return {
        "requested_actions": requested_actions,
        "recorded_actions": recorded_actions,
        "completed_actions": counts["completed"],
        "variant_redirect_actions": counts["variant_redirect"],
        "failed_actions": counts["failed"],
        "blocked_actions": counts["blocked"],
        "unrequested_actions": unrequested_actions,
        "access_control_rate": access_control_rate,
    }


def _explicit_sibling_identity(row: dict[str, Any]) -> bool:
    identity = _context_value(row).get("identity") or {}
    if not isinstance(identity, dict):
        return False
    requested = str(identity.get("requested_asin") or row.get("asin") or "").upper()
    observed = str(identity.get("observed_asin") or "").upper()
    canonical = str(identity.get("canonical_asin") or "").upper()
    parent = str(identity.get("parent_asin") or "").upper()
    children = {str(value).upper() for value in identity.get("child_asins") or []}
    return bool(
        ASIN_RE.fullmatch(requested)
        and ASIN_RE.fullmatch(observed)
        and ASIN_RE.fullmatch(parent)
        and requested != observed
        and canonical == observed
        and requested in children
        and observed in children
    )


def classify_evidence_outcome(row: dict[str, Any]) -> str:
    if row.get("block_reason"):
        return "blocked"
    if row.get("error_code") == "asin_mismatch" and _explicit_sibling_identity(row):
        return "variant_redirect"
    if row.get("error_code"):
        return "failed"
    return "completed"


def project_price_status(product: dict[str, Any] | None) -> str:
    product = product or {}
    if str(product.get("price") or "").strip():
        return "available"
    availability = str(product.get("availability") or "").strip()
    buy_box = product.get("buy_box") or {}
    buy_box_text = str(buy_box.get("text") or "") if isinstance(buy_box, dict) else str(buy_box)
    actionable_buy_box = bool(
        isinstance(buy_box, dict) and any(buy_box.get(key) for key in ("seller", "ships_from", "coupon"))
    ) or bool(re.search(r"(?:add\s+to\s+cart|buy\s+now)", buy_box_text, flags=re.IGNORECASE))
    if not actionable_buy_box and re.search(
        r"(?:currently\s+unavailable|temporarily\s+out\s+of\s+stock|not\s+available)",
        availability,
        flags=re.IGNORECASE,
    ):
        return "unavailable"
    return "missing"


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def project_batch_durations(runs: list[dict[str, Any]]) -> dict[str, float | bool | None]:
    completed = [run for run in runs if _coerce_datetime(run.get("started_at")) and _coerce_datetime(run.get("finished_at"))]
    active = round(sum(float(run.get("duration_seconds") or 0) for run in completed), 2)
    starts = [_coerce_datetime(run.get("started_at")) for run in completed]
    finishes = [_coerce_datetime(run.get("finished_at")) for run in completed]
    wall = round((max(finishes) - min(starts)).total_seconds(), 2) if starts and finishes else None
    return {
        "active_duration_seconds": active if completed else None,
        "wall_span_seconds": wall,
        "wall_span_includes_idle": bool(completed),
    }


def project_run_durations(
    ledger: dict[str, Any] | None,
    observed_start: Any = None,
    observed_end: Any = None,
) -> dict[str, Any]:
    ledger = ledger or {}
    started = _coerce_datetime(ledger.get("started_at")) or _coerce_datetime(observed_start)
    finished = _coerce_datetime(ledger.get("finished_at")) or _coerce_datetime(observed_end)
    worker_duration = round((finished - started).total_seconds(), 2) if started and finished else None
    receipt = ledger.get("receipt_json") or {}
    if isinstance(receipt, str):
        try:
            receipt = json.loads(receipt)
        except (TypeError, ValueError, json.JSONDecodeError):
            receipt = {}
    try:
        controller_duration = round(float(receipt.get("elapsed_seconds")), 2) if receipt.get("elapsed_seconds") is not None else None
    except (TypeError, ValueError):
        controller_duration = None
    source = "collection_run.started_at_to_finished_at" if ledger else "collection_evidence.first_to_last"
    if worker_duration is not None and controller_duration is not None and worker_duration > controller_duration:
        worker_duration = controller_duration
        source = "receipt_json.elapsed_seconds_backfill_cap"
        started = _coerce_datetime(receipt.get("started_at")) or started
        finished = _coerce_datetime(receipt.get("finished_at")) or finished
    return {
        "worker_duration_seconds": worker_duration,
        "controller_duration_seconds": controller_duration,
        "duration_source": source,
        "effective_started_at": started,
        "effective_finished_at": finished,
    }


def summarize_traffic(rows: list[dict[str, Any]]) -> dict[str, dict[str, int | None]]:
    accumulators = {
        "http_compressed_response": {"known_bytes": 0, "known_records": 0, "unknown_records": 0},
        "firefox_main_document": {"known_bytes": 0, "known_records": 0, "unknown_records": 0},
        "firefox_subresources": {"known_bytes": 0, "known_records": 0, "unknown_records": 0},
    }
    for row in rows:
        context = _context_value(row)
        context_traffic = context.get("traffic") if isinstance(context, dict) else {}
        context_traffic = context_traffic if isinstance(context_traffic, dict) else {}
        source = str(row.get("source_type") or "unknown")
        http_applicable = source == "http_html" or "http_compressed_response_bytes" in context_traffic
        if http_applicable:
            value = context_traffic.get("http_compressed_response_bytes")
            if value is None and source == "http_html":
                value = row.get("transfer_bytes")
            if value is None:
                accumulators["http_compressed_response"]["unknown_records"] += 1
            else:
                accumulators["http_compressed_response"]["known_bytes"] += max(0, int(value))
                accumulators["http_compressed_response"]["known_records"] += 1
        for category, byte_key, unknown_key, known_key in (
                ("firefox_main_document", "firefox_main_document_bytes", "firefox_main_document_unknown_count", "firefox_main_document_known_count"),
                ("firefox_subresources", "firefox_subresource_bytes", "firefox_subresource_unknown_count", "firefox_subresource_known_count"),
            ):
            applicable = source == "selenium_dom" or any(
                key in context_traffic for key in (byte_key, unknown_key, known_key)
            )
            if not applicable:
                continue
            value = context_traffic.get(byte_key)
            unknown = int(context_traffic.get(unknown_key) or 0)
            if value is None:
                accumulators[category]["unknown_records"] += max(1, unknown)
            else:
                accumulators[category]["known_bytes"] += max(0, int(value))
                accumulators[category]["known_records"] += 1
                accumulators[category]["unknown_records"] += unknown
    result = {
        category: {
            "bytes": None if values["unknown_records"] else values["known_bytes"],
            "known_records": values["known_records"],
            "unknown_records": values["unknown_records"],
        }
        for category, values in accumulators.items()
    }
    result["proxy_dashboard_bill"] = {"bytes": None, "known_records": 0, "unknown_records": 1}
    return result


class PostgresConsoleRepository:
    """Purpose-built, read-only query surface for the local operations UI."""

    def __init__(self, dsn: str, tenant_id: str | None = None):
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN must not be empty")
        self.dsn = dsn
        self.tenant_id = (tenant_id or "").strip()
        if self.tenant_id and not TENANT_RE.fullmatch(self.tenant_id):
            raise ValueError("invalid tenant_id")

    def for_tenant(self, tenant_id: str):
        tenant_id = str(tenant_id or "").strip()
        if not TENANT_RE.fullmatch(tenant_id):
            return None
        return type(self)(self.dsn, tenant_id)

    def _require_tenant(self) -> str:
        if not self.tenant_id:
            raise ValueError("tenant selection is required")
        return self.tenant_id

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL console requires optional dependency psycopg") from exc
        return psycopg.connect(
            self.dsn,
            row_factory=dict_row,
            options="-c default_transaction_read_only=on",
        )

    @staticmethod
    def _counts(cursor, sql: str, params: tuple[Any, ...]) -> dict[str, int]:
        cursor.execute(sql, params)
        return {str(row["key"]): int(row["count"]) for row in cursor.fetchall()}

    def list_tenants(self) -> list[dict[str, Any]]:
        state: dict[str, dict[str, Any]] = {}
        run_timings: dict[str, list[dict[str, Any]]] = {}
        operation_tenants: set[str] = set()
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT tenant_id,status,COUNT(*) AS count FROM amazon_us.item_state "
                "GROUP BY tenant_id,status ORDER BY tenant_id,status"
            )
            for row in cursor.fetchall():
                item = state.setdefault(str(row["tenant_id"]), {"status_counts": {}, "requested": 0})
                count = int(row["count"])
                item["status_counts"][str(row["status"])] = count
                item["requested"] += count
            cursor.execute(
                "SELECT tenant_id,COUNT(*) AS count FROM amazon_us.product_latest GROUP BY tenant_id"
            )
            product_counts = {str(row["tenant_id"]): int(row["count"]) for row in cursor.fetchall()}
            cursor.execute(
                """
                WITH latest AS (
                  SELECT DISTINCT ON (tenant_id,marketplace,asin,subject_type)
                         tenant_id,asin,error_code,block_reason,context_json,retrieved_at
                  FROM amazon_us.collection_evidence
                  ORDER BY tenant_id,marketplace,asin,subject_type,id DESC
                ), classified AS (
                  SELECT *,COALESCE((
                    error_code='asin_mismatch'
                    AND context_json->'identity'->>'requested_asin'=asin
                    AND context_json->'identity'->>'observed_asin'<>asin
                    AND context_json->'identity'->>'canonical_asin'=context_json->'identity'->>'observed_asin'
                    AND COALESCE(context_json->'identity'->>'parent_asin','')<>''
                    AND jsonb_typeof(context_json->'identity'->'child_asins')='array'
                    AND (context_json->'identity'->'child_asins') ? (context_json->'identity'->>'requested_asin')
                    AND (context_json->'identity'->'child_asins') ? (context_json->'identity'->>'observed_asin')
                  ),FALSE) AS is_variant
                  FROM latest
                )
                SELECT tenant_id,COUNT(*) AS recorded,MAX(retrieved_at) AS latest_at,
                       COUNT(*) FILTER (WHERE block_reason IS NOT NULL) AS blocked,
                       COUNT(*) FILTER (WHERE block_reason IS NULL AND is_variant) AS variant_redirect,
                       COUNT(*) FILTER (WHERE block_reason IS NULL AND error_code IS NOT NULL AND NOT is_variant) AS failed
                FROM classified GROUP BY tenant_id
                """
            )
            outcome_counts = {str(row["tenant_id"]): dict(row) for row in cursor.fetchall()}
            cursor.execute(
                """
                SELECT tenant_id,COUNT(*) AS evidence_actions,COUNT(transfer_bytes) AS known_transfer_records,
                       COALESCE(SUM(transfer_bytes),0) AS known_transfer_bytes,
                       MIN(retrieved_at) AS started_at,MAX(retrieved_at) AS ended_at,
                       COUNT(*) FILTER (WHERE source_type='http_html') AS http_actions,
                       COUNT(*) FILTER (WHERE source_type='selenium_dom') AS firefox_actions
                FROM amazon_us.collection_evidence GROUP BY tenant_id
                """
            )
            traffic = {str(row["tenant_id"]): dict(row) for row in cursor.fetchall()}
            cursor.execute("SELECT to_regclass('amazon_us.collection_run') AS relation")
            ledger_rows: dict[str, dict[str, Any]] = {}
            if cursor.fetchone()["relation"] is not None:
                cursor.execute(
                    """
                    SELECT DISTINCT ON (tenant_id) tenant_id,requested_actions,status,started_at,finished_at
                    FROM amazon_us.collection_run ORDER BY tenant_id,started_at DESC
                    """
                )
                ledger_rows = {str(row["tenant_id"]): dict(row) for row in cursor.fetchall()}
                cursor.execute(
                    """
                    SELECT tenant_id,started_at,finished_at,
                           CASE
                             WHEN receipt_json->>'elapsed_seconds' ~ '^[0-9]+([.][0-9]+)?$'
                             THEN LEAST(
                               EXTRACT(EPOCH FROM (finished_at-started_at)),
                               (receipt_json->>'elapsed_seconds')::numeric
                             )
                             ELSE EXTRACT(EPOCH FROM (finished_at-started_at))
                           END AS duration_seconds
                    FROM amazon_us.collection_run WHERE finished_at IS NOT NULL
                    ORDER BY tenant_id,started_at
                    """
                )
                for row in cursor.fetchall():
                    run_timings.setdefault(str(row["tenant_id"]), []).append(dict(row))
            cursor.execute("SELECT to_regclass('amazon_us.operation_run') AS relation")
            if cursor.fetchone()["relation"] is not None:
                cursor.execute("SELECT DISTINCT tenant_id FROM amazon_us.operation_run")
                operation_tenants = {str(row["tenant_id"]) for row in cursor.fetchall()}
        results: list[dict[str, Any]] = []
        tenant_ids = set(state) | set(product_counts) | set(outcome_counts) | set(traffic) | set(ledger_rows) | operation_tenants
        for tenant_id in tenant_ids:
            item = state.get(tenant_id) or {"status_counts": {}, "requested": 0}
            outcomes = outcome_counts.get(tenant_id) or {}
            metrics = traffic.get(tenant_id, {})
            ledger = ledger_rows.get(tenant_id) or {}
            requested = int(item.get("requested") or ledger.get("requested_actions") or 0)
            recorded = int(outcomes.get("recorded") or 0)
            running = int((item.get("status_counts") or {}).get("running") or 0)
            started_at = metrics.get("started_at") or ledger.get("started_at")
            ended_at = metrics.get("ended_at") or ledger.get("finished_at") or ledger.get("started_at")
            durations = project_batch_durations(run_timings.get(tenant_id) or [])
            evidence_wall_span = (
                round((metrics.get("ended_at") - metrics.get("started_at")).total_seconds(), 2)
                if metrics.get("started_at") is not None and metrics.get("ended_at") is not None else None
            )
            if evidence_wall_span is not None:
                durations["wall_span_seconds"] = evidence_wall_span
                durations["wall_span_includes_idle"] = True
            terminal_status = (
                "running" if running or ledger.get("status") == "running"
                else str(ledger.get("status")) if not item.get("requested") and ledger.get("status")
                else "complete" if requested and recorded >= requested
                else "partial" if recorded else "pending"
            )
            results.append({
                "tenant_id": tenant_id,
                "requested": requested,
                "recorded": recorded,
                "product_succeeded": int(product_counts.get(tenant_id, 0)),
                "variant_redirect": int(outcomes.get("variant_redirect") or 0),
                "failed": int(outcomes.get("failed") or 0),
                "blocked": int(outcomes.get("blocked") or 0),
                "pending": max(0, requested - recorded),
                "running": running,
                "evidence_actions": int(metrics.get("evidence_actions") or 0),
                "known_transfer_bytes": int(metrics.get("known_transfer_bytes") or 0),
                "unknown_transfer_records": int(metrics.get("evidence_actions") or 0) - int(metrics.get("known_transfer_records") or 0),
                "http_actions": int(metrics.get("http_actions") or 0),
                "firefox_actions": int(metrics.get("firefox_actions") or 0),
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_seconds": durations["active_duration_seconds"],
                **durations,
                "terminal_status": terminal_status,
            })
        results.sort(key=lambda row: (row.get("ended_at") is not None, row.get("ended_at") or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        return results

    def load_overview(self, raw_html_dir: Path | None = None) -> dict[str, Any]:
        tenant_id = self._require_tenant()
        with self._connect() as conn, conn.cursor() as cursor:
            params = (tenant_id,)
            status_counts = self._counts(
                cursor,
                "SELECT status AS key,COUNT(*) AS count FROM amazon_us.item_state "
                "WHERE tenant_id=%s GROUP BY status ORDER BY status",
                params,
            )
            stage_counts = self._counts(
                cursor,
                "SELECT task_stage AS key,COUNT(*) AS count FROM amazon_us.item_state "
                "WHERE tenant_id=%s GROUP BY task_stage ORDER BY task_stage",
                params,
            )
            source_counts = self._counts(
                cursor,
                "SELECT COALESCE(source_type,'unknown') AS key,COUNT(*) AS count "
                "FROM amazon_us.collection_evidence WHERE tenant_id=%s GROUP BY source_type ORDER BY source_type",
                params,
            )
            error_counts = self._counts(
                cursor,
                "SELECT COALESCE(error_code,'none') AS key,COUNT(*) AS count "
                "FROM amazon_us.collection_evidence WHERE tenant_id=%s GROUP BY error_code ORDER BY error_code",
                params,
            )
            block_counts = self._counts(
                cursor,
                "SELECT COALESCE(block_reason,'none') AS key,COUNT(*) AS count "
                "FROM amazon_us.collection_evidence WHERE tenant_id=%s GROUP BY block_reason ORDER BY block_reason",
                params,
            )
            cursor.execute(
                "SELECT COUNT(*) AS total FROM amazon_us.item_state WHERE tenant_id=%s",
                params,
            )
            total = int(cursor.fetchone()["total"])
            cursor.execute(
                "SELECT COUNT(DISTINCT asin) AS touched,COUNT(*) AS actions,COUNT(transfer_bytes) AS known_transfer_records,"
                "COALESCE(SUM(transfer_bytes),0) AS transfer_bytes,MAX(retrieved_at) AS last_evidence_at "
                "FROM amazon_us.collection_evidence WHERE tenant_id=%s",
                params,
            )
            evidence = dict(cursor.fetchone())
            cursor.execute(
                "SELECT source_type,transfer_bytes,context_json FROM amazon_us.collection_evidence WHERE tenant_id=%s",
                params,
            )
            evidence_rows = [dict(row) for row in cursor.fetchall()]
            traffic_summary = summarize_traffic(evidence_rows)
            context_quality_counts = summarize_context_quality(evidence_rows)
            table_counts: dict[str, int] = {}
            for name, table in (
                ("products", "product_latest"),
                ("media", "media_asset"),
                ("content", "content_module"),
                ("review_summaries", "review_summary"),
                ("reviews", "review_record"),
            ):
                cursor.execute(f"SELECT COUNT(*) AS count FROM amazon_us.{table} WHERE tenant_id=%s", params)
                table_counts[name] = int(cursor.fetchone()["count"])
            cursor.execute(
                "SELECT asin,task_stage,lease_owner,lease_expires_at FROM amazon_us.item_state "
                "WHERE tenant_id=%s AND status='running' ORDER BY asin LIMIT 50",
                params,
            )
            running = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT run_id,COUNT(*) AS actions,MIN(retrieved_at) AS started_at,MAX(retrieved_at) AS ended_at,"
                "COUNT(*) FILTER (WHERE block_reason IS NOT NULL) AS blocked,"
                "COUNT(*) FILTER (WHERE context_json->>'context_quality'='partial') AS partial "
                "FROM amazon_us.collection_evidence WHERE tenant_id=%s GROUP BY run_id "
                "ORDER BY MAX(retrieved_at) DESC LIMIT 10",
                params,
            )
            recent_runs = [dict(row) for row in cursor.fetchall()]

        files = [*raw_html_dir.rglob("*.html"), *raw_html_dir.rglob("*.html.gz")] if raw_html_dir and raw_html_dir.exists() else None
        raw_bytes = sum(path.stat().st_size for path in files) if files is not None else None
        touched = int(evidence.get("touched") or 0)
        actions = int(evidence.get("actions") or 0)
        known_transfers = int(evidence.get("known_transfer_records") or 0)
        database_rows = sum(table_counts.values())
        return {
            "schema_version": "amazon-us-console-v1",
            "tenant_id": tenant_id,
            "observed_at": datetime.now(timezone.utc),
            "status_counts": status_counts,
            "stage_counts": stage_counts,
            "source_counts": source_counts,
            "context_quality_counts": context_quality_counts,
            "error_counts": error_counts,
            "block_counts": block_counts,
            "progress": {
                "total": total,
                "touched": touched,
                "percent": round(touched / total * 100, 2) if total else 0,
                "successful_products": table_counts["products"],
            },
            "table_counts": table_counts,
            "running": running,
            "recent_runs": recent_runs,
            "four_scale_metrics": {
                "page_actions": actions,
                "successful_asins": table_counts["products"],
                "database_rows": database_rows,
                "field_values": None,
                "field_values_reason": "business definition required",
            },
            "traffic": {
                "raw_html_files": len(files) if files is not None else None,
                "saved_raw_html_bytes": raw_bytes,
                "saved_raw_html_reason": None if files is not None else "not_aggregated_across_output_directories",
                "known_http_transfer_bytes": int(evidence.get("transfer_bytes") or 0),
                "unknown_transfer_records": actions - known_transfers,
                "proxy_billed_bytes": None,
                **traffic_summary,
            },
            "last_evidence_at": evidence.get("last_evidence_at"),
        }

    def load_identity(self) -> dict[str, Any]:
        if not self.tenant_id:
            tenants = self.list_tenants()
            return {
                "tenant_count": len(tenants),
                "default_tenant_id": tenants[0]["tenant_id"] if tenants else None,
            }
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM amazon_us.item_state WHERE tenant_id=%s",
                (self.tenant_id,),
            )
            task_count = int(cursor.fetchone()["count"])
        return {"tenant_id": self.tenant_id, "task_count": task_count}

    def list_items(
        self,
        *,
        status: str | None = None,
        stage: str | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        tenant_id = self._require_tenant()
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        where = ["s.tenant_id=%s"]
        params: list[Any] = [tenant_id]
        if status:
            where.append("s.status=%s")
            params.append(status)
        if stage:
            where.append("s.task_stage=%s")
            params.append(stage)
        if query:
            term = f"%{query.strip()}%"
            where.append("(s.asin ILIKE %s OR COALESCE(p.title,'') ILIKE %s OR COALESCE(s.last_error,'') ILIKE %s)")
            params.extend([term, term, term])
        predicate = " AND ".join(where)
        join = (
            "LEFT JOIN amazon_us.product_latest p ON p.tenant_id=s.tenant_id AND p.marketplace=s.marketplace "
            "AND p.asin=s.asin AND p.subject_type=s.subject_type "
        )
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) AS count FROM amazon_us.item_state s {join} WHERE {predicate}", tuple(params))
            total = int(cursor.fetchone()["count"])
            cursor.execute(
                f"""
                SELECT s.asin,s.subject_type,s.status,s.task_stage,s.attempts,s.max_attempts,s.last_error,
                       s.block_reason,s.updated_at,s.lease_owner,s.lease_expires_at,
                       p.title,p.price,p.availability,p.buy_box,p.rating,p.reported_review_count,
                       e.source_type,e.http_status,e.error_code AS evidence_error,e.block_reason AS evidence_block,
                       e.context_json,e.raw_html_path,e.retrieved_at
                FROM amazon_us.item_state s
                {join}
                LEFT JOIN LATERAL (
                  SELECT source_type,http_status,error_code,block_reason,context_json,raw_html_path,retrieved_at
                  FROM amazon_us.collection_evidence ce
                  WHERE ce.tenant_id=s.tenant_id AND ce.marketplace=s.marketplace AND ce.asin=s.asin
                    AND ce.subject_type=s.subject_type
                  ORDER BY ce.id DESC LIMIT 1
                ) e ON TRUE
                WHERE {predicate}
                ORDER BY CASE s.status WHEN 'blocked' THEN 0 WHEN 'failed' THEN 1 WHEN 'running' THEN 2 ELSE 3 END,
                         s.updated_at DESC,s.asin
                LIMIT %s OFFSET %s
                """,
                tuple([*params, limit, offset]),
            )
            items = [dict(row) for row in cursor.fetchall()]
        for item in items:
            item["price_status"] = project_price_status(item)
            item["outcome"] = classify_evidence_outcome({
                **item,
                "error_code": item.get("evidence_error"),
                "block_reason": item.get("evidence_block") or item.get("block_reason"),
            })
        return {"tenant_id": tenant_id, "total": total, "limit": limit, "offset": offset, "items": items}

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        tenant_id = self._require_tenant()
        limit = max(1, min(int(limit), 100))
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                WITH recent AS (
                  SELECT run_id,MAX(retrieved_at) AS ended_at
                  FROM amazon_us.collection_evidence WHERE tenant_id=%s
                  GROUP BY run_id ORDER BY MAX(retrieved_at) DESC LIMIT %s
                )
                SELECT e.run_id,e.asin,e.source_type,e.transfer_bytes,e.retrieved_at,e.error_code,e.block_reason,
                       e.context_json,e.raw_html_path
                FROM amazon_us.collection_evidence e JOIN recent r ON r.run_id=e.run_id
                WHERE e.tenant_id=%s ORDER BY r.ended_at DESC,e.id
                """,
                (tenant_id, limit, tenant_id),
            )
            evidence_rows = [dict(row) for row in cursor.fetchall()]
            cursor.execute("SELECT to_regclass('amazon_us.collection_run') AS relation")
            has_ledger = cursor.fetchone()["relation"] is not None
            ledger_rows: dict[str, dict[str, Any]] = {}
            if has_ledger:
                cursor.execute(
                    "SELECT * FROM amazon_us.collection_run WHERE tenant_id=%s ORDER BY started_at DESC LIMIT %s",
                    (tenant_id, limit),
                )
                ledger_rows = {str(row["run_id"]): dict(row) for row in cursor.fetchall()}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in evidence_rows:
            grouped.setdefault(str(row["run_id"]), []).append(row)
        for run_id in ledger_rows:
            grouped.setdefault(run_id, [])
        results: list[dict[str, Any]] = []
        for run_id, rows in grouped.items():
            ledger = ledger_rows.get(run_id) or {}
            outcomes = {"completed": 0, "variant_redirect": 0, "failed": 0, "blocked": 0}
            for row in rows:
                outcomes[classify_evidence_outcome(row)] += 1
            observed_start = min((row["retrieved_at"] for row in rows), default=None)
            observed_end = max((row["retrieved_at"] for row in rows), default=None)
            started_at = ledger.get("started_at") or observed_start
            ended_at = ledger.get("finished_at") or observed_end
            requested = int(ledger.get("requested_actions") or len(rows))
            recorded = len(rows)
            status = str(ledger.get("status") or ("legacy_blocked" if outcomes["blocked"] else "legacy_complete"))
            duration_projection = project_run_durations(ledger, observed_start, observed_end)
            duration_seconds = duration_projection["worker_duration_seconds"]
            started_at = duration_projection.pop("effective_started_at")
            ended_at = duration_projection.pop("effective_finished_at")
            results.append({
                "run_id": run_id,
                "command": ledger.get("command") or "legacy",
                "requested_actions": requested,
                "recorded_actions": recorded,
                "evidence_actions": recorded,
                "unique_asins": len({row["asin"] for row in rows}),
                "product_succeeded": outcomes["completed"],
                "variant_redirect": outcomes["variant_redirect"],
                "failed": outcomes["failed"],
                "blocked": outcomes["blocked"],
                "pending": max(0, requested - recorded),
                "running": max(0, requested - recorded) if status == "running" else 0,
                "known_transfer_bytes": sum(int(row.get("transfer_bytes") or 0) for row in rows),
                "http_actions": sum(row.get("source_type") == "http_html" for row in rows),
                "firefox_actions": sum(row.get("source_type") == "selenium_dom" for row in rows),
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_seconds": duration_seconds,
                **duration_projection,
                "terminal_status": status,
                "termination_reason": ledger.get("termination_reason"),
                "capacity_authorization": ledger.get("capacity_authorization_json"),
            })
        results.sort(key=lambda row: row.get("started_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return results[:limit]

    def list_operations(self, limit: int = 100) -> list[dict[str, Any]]:
        tenant_id = self._require_tenant()
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT to_regclass('amazon_us.operation_run') AS relation")
            if cursor.fetchone()["relation"] is None:
                return []
            cursor.execute(
                """
                SELECT operation_id,tenant_id,operation_type,status,preflight_status,preflight_duration_ms,
                       failure_stage,error_class,egress_id,collection_run_id,http_status,response_bytes,
                       probe_elapsed_ms,started_at,finished_at,duration_ms,
                       canary_status,planned_slots,tested_slots,available_slots,unique_egress_count,
                       duplicate_egress_count,requested_capacity,required_slots,slot_budget,slot_capacity,
                       capacity_gate_status,capacity_gate_reason,canary_p95_latency_ms,capacity_detail_json,
                       credential_generation,authorizing_canary_operation_id,capacity_reservation_id,
                       capacity_fact_finished_at,capacity_fact_expires_at,reserved_slots,capacity_authorization_json
                FROM amazon_us.operation_run
                WHERE tenant_id=%s ORDER BY started_at DESC LIMIT %s
                """,
                (tenant_id, limit),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            cursor.execute("SELECT to_regclass('amazon_us.proxy_capacity_reservation') AS relation")
            if cursor.fetchone()["relation"] is not None:
                cursor.execute(
                    """
                    SELECT reservation_id,tenant_id,owner_id,canary_operation_id,requested_capacity,
                           required_slots,reserved_slots,slot_ids_json,status,reason,fact_finished_at,fact_expires_at,
                           expires_at,capacity_snapshot_json,created_at,released_at,updated_at,
                           CURRENT_TIMESTAMP AS observed_at
                    FROM amazon_us.proxy_capacity_reservation
                    WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s
                    """,
                    (tenant_id, limit),
                )
                for value in cursor.fetchall():
                    reservation = dict(value)
                    snapshot = dict(reservation.get("capacity_snapshot_json") or {})
                    stored_status = str(reservation.get("status") or "")
                    expired_active = bool(
                        stored_status == "active"
                        and reservation.get("expires_at") is not None
                        and reservation.get("observed_at") is not None
                        and reservation["expires_at"] <= reservation["observed_at"]
                    )
                    effective_status = "expired" if expired_active else stored_status
                    effective_reason = "reservation_expired" if expired_active else reservation.get("reason")
                    rows.append({
                        "operation_id": reservation.get("reservation_id"),
                        "tenant_id": reservation.get("tenant_id"),
                        "operation_type": "capacity_reservation",
                        "status": effective_status,
                        "preflight_status": "not_applicable",
                        "failure_stage": "capacity_gate" if effective_status in {"denied", "expired"} else None,
                        "error_class": effective_reason,
                        "egress_id": None,
                        "collection_run_id": None,
                        "http_status": None,
                        "response_bytes": None,
                        "probe_elapsed_ms": None,
                        "started_at": reservation.get("created_at"),
                        "finished_at": reservation.get("released_at") or reservation.get("updated_at"),
                        "duration_ms": None,
                        "duration_seconds": None,
                        "canary_status": snapshot.get("canary_status"),
                        "planned_slots": snapshot.get("planned_slots"),
                        "tested_slots": snapshot.get("tested_slots"),
                        "available_slots": snapshot.get("available_slots"),
                        "unique_egress_count": snapshot.get("unique_egress_count"),
                        "duplicate_egress_count": snapshot.get("duplicate_egress_count"),
                        "requested_capacity": reservation.get("requested_capacity"),
                        "required_slots": reservation.get("required_slots"),
                        "slot_budget": snapshot.get("slot_budget"),
                        "slot_capacity": snapshot.get("slot_capacity"),
                        "capacity_gate_status": "allowed" if effective_status in {"active", "released"} else "denied",
                        "capacity_gate_reason": effective_reason,
                        "canary_p95_latency_ms": snapshot.get("canary_p95_latency_ms"),
                        "authorizing_canary_operation_id": reservation.get("canary_operation_id"),
                        "capacity_reservation_id": reservation.get("reservation_id"),
                        "capacity_fact_finished_at": reservation.get("fact_finished_at"),
                        "capacity_fact_expires_at": reservation.get("fact_expires_at"),
                        "reserved_slots": reservation.get("reserved_slots"),
                        "capacity_authorization_json": {
                            "reservation_id": reservation.get("reservation_id"),
                            "canary_operation_id": reservation.get("canary_operation_id"),
                            "reserved_slots": reservation.get("reserved_slots"),
                            "slot_ids": list(reservation.get("slot_ids_json") or []),
                            "fact_finished_at": reservation.get("fact_finished_at"),
                            "fact_expires_at": reservation.get("fact_expires_at"),
                            "reservation_expires_at": reservation.get("expires_at"),
                            "capacity_snapshot": snapshot,
                        },
                    })
        for row in rows:
            if "duration_seconds" not in row:
                row["duration_seconds"] = round(float(row.get("duration_ms") or 0) / 1000, 2) if row.get("duration_ms") is not None else None
        rows.sort(key=lambda row: row.get("started_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return rows[:limit]

    def load_run(self, run_id: str) -> dict[str, Any] | None:
        tenant_id = self._require_tenant()
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT e.id,e.run_id,e.asin,e.subject_type,e.url,e.http_status,e.transfer_bytes,e.context_json,e.retrieved_at,
                       e.source_type,e.block_reason,e.error_code,e.raw_html_path,e.content_hash,
                       s.status AS current_status,s.task_stage,s.last_error,s.updated_at,
                       p.title,p.price,p.availability,p.buy_box
                FROM amazon_us.collection_evidence e
                LEFT JOIN amazon_us.item_state s ON s.tenant_id=e.tenant_id AND s.marketplace=e.marketplace
                  AND s.asin=e.asin AND s.subject_type=e.subject_type
                LEFT JOIN amazon_us.product_latest p ON p.tenant_id=e.tenant_id AND p.marketplace=e.marketplace
                  AND p.asin=e.asin AND p.subject_type=e.subject_type
                WHERE e.tenant_id=%s AND e.run_id=%s ORDER BY e.id
                """,
                (tenant_id, run_id),
            )
            evidence_rows = [dict(row) for row in cursor.fetchall()]
            cursor.execute("SELECT to_regclass('amazon_us.collection_run') AS relation")
            ledger = None
            if cursor.fetchone()["relation"] is not None:
                cursor.execute(
                    "SELECT * FROM amazon_us.collection_run WHERE tenant_id=%s AND run_id=%s",
                    (tenant_id, run_id),
                )
                ledger_row = cursor.fetchone()
                ledger = dict(ledger_row) if ledger_row is not None else None
            if not evidence_rows and ledger is None:
                return None
            if not evidence_rows:
                duration_projection = project_run_durations(ledger)
                effective_started_at = duration_projection.pop("effective_started_at")
                effective_finished_at = duration_projection.pop("effective_finished_at")
                return {
                    "schema_version": "amazon-us-console-v2",
                    "tenant_id": tenant_id,
                    "run_id": run_id,
                    "requested_actions": int(ledger.get("requested_actions") or 0),
                    "recorded_actions": 0,
                    "inferred_actions": 0,
                    "terminal_status": ledger.get("status"),
                    "capacity_authorization": ledger.get("capacity_authorization_json"),
                    "started_at": effective_started_at,
                    "ended_at": effective_finished_at,
                    **duration_projection,
                    "termination_reason": ledger.get("termination_reason"),
                    "items": [],
                }
            started_at = min(row["retrieved_at"] for row in evidence_rows)
            ended_at = max(row["retrieved_at"] for row in evidence_rows)
            evidence_asins = {row["asin"] for row in evidence_rows}
            items: list[dict[str, Any]] = []
            for row in evidence_rows:
                outcome = classify_evidence_outcome(row)
                quality = str(_context_value(row).get("context_quality") or "unknown")
                items.append({
                    **row,
                    "context_quality": quality,
                    "outcome": outcome,
                    "price_status": project_price_status(row),
                    "attribution": "evidence",
                })
            cursor.execute(
                """
                SELECT s.asin,s.subject_type,s.url,s.status AS current_status,s.task_stage,s.last_error,s.block_reason,
                       s.updated_at,p.title,p.price
                FROM amazon_us.item_state s
                LEFT JOIN amazon_us.product_latest p ON p.tenant_id=s.tenant_id AND p.marketplace=s.marketplace
                  AND p.asin=s.asin AND p.subject_type=s.subject_type
                WHERE s.tenant_id=%s AND s.updated_at BETWEEN %s AND %s
                ORDER BY s.updated_at,s.asin
                """,
                (tenant_id, started_at, ended_at),
            )
            for row_value in cursor.fetchall():
                row = dict(row_value)
                if row["asin"] in evidence_asins:
                    continue
                outcome = "blocked" if row.get("block_reason") else "failed" if row.get("last_error") else row.get("current_status")
                items.append({**row, "outcome": outcome, "attribution": "time_window_inference"})
        items.sort(key=lambda item: (item.get("retrieved_at") or item.get("updated_at"), item["asin"]))
        traffic_summary = summarize_traffic(evidence_rows)
        context_quality_counts = summarize_context_quality(evidence_rows)
        proxy_session_pool = latest_proxy_session_pool(evidence_rows)
        authorization = (ledger or {}).get("capacity_authorization_json") or latest_capacity_authorization(evidence_rows)
        requested_actions = int((ledger or {}).get("requested_actions") or len(evidence_rows))
        recorded_actions = len(evidence_rows)
        duration_projection = project_run_durations(ledger, started_at, ended_at)
        effective_started_at = duration_projection.pop("effective_started_at")
        effective_finished_at = duration_projection.pop("effective_finished_at")
        return {
            "schema_version": "amazon-us-console-v2",
            "tenant_id": tenant_id,
            "run_id": run_id,
            "started_at": effective_started_at,
            "ended_at": effective_finished_at,
            "requested_actions": requested_actions,
            "recorded_actions": recorded_actions,
            "inferred_actions": sum(1 for item in items if item["attribution"] == "time_window_inference"),
            "known_transfer_bytes": sum(int(item.get("transfer_bytes") or 0) for item in evidence_rows),
            "traffic": traffic_summary,
            "context_quality_counts": context_quality_counts,
            "proxy_session_pool": proxy_session_pool,
            "proxy_connectivity": project_proxy_connectivity(authorization),
            "amazon_business": project_amazon_business(
                items,
                requested_actions=requested_actions,
                recorded_actions=recorded_actions,
                unrequested_actions=(proxy_session_pool or {}).get("unrequested_count"),
            ),
            "outcome_counts": {
                outcome: sum(item["outcome"] == outcome for item in items)
                for outcome in ("completed", "variant_redirect", "failed", "blocked")
            },
            "items": items,
            "terminal_status": (ledger or {}).get("status") or ("legacy_blocked" if any(item["outcome"] == "blocked" for item in items) else "legacy_complete"),
            "termination_reason": (ledger or {}).get("termination_reason"),
            "capacity_authorization": authorization,
            **duration_projection,
            "warning": "time_window_inference is legacy fallback; new network failures write run evidence",
        }

    def load_detail(self, asin: str) -> dict[str, Any] | None:
        tenant_id = self._require_tenant()
        asin = asin.upper()
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM amazon_us.item_state WHERE tenant_id=%s AND marketplace='US' AND asin=%s "
                "ORDER BY CASE subject_type WHEN 'own' THEN 0 WHEN 'competitor' THEN 1 ELSE 2 END LIMIT 1",
                (tenant_id, asin),
            )
            state = cursor.fetchone()
            if state is None:
                return None
            task = dict(state)
            subject_type = task["subject_type"]
            identity = (tenant_id, asin, subject_type)
            cursor.execute(
                "SELECT * FROM amazon_us.product_latest WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s",
                identity,
            )
            product_row = cursor.fetchone()
            product = dict(product_row) if product_row is not None else None
            cursor.execute(
                "SELECT placement,entry_type,thumbnail_url,display_url,asset_url,poster_url,ordinal,is_primary,"
                "width,height,alt_text,variant_asin,load_status,failure_reason FROM amazon_us.media_asset "
                "WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s ORDER BY ordinal NULLS LAST LIMIT 300",
                identity,
            )
            media = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT module_type,position,order_index,text,image_url,link_url,status FROM amazon_us.content_module "
                "WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s "
                "ORDER BY position,order_index NULLS LAST LIMIT 300",
                identity,
            )
            content = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT * FROM amazon_us.review_summary WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s",
                identity,
            )
            summary_row = cursor.fetchone()
            review_summary = dict(summary_row) if summary_row is not None else None
            cursor.execute(
                "SELECT review_id,rating,title,body,review_url,review_date,locale,verified,body_truncated,review_images,page "
                "FROM amazon_us.review_record WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s "
                "ORDER BY page NULLS LAST,review_id LIMIT 100",
                identity,
            )
            reviews = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT run_id,url,http_status,transfer_bytes,retrieved_at,source_type,content_hash,raw_html_path,"
                "block_reason,parser_version,error_code,context_json FROM amazon_us.collection_evidence "
                "WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s ORDER BY id DESC LIMIT 30",
                identity,
            )
            evidence = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT snapshot_id,collected_at,price,availability,rating,review_count,status FROM amazon_us.product_snapshot "
                "WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s "
                "ORDER BY collected_at DESC,snapshot_id DESC LIMIT 30",
                identity,
            )
            history = [dict(row) for row in cursor.fetchall()]
        for row in evidence:
            row["outcome"] = classify_evidence_outcome(row)
        if product is not None:
            product["price_status"] = project_price_status(product)
        return {
            "schema_version": "amazon-us-console-v1",
            "tenant_id": tenant_id,
            "asin": asin,
            "task": task,
            "product": product,
            "media": media,
            "content_modules": content,
            "review_summary": review_summary,
            "reviews": reviews,
            "top_reviews": (product or {}).get("top_reviews") or [],
            "evidence": evidence,
            "history": history,
        }


class ConsoleHandler(BaseHTTPRequestHandler):
    server: "ConsoleServer"

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_default).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _scoped_repository(self, parsed):
        repository = self.server.repository
        if not hasattr(repository, "for_tenant"):
            return repository
        query = parse_qs(parsed.query)
        tenant_id = str(query.get("tenant", [""])[0]).strip()
        if not tenant_id:
            identity = repository.load_identity()
            tenant_id = str(identity.get("default_tenant_id") or "").strip()
        scoped = repository.for_tenant(tenant_id)
        if scoped is None:
            raise ValueError("invalid tenant")
        return scoped

    def _authorized(self) -> bool:
        expected = self.server.api_key
        if not expected:
            return True
        supplied = self.headers.get("X-Collection-API-Key", "")
        if hmac.compare_digest(supplied, expected):
            return True
        self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def _send_static(self, path: str) -> None:
        name = STATIC_FILES.get(path)
        if name is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})
            return
        file_path = STATIC_ROOT / name
        try:
            body = file_path.read_bytes()
        except OSError:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "console_asset_unavailable"})
            return
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        if file_path.suffix == ".js":
            content_type = "text/javascript"
        self._send_bytes(HTTPStatus.OK, body, f"{content_type}; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        if path in STATIC_FILES:
            self._send_static(path)
            return
        if path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True, "schema_version": "amazon-us-console-v2"})
            return
        if path == "/readyz":
            try:
                identity = self.server.repository.load_identity()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "schema_version": "amazon-us-console-v2",
                        "runtime_fingerprint": RUNTIME_FINGERPRINT,
                        **identity,
                    },
                )
            except Exception:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "database_unavailable"})
            return
        if not path.startswith("/api/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})
            return
        if not self._authorized():
            return
        try:
            if path == "/api/tenants":
                if hasattr(self.server.repository, "list_tenants"):
                    items = self.server.repository.list_tenants()
                else:
                    identity = self.server.repository.load_identity()
                    items = [{"tenant_id": identity.get("tenant_id"), "requested": identity.get("task_count", 0)}]
                self._send_json(
                    HTTPStatus.OK,
                    {"schema_version": "amazon-us-console-v2", "items": items},
                )
                return
            repository = self._scoped_repository(parsed)
            if path == "/api/overview":
                self._send_json(HTTPStatus.OK, repository.load_overview(self.server.raw_html_dir))
                return
            if path == "/api/runs":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["20"])[0])
                self._send_json(
                    HTTPStatus.OK,
                    {"schema_version": "amazon-us-console-v2", "items": repository.list_runs(limit)},
                )
                return
            if path == "/api/operations":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["100"])[0])
                self._send_json(
                    HTTPStatus.OK,
                    {"schema_version": "amazon-us-console-v2", "items": repository.list_operations(limit)},
                )
                return
            run_match = RUN_PATH.fullmatch(path)
            if run_match:
                run_id = run_match.group(1)
                payload = repository.load_run(run_id)
                if payload is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "run_not_found", "run_id": run_id})
                    return
                self._send_json(HTTPStatus.OK, payload)
                return
            if path == "/api/items":
                query = parse_qs(parsed.query)
                status = query.get("status", [""])[0].strip() or None
                stage = query.get("stage", [""])[0].strip() or None
                term = query.get("q", [""])[0].strip()[:100] or None
                limit = int(query.get("limit", ["100"])[0])
                offset = int(query.get("offset", ["0"])[0])
                if status and not re.fullmatch(r"[a-z_]+", status):
                    raise ValueError("invalid status")
                if stage and stage not in {"product", "reviews", "complete"}:
                    raise ValueError("invalid stage")
                payload = repository.list_items(
                    status=status, stage=stage, query=term, limit=limit, offset=offset
                )
                self._send_json(HTTPStatus.OK, payload)
                return
            match = ITEM_PATH.fullmatch(path)
            if match:
                asin = match.group(1).upper()
                if not ASIN_RE.fullmatch(asin):
                    raise ValueError("invalid ASIN")
                payload = repository.load_detail(asin)
                if payload is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "asin_not_found", "asin": asin})
                    return
                self._send_json(HTTPStatus.OK, payload)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "detail": str(exc)})
        except Exception:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable"})

    def do_POST(self) -> None:  # noqa: N802
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only_console"})

    def do_PUT(self) -> None:  # noqa: N802
        self.do_POST()

    def do_DELETE(self) -> None:  # noqa: N802
        self.do_POST()

    def log_message(self, format: str, *args: Any) -> None:
        if self.server.access_log:
            super().log_message(format, *args)


class ConsoleServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        repository: Any,
        *,
        raw_html_dir: Path | None = None,
        api_key: str = "",
        access_log: bool = False,
    ):
        if address[0] not in LOOPBACK_HOSTS:
            raise ValueError("console only allows loopback host")
        super().__init__(address, ConsoleHandler)
        self.repository = repository
        self.raw_html_dir = raw_html_dir
        self.api_key = api_key
        self.access_log = access_log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--tenant-id", help="optional initial tenant; the UI can switch among PostgreSQL tenants")
    parser.add_argument("--raw-html-dir", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--api-key-env", default="AMAZON_COLLECTION_API_KEY")
    parser.add_argument("--require-api-key", action="store_true")
    parser.add_argument("--access-log", action="store_true")
    args = parser.parse_args(argv)
    try:
        dsn = os.environ.get(args.dsn_env, "").strip()
        if not dsn:
            raise ValueError(f"PostgreSQL DSN environment variable is required: {args.dsn_env}")
        api_key = os.environ.get(args.api_key_env, "")
        if args.require_api_key and not api_key:
            raise ValueError(f"required API key environment variable is missing: {args.api_key_env}")
        repository = PostgresConsoleRepository(dsn, args.tenant_id)
        server = ConsoleServer(
            (args.host, args.port),
            repository,
            raw_html_dir=args.raw_html_dir,
            api_key=api_key,
            access_log=args.access_log,
        )
        print(f"Amazon Collection Console: http://{args.host}:{server.server_port}")
        print(f"Tenants: PostgreSQL-visible | read-only | refresh: 5s")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
