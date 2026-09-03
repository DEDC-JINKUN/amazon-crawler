#!/usr/bin/env python3
"""PostgreSQL lifecycle ledger for audited crawler control operations."""
from __future__ import annotations

import argparse
import json
import math
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
    slot_budget integer CHECK (slot_budget IS NULL OR slot_budget >= 1),
    slot_capacity integer CHECK (slot_capacity IS NULL OR slot_capacity >= 0),
    capacity_gate_status text CHECK (capacity_gate_status IS NULL OR capacity_gate_status IN ('allowed','denied')),
    capacity_gate_reason text,
    capacity_config_hash text,
    credential_generation text,
    canary_p95_latency_ms numeric(14,1),
    capacity_detail_json jsonb,
    authorizing_canary_operation_id text,
    capacity_reservation_id text,
    capacity_fact_finished_at timestamptz,
    capacity_fact_expires_at timestamptz,
    reserved_slots integer,
    capacity_authorization_json jsonb,
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
    ADD COLUMN IF NOT EXISTS slot_budget integer,
    ADD COLUMN IF NOT EXISTS slot_capacity integer,
    ADD COLUMN IF NOT EXISTS capacity_gate_status text,
    ADD COLUMN IF NOT EXISTS capacity_gate_reason text,
    ADD COLUMN IF NOT EXISTS capacity_config_hash text,
    ADD COLUMN IF NOT EXISTS credential_generation text,
    ADD COLUMN IF NOT EXISTS canary_p95_latency_ms numeric(14,1),
    ADD COLUMN IF NOT EXISTS capacity_detail_json jsonb;
ALTER TABLE amazon_us.operation_run
    ADD COLUMN IF NOT EXISTS authorizing_canary_operation_id text,
    ADD COLUMN IF NOT EXISTS capacity_reservation_id text,
    ADD COLUMN IF NOT EXISTS capacity_fact_finished_at timestamptz,
    ADD COLUMN IF NOT EXISTS capacity_fact_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS reserved_slots integer,
    ADD COLUMN IF NOT EXISTS capacity_authorization_json jsonb;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='operation_canary_counts_v2_check' AND conrelid='amazon_us.operation_run'::regclass) THEN
        ALTER TABLE amazon_us.operation_run ADD CONSTRAINT operation_canary_counts_v2_check CHECK (
            canary_status IS NULL OR (
                planned_slots IS NOT NULL AND tested_slots IS NOT NULL
                AND planned_slots >= 1 AND tested_slots >= 0 AND tested_slots <= planned_slots
                AND (available_slots IS NULL OR (available_slots >= 0 AND available_slots <= tested_slots))
                AND (unique_egress_count IS NULL OR (available_slots IS NOT NULL AND unique_egress_count >= 0 AND unique_egress_count <= available_slots))
                AND (duplicate_egress_count IS NULL OR (available_slots IS NOT NULL AND unique_egress_count IS NOT NULL AND duplicate_egress_count=available_slots-unique_egress_count))
                AND (slot_capacity IS NULL OR (slot_budget IS NOT NULL AND unique_egress_count IS NOT NULL AND slot_capacity=slot_budget*unique_egress_count))
            )
        ) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='operation_capacity_binding_v2_check' AND conrelid='amazon_us.operation_run'::regclass) THEN
        ALTER TABLE amazon_us.operation_run ADD CONSTRAINT operation_capacity_binding_v2_check CHECK (
            capacity_reservation_id IS NULL OR (
                authorizing_canary_operation_id IS NOT NULL
                AND capacity_fact_finished_at IS NOT NULL
                AND capacity_fact_expires_at IS NOT NULL
                AND reserved_slots IS NOT NULL AND reserved_slots > 0
                AND capacity_authorization_json IS NOT NULL
            )
        ) NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='operation_canary_state_v2_check' AND conrelid='amazon_us.operation_run'::regclass) THEN
        ALTER TABLE amazon_us.operation_run ADD CONSTRAINT operation_canary_state_v2_check CHECK (
            canary_status IS NULL OR
            (canary_status='unknown' AND capacity_gate_status='denied' AND tested_slots=0
             AND available_slots IS NULL AND unique_egress_count IS NULL
             AND duplicate_egress_count IS NULL AND slot_capacity IS NULL) OR
            (canary_status='succeeded' AND capacity_gate_status='allowed'
             AND tested_slots=planned_slots AND available_slots=planned_slots
             AND unique_egress_count=planned_slots AND duplicate_egress_count=0) OR
            (canary_status='partial' AND unique_egress_count>0
             AND NOT (tested_slots=planned_slots AND available_slots=planned_slots
                      AND unique_egress_count=planned_slots AND duplicate_egress_count=0)
             AND ((capacity_gate_status='allowed')=(slot_capacity>=requested_capacity AND unique_egress_count>=required_slots))) OR
            (canary_status='failed' AND capacity_gate_status='denied' AND unique_egress_count=0)
        ) NOT VALID;
    END IF;
