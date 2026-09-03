#!/usr/bin/env python3
"""PostgreSQL lifecycle ledger for audited crawler control operations."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Callable


OPERATION_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS amazon_us;
CREATE TABLE IF NOT EXISTS amazon_us.operation_run (
    operation_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    operation_type text NOT NULL CHECK (operation_type IN ('egress','canary','probe','run','reviews')),
    status text NOT NULL CHECK (status IN ('running','succeeded','failed','blocked','interrupted')),
    preflight_status text NOT NULL DEFAULT 'not_applicable'
        CHECK (preflight_status IN ('not_applicable','not_started','running','succeeded','failed')),
    preflight_duration_ms numeric(14,1),
    failure_stage text,
    error_class text,
    egress_id text,
    collection_run_id text,
    http_status integer,
    response_bytes bigint,
    probe_elapsed_ms numeric(14,1),
    started_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at timestamptz,
    duration_ms numeric(16,1),
    canary_status text CHECK (canary_status IS NULL OR canary_status IN ('succeeded','partial','failed','unknown')),
    planned_slots integer CHECK (planned_slots IS NULL OR planned_slots >= 0),
    tested_slots integer CHECK (tested_slots IS NULL OR tested_slots >= 0),
    available_slots integer CHECK (available_slots IS NULL OR available_slots >= 0),
    unique_egress_count integer CHECK (unique_egress_count IS NULL OR unique_egress_count >= 0),
    duplicate_egress_count integer CHECK (duplicate_egress_count IS NULL OR duplicate_egress_count >= 0),
    requested_capacity integer CHECK (requested_capacity IS NULL OR requested_capacity >= 1),
    required_slots integer CHECK (required_slots IS NULL OR required_slots >= 1),
    slot_capacity integer CHECK (slot_capacity IS NULL OR slot_capacity >= 0),
    capacity_gate_status text CHECK (capacity_gate_status IS NULL OR capacity_gate_status IN ('allowed','denied')),
    capacity_gate_reason text,
    capacity_config_hash text,
    canary_p95_latency_ms numeric(14,1),
    capacity_detail_json jsonb,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE amazon_us.operation_run
    ADD COLUMN IF NOT EXISTS canary_status text,
    ADD COLUMN IF NOT EXISTS planned_slots integer,
    ADD COLUMN IF NOT EXISTS tested_slots integer,
    ADD COLUMN IF NOT EXISTS available_slots integer,
    ADD COLUMN IF NOT EXISTS unique_egress_count integer,
    ADD COLUMN IF NOT EXISTS duplicate_egress_count integer,
    ADD COLUMN IF NOT EXISTS requested_capacity integer,
    ADD COLUMN IF NOT EXISTS required_slots integer,
    ADD COLUMN IF NOT EXISTS slot_capacity integer,
    ADD COLUMN IF NOT EXISTS capacity_gate_status text,
    ADD COLUMN IF NOT EXISTS capacity_gate_reason text,
    ADD COLUMN IF NOT EXISTS capacity_config_hash text,
    ADD COLUMN IF NOT EXISTS canary_p95_latency_ms numeric(14,1),
    ADD COLUMN IF NOT EXISTS capacity_detail_json jsonb;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='amazon_us.operation_run'::regclass
          AND conname='operation_run_operation_type_check'
          AND pg_get_constraintdef(oid) NOT LIKE '%canary%'
    ) THEN
        ALTER TABLE amazon_us.operation_run DROP CONSTRAINT operation_run_operation_type_check;
        ALTER TABLE amazon_us.operation_run ADD CONSTRAINT operation_run_operation_type_check
            CHECK (operation_type IN ('egress','canary','probe','run','reviews'));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_operation_run_tenant_started
    ON amazon_us.operation_run (tenant_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_operation_run_status_started
    ON amazon_us.operation_run (status, started_at DESC);
"""

ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
OPERATION_TYPES = {"egress", "canary", "probe", "run", "reviews"}
TERMINAL_STATUSES = {"succeeded", "failed", "blocked", "interrupted"}
PREFLIGHT_STATUSES = {"running", "succeeded", "failed"}
CAPACITY_FACT_KEYS = {
    "schema_version", "canary_status", "planned_slots", "tested_slots", "available_slots",
    "unique_egress_count", "duplicate_egress_count", "requested_capacity", "required_slots",
    "slot_capacity", "capacity_gate_status", "capacity_gate_reason", "p95_latency_ms",
    "config_hash", "sessions",
}
CAPACITY_SESSION_KEYS = {
    "session_id", "status", "auth_status", "connect_tls_status", "error_class", "http_status", "latency_ms",
}


