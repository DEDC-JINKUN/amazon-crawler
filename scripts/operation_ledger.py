#!/usr/bin/env python3
"""PostgreSQL lifecycle ledger for audited crawler control operations."""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any, Callable


OPERATION_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS amazon_us;
CREATE TABLE IF NOT EXISTS amazon_us.operation_run (
    operation_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    operation_type text NOT NULL CHECK (operation_type IN ('egress','probe','run','reviews')),
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
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_operation_run_tenant_started
    ON amazon_us.operation_run (tenant_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_operation_run_status_started
    ON amazon_us.operation_run (status, started_at DESC);
"""

ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
OPERATION_TYPES = {"egress", "probe", "run", "reviews"}
TERMINAL_STATUSES = {"succeeded", "failed", "blocked", "interrupted"}
PREFLIGHT_STATUSES = {"running", "succeeded", "failed"}


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
    connect: Callable[[], Any],
) -> None:
    operation_id = _validate_id(operation_id, "operation_id")
    tenant_id = _validate_id(tenant_id, "tenant_id")
    if status not in TERMINAL_STATUSES:
        raise ValueError("invalid operation status")
    failure_stage = _validate_label(failure_stage, "failure_stage")
    error_class = _validate_label(error_class, "error_class")
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE amazon_us.operation_run
            SET status=%s,failure_stage=%s,error_class=%s,http_status=%s,response_bytes=%s,
                probe_elapsed_ms=%s,finished_at=CURRENT_TIMESTAMP,
                duration_ms=EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP-started_at))*1000,
                updated_at=CURRENT_TIMESTAMP
            WHERE operation_id=%s AND tenant_id=%s AND status='running'
            """,
            (
                status, failure_stage, error_class, http_status, response_bytes,
                round(float(probe_elapsed_ms), 1) if probe_elapsed_ms is not None else None,
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
