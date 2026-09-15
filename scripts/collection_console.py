#!/usr/bin/env python3
"""Loopback-only, read-only PostgreSQL operations console across crawler tenants."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import hmac
import json
import mimetypes
import os
import re
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
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
REVIEW_ITEM_PATH = re.compile(r"^/api/reviews/([A-Za-z0-9]{10})$")
MAX_REVIEW_OFFSET = 100_000
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
    if requested_actions is not None and recorded_actions is not None and unrequested_actions is not None:
        unrequested_actions = max(unrequested_actions, max(0,requested_actions-recorded_actions))
    counts = {
        outcome: sum(item.get("outcome") == outcome for item in items)
        for outcome in ("completed", "variant_redirect", "failed", "blocked")
    }
    access_control_rate = (
        round(counts["blocked"] / recorded_actions, 4)
        if recorded_actions not in {None, 0} else None
    )
    quality_gate_ok = bool(
        requested_actions is not None
        and recorded_actions is not None
        and unrequested_actions is not None
        and requested_actions == recorded_actions
        and counts["completed"] + counts["variant_redirect"] == recorded_actions
        and counts["failed"] == 0
        and counts["blocked"] == 0
        and unrequested_actions == 0
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
        "quality_gate_ok": quality_gate_ok,
        "quality_gate_reason": "completed_or_variant" if quality_gate_ok else "incomplete_or_failed_business_outcomes",
    }


def latest_cohort_evidence(rows):
    latest={}; attempts={}
    for value in rows:
        row=dict(value); asin=row['asin']; latest[asin]=row
        attempts.setdefault(asin,[]).append({key:row.get(key) for key in (
            'id','run_id','http_status','transfer_bytes','retrieved_at','source_type','content_hash','raw_html_path','block_reason','error_code')})
    for asin,row in latest.items():
        row['attempt_evidence']=attempts[asin]
        row['attempt_count']=len(attempts[asin])
    return list(latest.values())


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
        and identity.get("canonical_valid_amazon") is True
    )


class _CanonicalLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "link" or self.href is not None:
            return
        values = {str(name).lower(): value for name, value in attrs}
        if "canonical" in str(values.get("rel") or "").lower().split():
            self.href = str(values.get("href") or "") or None


def reconcile_legacy_variant_canonical(
    row: dict[str, Any], raw_html_dir: Path | None,
) -> dict[str, Any]:
    """Derive missing canonical validity from hash-verified raw evidence in memory only."""
    context = _context_value(row)
    identity = context.get("identity") or {}
    if not isinstance(identity, dict) or "canonical_valid_amazon" in identity:
        return row
    # Raw reconciliation can only supply the missing canonical proof. It cannot
    # invent sibling identity for ordinary successful/blocked/transport rows.
    proof_candidate = {**row, "context_json": {**context, "identity": {
        **identity, "canonical_valid_amazon": True,
    }}}
    if row.get("block_reason") or not _explicit_sibling_identity(proof_candidate):
        return row
    updated = dict(row)
    updated_context = dict(context)
    updated_identity = dict(identity)
    updated_identity["canonical_valid_amazon"] = False
    updated_context["identity"] = updated_identity
    updated["context_json"] = updated_context
    if raw_html_dir is None or not row.get("raw_html_path") or not row.get("content_hash"):
        return updated
    root = raw_html_dir.resolve()
    candidate = (root / str(row["raw_html_path"])).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return updated
    try:
        if candidate.name.endswith(".gz"):
            with gzip.open(candidate, "rb") as handle:
                raw_bytes = handle.read()
        else:
            raw_bytes = candidate.read_bytes()
    except OSError:
        return updated
    if hashlib.sha256(raw_bytes).hexdigest() != str(row.get("content_hash") or ""):
        return updated
    parser = _CanonicalLinkParser()
    parser.feed(raw_bytes.decode("utf-8", errors="replace"))
    parts = urlsplit(parser.href or "")
    match = re.search(r"/(?:dp|clp)/([A-Za-z0-9]{10})(?:/|$|[?#])", parts.path)
    observed = str(updated_identity.get("observed_asin") or "").upper()
    updated_identity["canonical_valid_amazon"] = bool(
        parts.scheme == "https"
        and (parts.hostname or "").lower().removeprefix("www.") == "amazon.com"
        and match
        and match.group(1).upper() == observed
    )
    return updated


def raw_root_for_tenant(
    raw_html_dir: Path | None, raw_tenant_id: str | None, selected_tenant_id: str | None,
) -> Path | None:
    return raw_html_dir if raw_html_dir is not None and raw_tenant_id == selected_tenant_id else None


def lexical_raw_root(raw_html_dir: Path) -> str:
    """Normalize the supplied path without resolving junctions or symlinks."""
    value = os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(raw_html_dir))))
    return value.rstrip("\\/") or value


def raw_root_fingerprint(tenant_id: str, raw_html_dir: Path) -> str:
    identity = f"{str(tenant_id)}|{lexical_raw_root(raw_html_dir)}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


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
                    AND context_json->'identity'->'canonical_valid_amazon'='true'::jsonb
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



    def load_recovery_status(self) -> dict[str, Any]:
        from recovery_scheduler import read_projection
        return read_projection(self._connect, self._require_tenant())

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
        raw_html_dir: Path | None = None,
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
                       e.context_json,e.raw_html_path,e.content_hash,e.retrieved_at
                FROM amazon_us.item_state s
                {join}
                LEFT JOIN LATERAL (
                  SELECT source_type,http_status,error_code,block_reason,context_json,raw_html_path,content_hash,retrieved_at
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
            evidence = reconcile_legacy_variant_canonical({
                **item,
                "error_code": item.get("evidence_error"),
                "block_reason": item.get("evidence_block") or item.get("block_reason"),
            }, raw_html_dir)
            item["context_json"] = evidence.get("context_json")
            item["outcome"] = classify_evidence_outcome(evidence)
        return {"tenant_id": tenant_id, "total": total, "limit": limit, "offset": offset, "items": items}

    def list_runs(self, limit: int = 20, raw_html_dir: Path | None = None) -> list[dict[str, Any]]:
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
                       e.context_json,e.raw_html_path,e.content_hash
                FROM amazon_us.collection_evidence e JOIN recent r ON r.run_id=e.run_id
                WHERE e.tenant_id=%s ORDER BY r.ended_at DESC,e.id
                """,
                (tenant_id, limit, tenant_id),
            )
            evidence_rows = [
                reconcile_legacy_variant_canonical(dict(row), raw_html_dir)
                for row in cursor.fetchall()
            ]
            cursor.execute("SELECT to_regclass('amazon_us.collection_run') AS relation")
            has_ledger = cursor.fetchone()["relation"] is not None
            ledger_rows: dict[str, dict[str, Any]] = {}
            if has_ledger:
                cursor.execute(
                    "SELECT * FROM amazon_us.collection_run WHERE tenant_id=%s ORDER BY started_at DESC LIMIT %s",
                    (tenant_id, limit),
                )
                ledger_rows = {str(row["run_id"]): dict(row) for row in cursor.fetchall()}
            batch_ids=sorted({str(row['run_id']) for row in evidence_rows if _context_value(row).get('recovery_batch_id')})
            batches={}
            if batch_ids:
                cursor.execute('SELECT * FROM amazon_us.recovery_batch WHERE tenant_id=%s AND run_id=ANY(%s)',(tenant_id,batch_ids))
                batches={str(row['run_id']):dict(row) for row in cursor.fetchall()}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in evidence_rows:
            grouped.setdefault(str(row["run_id"]), []).append(row)
        for run_id in ledger_rows:
            grouped.setdefault(run_id, [])
        results: list[dict[str, Any]] = []
        for run_id, rows in grouped.items():
            ledger = ledger_rows.get(run_id) or {}
            batch=batches.get(run_id) or (ledger.get('receipt_json') or {}).get('recovery_batch')
            all_rows=rows
            if any(_context_value(row).get('recovery_batch_id')==run_id for row in rows):
                rows=latest_cohort_evidence(rows)
            outcomes = {"completed": 0, "variant_redirect": 0, "failed": 0, "blocked": 0}
            for row in rows:
                outcomes[classify_evidence_outcome(row)] += 1
            observed_start = min((row["retrieved_at"] for row in all_rows), default=None)
            observed_end = max((row["retrieved_at"] for row in all_rows), default=None)
            started_at = ledger.get("started_at") or observed_start
            ended_at = ledger.get("finished_at") or observed_end
            requested = len(batch['asins']) if batch else int(ledger.get("requested_actions") or len(rows))
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
                "attempt_actions":len(all_rows),
                "recovery_batch":batch,
                "evidence_actions": recorded,
                "unique_asins": len({row["asin"] for row in rows}),
                "product_succeeded": outcomes["completed"],
                "variant_redirect": outcomes["variant_redirect"],
                "failed": outcomes["failed"],
                "blocked": outcomes["blocked"],
                "pending": max(0, requested - recorded),
                "running": max(0, requested - recorded) if status == "running" else 0,
                "known_transfer_bytes": sum(int(row.get("transfer_bytes") or 0) for row in all_rows),
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

    def load_run(self, run_id: str, raw_html_dir: Path | None = None) -> dict[str, Any] | None:
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
            evidence_rows = [
                reconcile_legacy_variant_canonical(dict(row), raw_html_dir)
                for row in cursor.fetchall()
            ]
            cursor.execute("SELECT to_regclass('amazon_us.collection_run') AS relation")
            ledger = None
            if cursor.fetchone()["relation"] is not None:
                cursor.execute(
                    "SELECT * FROM amazon_us.collection_run WHERE tenant_id=%s AND run_id=%s",
                    (tenant_id, run_id),
                )
                ledger_row = cursor.fetchone()
                ledger = dict(ledger_row) if ledger_row is not None else None
            batch=((ledger or {}).get('receipt_json') or {}).get('recovery_batch')
            if not batch and any(_context_value(row).get('recovery_batch_id')==run_id for row in evidence_rows):
                cursor.execute('SELECT * FROM amazon_us.recovery_batch WHERE tenant_id=%s AND run_id=%s',(tenant_id,run_id))
                value=cursor.fetchone()
                batch=dict(value) if value else None
            all_evidence_rows=list(evidence_rows)
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
                    "attempt_actions":0,
                    "recovery_batch":batch,
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
            if batch:
                if any(row['asin'] not in batch['asins'] for row in evidence_rows):
                    raise RuntimeError('cohort_evidence_scope_mismatch')
                evidence_rows=latest_cohort_evidence(evidence_rows)
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
                if batch or row["asin"] in evidence_asins:
                    continue
                outcome = "blocked" if row.get("block_reason") else "failed" if row.get("last_error") else row.get("current_status")
                items.append({**row, "outcome": outcome, "attribution": "time_window_inference"})
        items.sort(key=lambda item: (item.get("retrieved_at") or item.get("updated_at"), item["asin"]))
        traffic_summary = summarize_traffic(all_evidence_rows)
        context_quality_counts = summarize_context_quality(evidence_rows)
        proxy_session_pool = latest_proxy_session_pool(evidence_rows)
        authorization = (ledger or {}).get("capacity_authorization_json") or latest_capacity_authorization(evidence_rows)
        requested_actions = len(batch['asins']) if batch else int((ledger or {}).get("requested_actions") or len(evidence_rows))
        recorded_actions = len(evidence_rows)
        duration_projection = project_run_durations(ledger, started_at, ended_at)
        effective_started_at = duration_projection.pop("effective_started_at")
        effective_finished_at = duration_projection.pop("effective_finished_at")
        return {
            "schema_version": "amazon-us-console-v2",
            "tenant_id": tenant_id,
            "run_id": run_id,
            "started_at": effective_started_at,
            "command": (ledger or {}).get("command") or "legacy",
            "ended_at": effective_finished_at,
            "requested_actions": requested_actions,
            "recorded_actions": recorded_actions,
            "attempt_actions":len(all_evidence_rows),
            "recovery_batch":batch,
            "capacity_authorizations":list({(_context_value(row).get('capacity_authorization') or {}).get('reservation_id'):
                _context_value(row).get('capacity_authorization') for row in all_evidence_rows
                if _context_value(row).get('capacity_authorization')}.values()),
            "inferred_actions": sum(1 for item in items if item["attribution"] == "time_window_inference"),
            "known_transfer_bytes": sum(int(item.get("transfer_bytes") or 0) for item in all_evidence_rows),
            "traffic": traffic_summary,
            "context_quality_counts": context_quality_counts,
            "proxy_session_pool": proxy_session_pool,
            "proxy_connectivity": project_proxy_connectivity(authorization),
            "amazon_business": project_amazon_business(
                items,
                requested_actions=requested_actions,
                recorded_actions=recorded_actions,
                unrequested_actions=max(0,requested_actions-recorded_actions) if batch else (proxy_session_pool or {}).get("unrequested_count"),
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

    def load_detail(self, asin: str, raw_html_dir: Path | None = None) -> dict[str, Any] | None:
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
            evidence = [
                reconcile_legacy_variant_canonical(dict(row), raw_html_dir)
                for row in cursor.fetchall()
            ]
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


class PostgresReviewConsoleRepository:
    """Bounded read-only views over the independent amazon_reviews schema."""

    COVERAGE_STATUSES = {"complete", "no_reviews_verified", "partial", "source_limited", "blocked"}

    def __init__(self, dsn: str, tenant_id: str | None = None, provider: str = "woot_reviews"):
        if not str(dsn or "").strip():
            raise ValueError("PostgreSQL DSN must not be empty")
        self.dsn = str(dsn)
        self.tenant_id = str(tenant_id or "").strip()
        self.provider = str(provider or "").strip()
        if self.tenant_id and not TENANT_RE.fullmatch(self.tenant_id):
            raise ValueError("invalid review tenant_id")
        if not TENANT_RE.fullmatch(self.provider):
            raise ValueError("invalid review provider")

    def for_tenant(self, tenant_id: str):
        tenant_id = str(tenant_id or "").strip()
        if not TENANT_RE.fullmatch(tenant_id):
            return None
        return type(self)(self.dsn, tenant_id, self.provider)

    def _require_tenant(self) -> str:
        if not self.tenant_id:
            raise ValueError("review tenant selection is required")
        return self.tenant_id

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL review console requires optional dependency psycopg") from exc
        return psycopg.connect(
            self.dsn,
            row_factory=dict_row,
            options="-c default_transaction_read_only=on -c statement_timeout=5000",
        )

    @staticmethod
    def _page(limit: int, offset: int) -> tuple[int, int]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if offset < 0 or offset > MAX_REVIEW_OFFSET:
            raise ValueError(f"offset must be between 0 and {MAX_REVIEW_OFFSET}")
        return limit, offset

    def _unavailable(self, error_code: str = "review_database_unavailable") -> dict[str, Any]:
        return {
            "schema_version": "amazon-us-console-woot-reviews-v1",
            "availability": "unavailable",
            "error_code": error_code,
            "source_type": "postgres",
            "provider": self.provider,
            "tenant_id": self.tenant_id or None,
            "observed_at": None,
            "manifest_verified": None,
            "summary": {},
            "derived": {},
        }

    def _require_schema(self, cursor) -> bool:
        cursor.execute(
            "SELECT to_regclass('amazon_reviews.review_job_state') AS job_relation,"
            "to_regclass('amazon_reviews.review_record') AS record_relation"
        )
        row = cursor.fetchone() or {}
        return row.get("job_relation") is not None and row.get("record_relation") is not None

    def load_overview(self) -> dict[str, Any]:
        try:
            tenant_id = self._require_tenant()
            with self._connect() as conn, conn.cursor() as cursor:
                if not self._require_schema(cursor):
                    return self._unavailable("review_schema_unavailable")
                cursor.execute(
                    """
                    SELECT COUNT(*) AS source_unique_asins,
                           COUNT(*) FILTER (WHERE status IN ('succeeded','source_limited')) AS successful_asins,
                           COUNT(*) FILTER (WHERE status IN ('pending','running','retryable')) AS pending_asins,
                           COUNT(*) FILTER (WHERE status IN ('blocked','failed')) AS failed_asins,
                           COUNT(*) FILTER (WHERE fetched_review_count=0) AS zero_review_asins,
                           MAX(updated_at) AS job_observed_at
                    FROM amazon_reviews.review_job_state
                    WHERE tenant_id=%s AND marketplace='US' AND provider=%s
                    """,
                    (tenant_id, self.provider),
                )
                jobs = dict(cursor.fetchone() or {})
                cursor.execute(
                    """
                    SELECT coverage_status,COUNT(*) AS count
                    FROM amazon_reviews.review_job_state
                    WHERE tenant_id=%s AND marketplace='US' AND provider=%s
                      AND coverage_status IS NOT NULL
                    GROUP BY coverage_status ORDER BY coverage_status
                    """,
                    (tenant_id, self.provider),
                )
                coverage = {str(row["coverage_status"]): int(row["count"]) for row in cursor.fetchall()}
                cursor.execute(
                    """
                    SELECT COUNT(*) AS unique_reviews,
                           COUNT(*) FILTER (WHERE jsonb_array_length(review_images)>0) AS with_images,
                           COUNT(*) FILTER (WHERE jsonb_array_length(review_videos)>0) AS with_videos,
                           COUNT(*) FILTER (WHERE verified IS TRUE) AS verified_reviews,
                           COUNT(*) FILTER (WHERE vine IS TRUE) AS vine_reviews,
                           MAX(last_seen_at) AS record_observed_at
                    FROM amazon_reviews.review_record
                    WHERE tenant_id=%s AND marketplace='US' AND provider=%s
                    """,
                    (tenant_id, self.provider),
                )
                records = dict(cursor.fetchone() or {})
            observed_values = [
                value for value in (jobs.get("job_observed_at"), records.get("record_observed_at"))
                if value is not None
            ]
            observed_at = max(observed_values) if observed_values else None
            claim_complete = int(coverage.get("complete", 0)) + int(coverage.get("no_reviews_verified", 0))
            return {
                "schema_version": "amazon-us-console-woot-reviews-v1",
                "availability": "available",
                "error_code": None,
                "source_type": "postgres",
                "provider": self.provider,
                "tenant_id": tenant_id,
                "observed_at": observed_at,
                "manifest_verified": None,
                "summary": {
                    "source_unique_asins": int(jobs.get("source_unique_asins") or 0),
                    "successful_asins": int(jobs.get("successful_asins") or 0),
                    "pending_asins": int(jobs.get("pending_asins") or 0),
                    "failed_asins": int(jobs.get("failed_asins") or 0),
                    "unique_reviews": int(records.get("unique_reviews") or 0),
                    "coverage_status_counts": coverage,
                    "claim_100_percent_asins": claim_complete,
                },
                "derived": {
                    "zero_review_asins": int(jobs.get("zero_review_asins") or 0),
                    "with_images": int(records.get("with_images") or 0),
                    "with_videos": int(records.get("with_videos") or 0),
                    "verified_reviews": int(records.get("verified_reviews") or 0),
                    "vine_reviews": int(records.get("vine_reviews") or 0),
                },
            }
        except (OSError, RuntimeError, ValueError):
            return self._unavailable()
        except Exception:
            return self._unavailable()

    def list_asins(
        self,
        *,
        query: str | None = None,
        coverage: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit, offset = self._page(limit, offset)
        if coverage and coverage not in self.COVERAGE_STATUSES:
            raise ValueError("invalid coverage status")
        try:
            tenant_id = self._require_tenant()
            conditions = ["tenant_id=%s", "marketplace='US'", "provider=%s"]
            params: list[Any] = [tenant_id, self.provider]
            term = str(query or "").strip().upper()
            if term:
                conditions.append("asin LIKE %s")
                params.append(f"%{term}%")
            if coverage:
                conditions.append("coverage_status=%s")
                params.append(coverage)
            where = " AND ".join(conditions)
            with self._connect() as conn, conn.cursor() as cursor:
                if not self._require_schema(cursor):
                    unavailable = self._unavailable("review_schema_unavailable")
                    return {**unavailable, "total": 0, "limit": limit, "offset": offset, "items": []}
                cursor.execute(
                    f"SELECT COUNT(*) AS count FROM amazon_reviews.review_job_state WHERE {where}",
                    tuple(params),
                )
                total = int((cursor.fetchone() or {}).get("count") or 0)
                cursor.execute(
                    f"""
                    SELECT asin,status,coverage_status,
                           fetched_review_count AS fetched_unique_count,
                           last_run_id,last_error AS error_code,updated_at
                    FROM amazon_reviews.review_job_state
                    WHERE {where}
                    ORDER BY asin LIMIT %s OFFSET %s
                    """,
                    tuple(params + [limit, offset]),
                )
                items = [dict(row) for row in cursor.fetchall()]
            return {
                "schema_version": "amazon-us-console-woot-reviews-v1",
                "availability": "available",
                "source_type": "postgres",
                "provider": self.provider,
                "tenant_id": tenant_id,
                "total": total,
                "limit": limit,
                "offset": offset,
                "items": items,
            }
        except (OSError, RuntimeError, ValueError):
            unavailable = self._unavailable()
            return {**unavailable, "total": 0, "limit": limit, "offset": offset, "items": []}
        except Exception:
            unavailable = self._unavailable()
            return {**unavailable, "total": 0, "limit": limit, "offset": offset, "items": []}

    def load_asin(
        self,
        asin: str,
        *,
        limit: int = 20,
        offset: int = 0,
        rating: int | None = None,
        verified: bool | None = None,
    ) -> dict[str, Any]:
        limit, offset = self._page(limit, offset)
        asin = str(asin or "").strip().upper()
        if not ASIN_RE.fullmatch(asin):
            raise ValueError("invalid ASIN")
        if rating is not None and rating not in {1, 2, 3, 4, 5}:
            raise ValueError("rating must be between 1 and 5")
        try:
            tenant_id = self._require_tenant()
            conditions = ["tenant_id=%s", "marketplace='US'", "asin=%s", "provider=%s"]
            params: list[Any] = [tenant_id, asin, self.provider]
            if rating is not None:
                conditions.append("rating=%s")
                params.append(rating)
            if verified is not None:
                conditions.append("verified=%s")
                params.append(verified)
            where = " AND ".join(conditions)
            with self._connect() as conn, conn.cursor() as cursor:
                if not self._require_schema(cursor):
                    unavailable = self._unavailable("review_schema_unavailable")
                    return {**unavailable, "asin": asin, "status": None, "total": 0, "limit": limit, "offset": offset, "next_offset": None, "items": []}
                cursor.execute(
                    """
                    SELECT asin,status,coverage_status,
                           fetched_review_count AS fetched_unique_count,
                           last_run_id,last_error AS error_code,updated_at
                    FROM amazon_reviews.review_job_state
                    WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND provider=%s
                    """,
                    (tenant_id, asin, self.provider),
                )
                status = cursor.fetchone()
                cursor.execute(
                    f"SELECT COUNT(*) AS count FROM amazon_reviews.review_record WHERE {where}",
                    tuple(params),
                )
                total = int((cursor.fetchone() or {}).get("count") or 0)
                cursor.execute(
                    f"""
                    SELECT review_id,provider_review_id,id_kind,author,rating,title,body,
                           review_url,review_date_text AS review_date,locale,verified,vine,
                           helpful_votes,review_images,review_videos,first_seen_at,last_seen_at
                    FROM amazon_reviews.review_record
                    WHERE {where}
                    ORDER BY last_seen_at DESC,review_id
                    LIMIT %s OFFSET %s
                    """,
                    tuple(params + [limit, offset]),
                )
                items = [dict(row) for row in cursor.fetchall()]
            next_offset = offset + len(items) if offset + len(items) < total else None
            return {
                "schema_version": "amazon-us-console-woot-reviews-v1",
                "availability": "available",
                "source_type": "postgres",
                "provider": self.provider,
                "tenant_id": tenant_id,
                "asin": asin,
                "status": dict(status) if status else None,
                "total": total,
                "limit": limit,
                "offset": offset,
                "next_offset": next_offset,
                "items": items,
            }
        except (OSError, RuntimeError, ValueError):
            unavailable = self._unavailable()
            return {**unavailable, "asin": asin, "status": None, "total": 0, "limit": limit, "offset": offset, "next_offset": None, "items": []}
        except Exception:
            unavailable = self._unavailable()
            return {**unavailable, "asin": asin, "status": None, "total": 0, "limit": limit, "offset": offset, "next_offset": None, "items": []}


class WootReviewConsoleRepository:
    """Verified, read-only views over one Woot aggregate directory."""

    REQUIRED_FILES = {
        "asin_status.jsonl", "collection_summary.json", "pending_asins.json", "reviews.jsonl"
    }
    COVERAGE_STATUSES = {"complete", "no_reviews_verified", "partial", "source_limited", "blocked"}

    def __init__(self, root: Path | None, tenant_id: str | None = None):
        self.root = Path(root).resolve() if root is not None else None
        self.tenant_id = str(tenant_id or "").strip()
        if self.tenant_id and not TENANT_RE.fullmatch(self.tenant_id):
            raise ValueError("invalid review tenant_id")
        self.availability = "unavailable"
        self.error_code = "not_configured" if root is None else "review_data_unavailable"
        self.summary: dict[str, Any] = {}
        self.statuses: list[dict[str, Any]] = []
        self.status_by_asin: dict[str, dict[str, Any]] = {}
        self.review_offsets: dict[str, list[int]] = {}
        self.derived = {
            "zero_review_asins": 0,
            "with_images": 0,
            "with_videos": 0,
            "verified_reviews": 0,
            "vine_reviews": 0,
        }
        self.observed_at: str | None = None
        if root is None:
            return
        try:
            self._load()
        except (OSError, ValueError, json.JSONDecodeError):
            self.availability = "unavailable"
            self.error_code = "review_data_invalid"

    def for_tenant(self, tenant_id: str):
        tenant_id = str(tenant_id or "").strip()
        if not TENANT_RE.fullmatch(tenant_id):
            return None
        if not self.tenant_id or tenant_id != self.tenant_id:
            return WootReviewConsoleRepository(None, tenant_id)
        return self

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _load(self) -> None:
        if self.root is None or not self.root.is_dir():
            raise ValueError("aggregate directory is missing")
        manifest_path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = manifest.get("files")
        if not isinstance(entries, list):
            raise ValueError("manifest files must be a list")
        indexed: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("manifest entry must be an object")
            name = str(entry.get("path") or "")
            if name not in self.REQUIRED_FILES or name in indexed:
                raise ValueError("manifest contains an unexpected or duplicate path")
            indexed[name] = entry
        if set(indexed) != self.REQUIRED_FILES:
            raise ValueError("manifest is missing required review files")
        for name, entry in indexed.items():
            path = (self.root / name).resolve()
            if path.parent != self.root or not path.is_file():
                raise ValueError("review file is outside aggregate directory or missing")
            if path.stat().st_size != int(entry.get("bytes") or -1):
                raise ValueError("review file size mismatch")
            if self._sha256(path) != str(entry.get("sha256") or "").lower():
                raise ValueError("review file hash mismatch")

        summary = json.loads((self.root / "collection_summary.json").read_text(encoding="utf-8"))
        if not isinstance(summary, dict):
            raise ValueError("review summary must be an object")
        statuses: list[dict[str, Any]] = []
        status_by_asin: dict[str, dict[str, Any]] = {}
        with (self.root / "asin_status.jsonl").open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("review status row must be an object")
                asin = str(item.get("asin") or "").strip().upper()
                coverage = str(item.get("coverage_status") or "")
                if not ASIN_RE.fullmatch(asin) or asin in status_by_asin:
                    raise ValueError("review status ASIN is invalid or duplicated")
                if coverage not in self.COVERAGE_STATUSES:
                    raise ValueError("review coverage status is invalid")
                normalized = dict(item)
                normalized["asin"] = asin
                normalized["coverage_status"] = coverage
                normalized["fetched_unique_count"] = int(item.get("fetched_unique_count") or 0)
                if normalized["fetched_unique_count"] < 0:
                    raise ValueError("review count must be non-negative")
                statuses.append(normalized)
                status_by_asin[asin] = normalized

        review_offsets: dict[str, list[int]] = {}
        derived = {
            "zero_review_asins": sum(item["fetched_unique_count"] == 0 for item in statuses),
            "with_images": 0,
            "with_videos": 0,
            "verified_reviews": 0,
            "vine_reviews": 0,
        }
        review_count = 0
        review_ids: dict[str, set[str]] = {}
        reviews_path = self.root / "reviews.jsonl"
        with reviews_path.open("rb") as handle:
            while True:
                offset = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("review row must be an object")
                asin = str(item.get("asin") or "").strip().upper()
                if asin not in status_by_asin:
                    raise ValueError("review ASIN is missing from status index")
                review_id = str(item.get("review_id") or "")
                ids = review_ids.setdefault(asin, set())
                if not review_id or review_id in ids:
                    raise ValueError("review row has blank or duplicate review_id")
                ids.add(review_id)
                review_offsets.setdefault(asin, []).append(offset)
                review_count += 1
                derived["with_images"] += bool(item.get("review_images"))
                derived["with_videos"] += bool(item.get("review_videos"))
                derived["verified_reviews"] += item.get("verified") is True
                derived["vine_reviews"] += item.get("vine") is True
        if len(statuses) != int(summary.get("source_unique_asins") or -1):
            raise ValueError("review status count does not match summary")
        if review_count != int(summary.get("unique_reviews") or -1):
            raise ValueError("review row count does not match summary")
        for asin, status in status_by_asin.items():
            if len(review_offsets.get(asin, [])) != status["fetched_unique_count"]:
                raise ValueError("per-ASIN review count does not match status")

        self.summary = summary
        self.statuses = statuses
        self.status_by_asin = status_by_asin
        self.review_offsets = review_offsets
        self.derived = derived
        self.observed_at = datetime.fromtimestamp(
            max((self.root / name).stat().st_mtime for name in self.REQUIRED_FILES), timezone.utc
        ).isoformat()
        self.availability = "available"
        self.error_code = None

    def load_overview(self) -> dict[str, Any]:
        return {
            "schema_version": "amazon-us-console-woot-reviews-v1",
            "availability": self.availability,
            "error_code": self.error_code,
            "provider": "woot_reviews",
            "source_type": "aggregate_files",
            "tenant_id": self.tenant_id or None,
            "observed_at": self.observed_at,
            "manifest_verified": self.availability == "available",
            "summary": dict(self.summary),
            "derived": dict(self.derived),
        }

    @staticmethod
    def _page(limit: int, offset: int) -> tuple[int, int]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if offset < 0 or offset > MAX_REVIEW_OFFSET:
            raise ValueError(f"offset must be between 0 and {MAX_REVIEW_OFFSET}")
        return limit, offset

    def list_asins(
        self,
        *,
        query: str | None = None,
        coverage: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit, offset = self._page(limit, offset)
        if self.availability != "available":
            return {**self.load_overview(), "total": 0, "limit": limit, "offset": offset, "items": []}
        if coverage and coverage not in self.COVERAGE_STATUSES:
            raise ValueError("invalid coverage status")
        term = str(query or "").strip().upper()
        values = [
            item for item in self.statuses
            if (not term or term in item["asin"])
            and (not coverage or item["coverage_status"] == coverage)
        ]
        return {
            "schema_version": "amazon-us-console-woot-reviews-v1",
            "availability": "available",
            "total": len(values),
            "limit": limit,
            "offset": offset,
            "items": values[offset:offset + limit],
        }

    def _read_asin(self, asin: str) -> list[dict[str, Any]]:
        if self.root is None:
            return []
        rows = []
        with (self.root / "reviews.jsonl").open("rb") as handle:
            for offset in self.review_offsets.get(asin, []):
                handle.seek(offset)
                item = json.loads(handle.readline())
                if str(item.get("asin") or "").upper() != asin:
                    raise ValueError("review index ASIN mismatch")
                rows.append(item)
        return rows

    def load_asin(
        self,
        asin: str,
        *,
        limit: int = 20,
        offset: int = 0,
        rating: int | None = None,
        verified: bool | None = None,
    ) -> dict[str, Any]:
        limit, offset = self._page(limit, offset)
        asin = str(asin or "").strip().upper()
        if not ASIN_RE.fullmatch(asin):
            raise ValueError("invalid ASIN")
        if rating is not None and rating not in {1, 2, 3, 4, 5}:
            raise ValueError("rating must be between 1 and 5")
        if self.availability != "available":
            return {**self.load_overview(), "asin": asin, "status": None, "total": 0, "limit": limit, "offset": offset, "next_offset": None, "items": []}
        rows = self._read_asin(asin)
        if rating is not None:
            rows = [item for item in rows if item.get("rating") == rating]
        if verified is not None:
            rows = [item for item in rows if item.get("verified") is verified]
        items = rows[offset:offset + limit]
        next_offset = offset + len(items) if offset + len(items) < len(rows) else None
        return {
            "schema_version": "amazon-us-console-woot-reviews-v1",
            "availability": "available",
            "provider": "woot_reviews",
            "asin": asin,
            "status": self.status_by_asin.get(asin),
            "total": len(rows),
            "limit": limit,
            "offset": offset,
            "next_offset": next_offset,
            "items": items,
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

    def _scoped_review_repository(self, parsed):
        repository = self.server.review_repository
        if not hasattr(repository, "for_tenant"):
            return repository
        query = parse_qs(parsed.query)
        tenant_id = str(query.get("tenant", [""])[0]).strip()
        if not tenant_id:
            identity = self.server.repository.load_identity()
            tenant_id = str(identity.get("default_tenant_id") or identity.get("tenant_id") or "").strip()
        scoped = repository.for_tenant(tenant_id)
        if scoped is None:
            raise ValueError("invalid review tenant")
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
                        "raw_tenant_id": self.server.raw_tenant_id,
                        "raw_root_fingerprint": self.server.raw_root_fingerprint,
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
            if path == "/api/reviews/overview":
                review_repository = self._scoped_review_repository(parsed)
                self._send_json(HTTPStatus.OK, review_repository.load_overview())
                return
            if path == "/api/reviews/items":
                review_repository = self._scoped_review_repository(parsed)
                query = parse_qs(parsed.query)
                term = query.get("q", [""])[0].strip()[:100] or None
                coverage = query.get("coverage", [""])[0].strip() or None
                limit = int(query.get("limit", ["50"])[0])
                offset = int(query.get("offset", ["0"])[0])
                self._send_json(
                    HTTPStatus.OK,
                    review_repository.list_asins(
                        query=term, coverage=coverage, limit=limit, offset=offset,
                    ),
                )
                return
            review_match = REVIEW_ITEM_PATH.fullmatch(path)
            if review_match:
                review_repository = self._scoped_review_repository(parsed)
                asin = review_match.group(1).upper()
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["20"])[0])
                offset = int(query.get("offset", ["0"])[0])
                rating_text = query.get("rating", [""])[0].strip()
                rating = int(rating_text) if rating_text else None
                verified_text = query.get("verified", [""])[0].strip().lower()
                if verified_text not in {"", "true", "false"}:
                    raise ValueError("verified must be true or false")
                verified = None if not verified_text else verified_text == "true"
                self._send_json(
                    HTTPStatus.OK,
                    review_repository.load_asin(
                        asin, limit=limit, offset=offset, rating=rating, verified=verified,
                    ),
                )
                return
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
            selected_raw_root = raw_root_for_tenant(
                self.server.raw_html_dir,
                self.server.raw_tenant_id,
                getattr(repository, "tenant_id", None),
            )
            if path == "/api/recovery":
                self._send_json(HTTPStatus.OK, repository.load_recovery_status())
                return
            if path == "/api/overview":
                self._send_json(HTTPStatus.OK, repository.load_overview(selected_raw_root))
                return
            if path == "/api/runs":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["20"])[0])
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "schema_version": "amazon-us-console-v2",
                        "items": (
                            repository.list_runs(limit, selected_raw_root)
                            if isinstance(repository, PostgresConsoleRepository)
                            else repository.list_runs(limit)
                        ),
                    },
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
                payload = (
                    repository.load_run(run_id, selected_raw_root)
                    if isinstance(repository, PostgresConsoleRepository)
                    else repository.load_run(run_id)
                )
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
                payload = (
                    repository.list_items(
                        status=status, stage=stage, query=term, limit=limit, offset=offset,
                        raw_html_dir=selected_raw_root,
                    )
                    if isinstance(repository, PostgresConsoleRepository)
                    else repository.list_items(
                        status=status, stage=stage, query=term, limit=limit, offset=offset
                    )
                )
                self._send_json(HTTPStatus.OK, payload)
                return
            match = ITEM_PATH.fullmatch(path)
            if match:
                asin = match.group(1).upper()
                if not ASIN_RE.fullmatch(asin):
                    raise ValueError("invalid ASIN")
                payload = (
                    repository.load_detail(asin, selected_raw_root)
                    if isinstance(repository, PostgresConsoleRepository)
                    else repository.load_detail(asin)
                )
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
        raw_tenant_id: str | None = None,
        review_repository: Any | None = None,
        api_key: str = "",
        access_log: bool = False,
    ):
        if address[0] not in LOOPBACK_HOSTS:
            raise ValueError("console only allows loopback host")
        super().__init__(address, ConsoleHandler)
        self.repository = repository
        self.review_repository = review_repository or WootReviewConsoleRepository(None)
        self.raw_html_dir = raw_html_dir
        self.raw_tenant_id = str(raw_tenant_id or "").strip() or None
        if self.raw_tenant_id and not TENANT_RE.fullmatch(self.raw_tenant_id):
            raise ValueError("invalid raw_tenant_id")
        self.raw_root_fingerprint = (
            raw_root_fingerprint(self.raw_tenant_id, raw_html_dir)
            if self.raw_tenant_id and raw_html_dir else None
        )
        self.api_key = api_key
        self.access_log = access_log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--tenant-id", help="optional initial tenant; the UI can switch among PostgreSQL tenants")
    parser.add_argument("--raw-html-dir", type=Path)
    parser.add_argument(
        "--woot-review-dir",
        type=Path,
        default=Path(os.environ["WOOT_REVIEW_AGGREGATE_DIR"])
        if os.environ.get("WOOT_REVIEW_AGGREGATE_DIR") else None,
        help="verified Woot aggregate directory for read-only console views",
    )
    parser.add_argument(
        "--review-source",
        choices=("auto", "postgres", "files", "disabled"),
        default="auto",
        help="review data source; auto prefers amazon_reviews PostgreSQL and falls back to files",
    )
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
        postgres_reviews = PostgresReviewConsoleRepository(dsn, args.tenant_id)
        file_reviews = WootReviewConsoleRepository(args.woot_review_dir, args.tenant_id)
        if args.review_source == "disabled":
            review_repository = WootReviewConsoleRepository(None, args.tenant_id)
        elif args.review_source == "files":
            if args.woot_review_dir is None:
                raise ValueError("--review-source files requires --woot-review-dir")
            if not args.tenant_id:
                raise ValueError("--review-source files requires --tenant-id to bind aggregate ownership")
            review_repository = file_reviews
        elif args.review_source == "postgres":
            review_repository = postgres_reviews
        else:
            review_repository = (
                postgres_reviews
                if postgres_reviews.load_overview().get("availability") == "available"
                else file_reviews
                if args.woot_review_dir is not None and args.tenant_id
                else postgres_reviews
            )
        server = ConsoleServer(
            (args.host, args.port),
            repository,
            raw_html_dir=args.raw_html_dir,
            raw_tenant_id=args.tenant_id,
            review_repository=review_repository,
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