def _default_connect(dsn: str):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("PostgreSQL operation ledger requires psycopg") from exc
    return psycopg.connect(dsn)


def _validate_id(value: str, name: str) -> str:
    value = str(value or "").strip()
    if not ID_RE.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


def _validate_label(value: str | None, name: str, *, required: bool = False) -> str | None:
    value = str(value or "").strip()
    if not value:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if not SAFE_LABEL_RE.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


def _validated_capacity_fact(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if set(value) != CAPACITY_FACT_KEYS:
        raise ValueError("invalid capacity fact fields")
    if value.get("schema_version") != "amazon-us-proxy-canary-v1":
        raise ValueError("invalid capacity fact schema")
    if value.get("canary_status") not in {"succeeded", "partial", "failed", "unknown"}:
        raise ValueError("invalid canary_status")
    if value.get("capacity_gate_status") not in {"allowed", "denied"}:
        raise ValueError("invalid capacity_gate_status")
    _validate_label(value.get("capacity_gate_reason"), "capacity_gate_reason", required=True)
    if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("config_hash") or "")):
        raise ValueError("invalid capacity config hash")
    sessions = value.get("sessions")
    if not isinstance(sessions, list):
        raise ValueError("invalid capacity sessions")
    for session in sessions:
        if not isinstance(session, dict) or set(session) != CAPACITY_SESSION_KEYS:
            raise ValueError("invalid capacity session fields")
        _validate_label(session.get("session_id"), "session_id", required=True)
        _validate_label(session.get("status"), "session_status", required=True)
        _validate_label(session.get("auth_status"), "auth_status", required=True)
        _validate_label(session.get("connect_tls_status"), "connect_tls_status", required=True)
        _validate_label(session.get("error_class"), "error_class")
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
    for forbidden in ("egress_ip", "proxy_url", "proxy_username", "proxy_password", "cookie", "authorization"):
        if forbidden in rendered.lower():
            raise ValueError("capacity fact contains forbidden data")
    return json.loads(rendered)


def ensure_schema(connect: Callable[[], Any]) -> None:
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(OPERATION_SCHEMA_SQL)
        connection.commit()