END $$;
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
CREATE TABLE IF NOT EXISTS amazon_us.proxy_capacity_reservation (
    reservation_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    owner_id text NOT NULL,
    canary_operation_id text,
    capacity_config_hash text NOT NULL,
    credential_generation text NOT NULL,
    requested_capacity integer NOT NULL CHECK (requested_capacity > 0),
    required_slots integer NOT NULL CHECK (required_slots > 0),
    reserved_slots integer NOT NULL CHECK (reserved_slots >= 0),
    slot_ids_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL CHECK (status IN ('active','released','expired','denied')),
    reason text NOT NULL,
    fact_finished_at timestamptz,
    fact_expires_at timestamptz,
    expires_at timestamptz,
    capacity_snapshot_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    released_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT proxy_capacity_reservation_state_check CHECK (
        (status='denied' AND reserved_slots=0 AND jsonb_array_length(slot_ids_json)=0) OR
        (status IN ('active','released','expired') AND canary_operation_id IS NOT NULL
         AND reserved_slots=required_slots AND reserved_slots=jsonb_array_length(slot_ids_json)
         AND fact_finished_at IS NOT NULL AND fact_expires_at IS NOT NULL AND expires_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_proxy_capacity_active_config
    ON amazon_us.proxy_capacity_reservation (capacity_config_hash, expires_at)
    WHERE status='active';
"""

ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
OPERATION_TYPES = {"egress", "canary", "probe", "run", "reviews"}
TERMINAL_STATUSES = {"succeeded", "failed", "blocked", "interrupted"}
PREFLIGHT_STATUSES = {"running", "succeeded", "failed"}
CAPACITY_FACT_KEYS = {
    "schema_version", "canary_status", "planned_slots", "tested_slots", "available_slots",
    "unique_egress_count", "duplicate_egress_count", "requested_capacity", "required_slots",
    "slot_budget", "slot_capacity", "capacity_gate_status", "capacity_gate_reason", "p95_latency_ms",
    "config_hash", "credential_generation", "sessions",
}
CAPACITY_SESSION_KEYS = {
    "session_id", "status", "usable", "auth_status", "connect_tls_status", "error_class", "http_status", "latency_ms",
}
CAPACITY_AUTHORIZATION_KEYS = {
    "status", "reason", "reservation_id", "owner_id", "canary_operation_id",
    "capacity_config_hash", "credential_generation", "requested_capacity",
    "required_slots", "reserved_slots", "slot_ids", "fact_finished_at",
    "fact_expires_at", "reservation_expires_at", "capacity_snapshot",
}
CAPACITY_SNAPSHOT_KEYS = {
    "canary_status", "planned_slots", "tested_slots", "available_slots", "unique_egress_count",
    "duplicate_egress_count", "requested_capacity", "required_slots", "slot_budget", "slot_capacity",
    "capacity_gate_status", "capacity_gate_reason", "canary_p95_latency_ms",
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
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,100}", str(value.get("credential_generation") or "")):
        raise ValueError("invalid credential generation")
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
        if not isinstance(session.get("usable"), bool):
            raise ValueError("capacity session usable must be boolean")
        if session.get("usable") and session.get("status") != "available":
            raise ValueError("only available capacity sessions can be usable")
    session_ids = [str(session.get("session_id")) for session in sessions]
    if len(set(session_ids)) != len(session_ids):
        raise ValueError("capacity session ids must be unique")
    canary_status = str(value.get("canary_status") or "")
    gate_status = str(value.get("capacity_gate_status") or "")
    if canary_status == "unknown":
        if gate_status != "denied" or int(value.get("tested_slots") or 0) != 0 or sessions:
            raise ValueError("inconsistent unknown capacity fact")
        if any(value.get(name) is not None for name in ("available_slots", "unique_egress_count", "duplicate_egress_count", "slot_capacity")):
            raise ValueError("unknown capacity fact must preserve null counts")
    else:
        names = (
            "planned_slots", "tested_slots", "available_slots", "unique_egress_count",
            "duplicate_egress_count", "requested_capacity", "required_slots", "slot_budget", "slot_capacity",
        )
        counts: dict[str, int] = {}
        for name in names:
            raw = value.get(name)
            if raw is None or isinstance(raw, bool):
                raise ValueError("capacity fact count is missing")
            counts[name] = int(raw)
        planned, tested, available, unique, duplicates = (
            counts["planned_slots"], counts["tested_slots"], counts["available_slots"],
            counts["unique_egress_count"], counts["duplicate_egress_count"],
        )
        requested, required, budget, slot_capacity = (
            counts["requested_capacity"], counts["required_slots"], counts["slot_budget"], counts["slot_capacity"],
        )
        available_from_sessions = sum(item.get("status") == "available" for item in sessions)
        usable_from_sessions = sum(item.get("usable") is True for item in sessions)
        all_unique = tested == planned == available == unique and duplicates == 0
        allowed = slot_capacity >= requested and unique >= required
        consistent = bool(
            planned >= 1 and tested == planned and len(sessions) == tested
            and 0 <= unique <= available <= tested and duplicates == available - unique
            and available_from_sessions == available and usable_from_sessions == unique
            and requested >= 1 and budget >= 1 and required == math.ceil(requested / budget)
            and slot_capacity == unique * budget
            and ((canary_status == "succeeded" and all_unique)
                 or (canary_status == "partial" and unique > 0 and not all_unique)
                 or (canary_status == "failed" and unique == 0))
            and ((gate_status == "allowed") == allowed)
            and ((value.get("capacity_gate_reason") == "capacity_sufficient") == allowed)
        )
        if not consistent:
            raise ValueError("inconsistent capacity fact")
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
    for forbidden in ("egress_ip", "proxy_url", "username", "password", "cookie", "authorization"):
        if forbidden in rendered.lower():
            raise ValueError("capacity fact contains forbidden data")
    return json.loads(rendered)


def _validated_capacity_authorization(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != CAPACITY_AUTHORIZATION_KEYS:
        raise ValueError("invalid capacity authorization fields")
    if value.get("status") != "active" or value.get("reason") != "capacity_reserved":
        raise ValueError("capacity authorization is not active")
    for name in ("reservation_id", "owner_id", "canary_operation_id"):
        _validate_id(str(value.get(name) or ""), name)
    if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("capacity_config_hash") or "")):
        raise ValueError("invalid capacity authorization hash")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,100}", str(value.get("credential_generation") or "")):
        raise ValueError("invalid credential generation")
    requested = int(value.get("requested_capacity") or 0)
    required = int(value.get("required_slots") or 0)
    reserved = int(value.get("reserved_slots") or 0)
    slot_ids = list(value.get("slot_ids") or [])
    if requested < 1 or required < 1 or reserved != required or len(slot_ids) != reserved:
        raise ValueError("invalid capacity authorization counts")
    if len(set(slot_ids)) != len(slot_ids) or any(not re.fullmatch(r"session-\d{2}", str(item)) for item in slot_ids):
        raise ValueError("invalid reserved slot ids")
    for name in ("fact_finished_at", "fact_expires_at", "reservation_expires_at"):
        if not str(value.get(name) or "").strip():
            raise ValueError(f"{name} is required")
    if not isinstance(value.get("capacity_snapshot"), dict) or not set(value["capacity_snapshot"]).issubset(CAPACITY_SNAPSHOT_KEYS):
        raise ValueError("capacity snapshot is required")
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
    for forbidden in ("egress_ip", "proxy_url", "username", "password", "cookie", "authorization"):
        if forbidden in rendered.lower():
            raise ValueError("capacity authorization contains forbidden data")
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


def bind_capacity_authorization(
    operation_id: str,
    tenant_id: str,
    authorization: dict[str, Any],
    *,
    connect: Callable[[], Any],
) -> None:
    operation_id = _validate_id(operation_id, "operation_id")
    tenant_id = _validate_id(tenant_id, "tenant_id")
    authorization = _validated_capacity_authorization(authorization)
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE amazon_us.operation_run
            SET authorizing_canary_operation_id=%s,capacity_reservation_id=%s,
                capacity_fact_finished_at=%s,capacity_fact_expires_at=%s,reserved_slots=%s,
                capacity_authorization_json=%s::jsonb,updated_at=CURRENT_TIMESTAMP
            WHERE operation_id=%s AND tenant_id=%s AND status='running'
            """,
            (
                authorization["canary_operation_id"], authorization["reservation_id"],
                authorization["fact_finished_at"], authorization["fact_expires_at"],
                authorization["reserved_slots"], json.dumps(authorization, ensure_ascii=False),
                operation_id, tenant_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("capacity authorization did not match a running operation")
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
                required_slots=%s,slot_budget=%s,slot_capacity=%s,capacity_gate_status=%s,capacity_gate_reason=%s,
                capacity_config_hash=%s,credential_generation=%s,canary_p95_latency_ms=%s,capacity_detail_json=%s::jsonb,
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
                capacity_values.get("slot_budget"), capacity_values.get("slot_capacity"), capacity_values.get("capacity_gate_status"),
                capacity_values.get("capacity_gate_reason"), capacity_values.get("config_hash"),
                capacity_values.get("credential_generation"), capacity_values.get("p95_latency_ms"),
                json.dumps(capacity_fact) if capacity_fact is not None else None,
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
    parser.add_argument("event", choices=("ensure-schema", "start", "preflight", "capacity", "finish"))
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
    parser.add_argument("--capacity-authorization", type=Path)
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
        elif args.event == "capacity":
            if args.capacity_authorization is None:
                raise ValueError("--capacity-authorization is required")
            authorization = json.loads(args.capacity_authorization.read_text(encoding="utf-8-sig"))
            bind_capacity_authorization(
                args.operation_id or "", args.tenant_id or "", authorization, connect=connect,
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