def start_operation(
    operation_id: str,
    tenant_id: str,
    operation_type: str,
    egress_id: str | None,
    collection_run_id: str | None,
    *,
    connect: Callable[[], Any],
) -> None:
    operation_id = _validate_id(operation_id, "operation_id")
    tenant_id = _validate_id(tenant_id, "tenant_id")
    operation_type = _validate_label(operation_type, "operation_type", required=True)
    if operation_type not in OPERATION_TYPES:
        raise ValueError("invalid operation_type")
    egress_id = _validate_label(egress_id, "egress_id")
    collection_run_id = _validate_id(collection_run_id, "collection_run_id") if collection_run_id else None
    preflight_status = "not_applicable" if operation_type == "egress" else "not_started"
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO amazon_us.operation_run
              (operation_id,tenant_id,operation_type,status,preflight_status,egress_id,collection_run_id)
            VALUES (%s,%s,%s,'running',%s,%s,%s)
            ON CONFLICT (operation_id) DO NOTHING
            """,
            (operation_id, tenant_id, operation_type, preflight_status, egress_id, collection_run_id),
        )
        connection.commit()


def mark_preflight(
    operation_id: str,
    tenant_id: str,
    status: str,
    duration_ms: float,
    error_class: str | None,
    *,
    connect: Callable[[], Any],
) -> None:
    operation_id = _validate_id(operation_id, "operation_id")
    tenant_id = _validate_id(tenant_id, "tenant_id")
    if status not in PREFLIGHT_STATUSES:
        raise ValueError("invalid preflight status")
    error_class = _validate_label(error_class, "error_class")
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE amazon_us.operation_run
            SET preflight_status=%s,preflight_duration_ms=%s,error_class=COALESCE(%s,error_class),
                updated_at=CURRENT_TIMESTAMP
            WHERE operation_id=%s AND tenant_id=%s AND status='running'
            """,
            (status, round(float(duration_ms), 1), error_class, operation_id, tenant_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("preflight update did not match a running operation")
        connection.commit()


def finish_operation(
    operation_id: str,
    tenant_id: str,
    status: str,
    failure_stage: str | None,
    error_class: str | None,
    *,
    http_status: int | None = None,
    response_bytes: int | None = None,
    probe_elapsed_ms: float | None = None,
    capacity_fact: dict[str, Any] | None = None,
    connect: Callable[[], Any],
) -> None:
    operation_id = _validate_id(operation_id, "operation_id")
    tenant_id = _validate_id(tenant_id, "tenant_id")
    if status not in TERMINAL_STATUSES:
        raise ValueError("invalid operation status")
    failure_stage = _validate_label(failure_stage, "failure_stage")
    error_class = _validate_label(error_class, "error_class")
    capacity_fact = _validated_capacity_fact(capacity_fact)
    capacity_values = capacity_fact or {}
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE amazon_us.operation_run
            SET status=%s,failure_stage=%s,error_class=%s,http_status=%s,response_bytes=%s,
                probe_elapsed_ms=%s,finished_at=CURRENT_TIMESTAMP,
                duration_ms=EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-started_at))*1000,
                canary_status=%s,planned_slots=%s,tested_slots=%s,available_slots=%s,
                unique_egress_count=%s,duplicate_egress_count=%s,requested_capacity=%s,
                required_slots=%s,slot_capacity=%s,capacity_gate_status=%s,capacity_gate_reason=%s,
                capacity_config_hash=%s,canary_p95_latency_ms=%s,capacity_detail_json=%s::jsonb,
                updated_at=CURRENT_TIMESTAMP
            WHERE operation_id=%s AND tenant_id=%s AND status='running'
            """,
            (
                status, failure_stage, error_class, http_status, response_bytes,
                round(float(probe_elapsed_ms), 1) if probe_elapsed_ms is not None else None,
                capacity_values.get("canary_status"), capacity_values.get("planned_slots"),
                capacity_values.get("tested_slots"), capacity_values.get("available_slots"),
                capacity_values.get("unique_egress_count"), capacity_values.get("duplicate_egress_count"),
                capacity_values.get("requested_capacity"), capacity_values.get("required_slots"),
                capacity_values.get("slot_capacity"), capacity_values.get("capacity_gate_status"),
                capacity_values.get("capacity_gate_reason"), capacity_values.get("config_hash"),
                capacity_values.get("p95_latency_ms"), json.dumps(capacity_fact) if capacity_fact is not None else None,
                operation_id, tenant_id,
            ),
        )
        if cursor.rowcount != 1:
            cursor.execute(
                "SELECT status FROM amazon_us.operation_run WHERE operation_id=%s AND tenant_id=%s",
                (operation_id, tenant_id),
            )
            row = cursor.fetchone()
            existing = row.get("status") if isinstance(row, dict) else row[0] if row else None
            if existing in TERMINAL_STATUSES:
                connection.commit()
                return
            raise RuntimeError("terminal update did not match a running operation")
        connection.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", choices=("ensure-schema", "start", "preflight", "finish"))
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--operation-id")
    parser.add_argument("--tenant-id")
    parser.add_argument("--operation-type")
    parser.add_argument("--egress-id")
    parser.add_argument("--collection-run-id")
    parser.add_argument("--status")
    parser.add_argument("--duration-ms", type=float)
    parser.add_argument("--failure-stage")
    parser.add_argument("--error-class")
    parser.add_argument("--http-status", type=int)
    parser.add_argument("--response-bytes", type=int)
    parser.add_argument("--probe-elapsed-ms", type=float)
    args = parser.parse_args(argv)
    try:
        dsn = os.environ.get(args.dsn_env, "").strip()
        if not dsn:
            raise ValueError(f"PostgreSQL DSN environment variable is required: {args.dsn_env}")
        connect = lambda: _default_connect(dsn)
        if args.event == "ensure-schema":
            ensure_schema(connect)
        elif args.event == "start":
            start_operation(
                args.operation_id or "", args.tenant_id or "", args.operation_type or "",
                args.egress_id, args.collection_run_id, connect=connect,
            )
        elif args.event == "preflight":
            mark_preflight(
                args.operation_id or "", args.tenant_id or "", args.status or "",
                float(args.duration_ms or 0), args.error_class, connect=connect,
            )
        else:
            finish_operation(
                args.operation_id or "", args.tenant_id or "", args.status or "",
                args.failure_stage, args.error_class, http_status=args.http_status,
                response_bytes=args.response_bytes, probe_elapsed_ms=args.probe_elapsed_ms,
                connect=connect,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"operation ledger failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
