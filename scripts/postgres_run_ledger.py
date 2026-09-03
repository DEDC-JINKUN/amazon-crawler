#!/usr/bin/env python3
"""Durable PostgreSQL lifecycle and receipt ledger for controlled crawler runs."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


RUN_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS amazon_us;
CREATE TABLE IF NOT EXISTS amazon_us.collection_run (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    command text NOT NULL,
    requested_actions integer NOT NULL CHECK (requested_actions > 0),
    status text NOT NULL CHECK (status IN ('starting','running','completed','blocked','quality_failed','failed','interrupted')),
    worker_id text,
    controller_pid integer,
    controller_exit_code integer,
    worker_exit_code integer,
    termination_reason text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    receipt_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    control_operation_id text,
    authorizing_canary_operation_id text,
    capacity_reservation_id text,
    capacity_fact_finished_at timestamptz,
    capacity_fact_expires_at timestamptz,
    reserved_slots integer,
    capacity_authorization_json jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id)
);
ALTER TABLE amazon_us.collection_run
    ADD COLUMN IF NOT EXISTS control_operation_id text,
    ADD COLUMN IF NOT EXISTS authorizing_canary_operation_id text,
    ADD COLUMN IF NOT EXISTS capacity_reservation_id text,
    ADD COLUMN IF NOT EXISTS capacity_fact_finished_at timestamptz,
    ADD COLUMN IF NOT EXISTS capacity_fact_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS reserved_slots integer,
    ADD COLUMN IF NOT EXISTS capacity_authorization_json jsonb;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='collection_run_capacity_binding_v2_check' AND conrelid='amazon_us.collection_run'::regclass) THEN
    ALTER TABLE amazon_us.collection_run ADD CONSTRAINT collection_run_capacity_binding_v2_check CHECK (
      capacity_reservation_id IS NULL OR (
        control_operation_id IS NOT NULL AND authorizing_canary_operation_id IS NOT NULL
        AND capacity_fact_finished_at IS NOT NULL AND capacity_fact_expires_at IS NOT NULL
        AND reserved_slots IS NOT NULL AND reserved_slots > 0 AND capacity_authorization_json IS NOT NULL
      )
    ) NOT VALID;
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_collection_run_tenant_started
    ON amazon_us.collection_run (tenant_id, started_at DESC);
DO $$
BEGIN
  IF to_regclass('amazon_us.collection_evidence') IS NOT NULL THEN
    CREATE INDEX IF NOT EXISTS idx_evidence_tenant_identity_latest
      ON amazon_us.collection_evidence (tenant_id, marketplace, asin, subject_type, id DESC);
  END IF;
END $$;
"""


def _default_connect(dsn: str):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("PostgreSQL run ledger requires optional dependency psycopg") from exc
    return psycopg.connect(dsn)


def ensure_schema(connect: Callable[[], Any]) -> None:
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(RUN_SCHEMA_SQL)
        connection.commit()


def start_run(
    connect: Callable[[], Any],
    *,
    tenant_id: str,
    run_id: str,
    command: str,
    requested_actions: int,
    worker_id: str,
    controller_pid: int,
    operation_id: str,
) -> None:
    if not tenant_id or not run_id or not operation_id or requested_actions < 1:
        raise ValueError("tenant_id, run_id, operation_id and positive requested_actions are required")
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO amazon_us.collection_run
              (tenant_id,run_id,command,requested_actions,status,worker_id,controller_pid,
               control_operation_id,authorizing_canary_operation_id,capacity_reservation_id,
               capacity_fact_finished_at,capacity_fact_expires_at,reserved_slots,capacity_authorization_json,
               started_at,updated_at)
            SELECT %s,%s,%s,%s,'running',%s,%s,o.operation_id,o.authorizing_canary_operation_id,
                   o.capacity_reservation_id,o.capacity_fact_finished_at,o.capacity_fact_expires_at,
                   o.reserved_slots,o.capacity_authorization_json,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP
            FROM amazon_us.operation_run o
            WHERE o.operation_id=%s AND o.tenant_id=%s AND o.status='running'
              AND o.capacity_reservation_id IS NOT NULL AND o.authorizing_canary_operation_id IS NOT NULL
            ON CONFLICT (tenant_id,run_id) DO NOTHING
            """,
            (tenant_id, run_id, command, requested_actions, worker_id, controller_pid, operation_id, tenant_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("run start requires a bound active capacity authorization")
        connection.commit()


def finish_run(
    connect: Callable[[], Any],
    *,
    tenant_id: str,
    run_id: str,
    status: str,
    controller_exit_code: int | None,
    worker_exit_code: int | None,
    termination_reason: str | None,
    receipt: dict[str, Any],
) -> None:
    if status not in {"completed", "blocked", "quality_failed", "failed", "interrupted"}:
        raise ValueError(f"invalid terminal run status: {status}")
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE amazon_us.collection_run
            SET status=%s,controller_exit_code=%s,worker_exit_code=%s,termination_reason=%s,
                receipt_json=%s,finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
            WHERE tenant_id=%s AND run_id=%s AND status IN ('starting','running')
            """,
            (
                status,
                controller_exit_code,
                worker_exit_code,
                termination_reason,
                json.dumps(receipt, ensure_ascii=False),
                tenant_id,
                run_id,
            ),
        )
        if cursor.rowcount != 1:
            cursor.execute(
                "SELECT status FROM amazon_us.collection_run WHERE tenant_id=%s AND run_id=%s",
                (tenant_id, run_id),
            )
            row = cursor.fetchone()
            existing = row.get("status") if isinstance(row, dict) else row[0] if row else None
            if existing in {"completed", "blocked", "quality_failed", "failed", "interrupted"}:
                connection.commit()
                return
            raise RuntimeError("run ledger terminal update did not match an existing run")
        connection.commit()


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def finish_interrupted_from_host(lifecycle: dict[str, Any], worker_exit_code: int | None) -> None:
    """Fail closed when the controller disappears; PostgreSQL is updated before the file mirror."""
    dsn_env = str(lifecycle.get("dsn_env") or "AMAZON_US_POSTGRES_DSN")
    dsn = os.environ.get(dsn_env, "").strip()
    if not dsn:
        raise RuntimeError(f"PostgreSQL DSN environment variable is required: {dsn_env}")
    now = datetime.now(timezone.utc).isoformat()
    capacity_cleanup_status = "not_applicable"
    capacity_cleanup_reason = None
    capacity_authorization = lifecycle.get("capacity_authorization") or {}
    reservation_id = capacity_authorization.get("reservation_id") if isinstance(capacity_authorization, dict) else None
    worker_id = lifecycle.get("worker_id")
    if reservation_id and worker_id:
        try:
            from postgres_worker_storage import PostgresWorkerStorage
            released = PostgresWorkerStorage(dsn, tenant_id=str(lifecycle["tenant_id"])).release_proxy_capacity(
                str(reservation_id), str(worker_id)
            )
            capacity_cleanup_status = "released" if released else "already_inactive"
        except Exception:
            capacity_cleanup_status = "ttl_fallback"
            capacity_cleanup_reason = "capacity_release_failed"
    receipt = {
        "schema_version": "amazon-us-control-receipt-v3",
        "run_id": lifecycle["run_id"],
        "tenant_id": lifecycle["tenant_id"],
        "command": lifecycle["command"],
        "requested_limit": int(lifecycle["requested_actions"]),
        "status": "interrupted",
        "exit_code": 130,
        "worker_exit_code": worker_exit_code,
        "termination_reason": "controller_exited",
        "capacity_authorization": lifecycle.get("capacity_authorization"),
        "capacity_cleanup_status": capacity_cleanup_status,
        "capacity_cleanup_reason": capacity_cleanup_reason,
        "finished_at": now,
    }
    connect = lambda: _default_connect(dsn)
    finish_run(
        connect,
        tenant_id=str(lifecycle["tenant_id"]),
        run_id=str(lifecycle["run_id"]),
        status="interrupted",
        controller_exit_code=130,
        worker_exit_code=worker_exit_code,
        termination_reason="controller_exited",
        receipt=receipt,
    )
    operation_id = lifecycle.get("operation_id")
    if operation_id:
        from operation_ledger import finish_operation
        finish_operation(
            str(operation_id),
            str(lifecycle["tenant_id"]),
            "interrupted",
            "worker",
            "controller_exited",
            connect=connect,
        )
    receipt_path = lifecycle.get("receipt_path")
    if receipt_path:
        _write_atomic(Path(receipt_path), receipt)


def _load_receipt(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("receipt must be a JSON object")
    return value


def _load_receipt_stdin() -> dict[str, Any]:
    value = json.loads(sys.stdin.read())
    if not isinstance(value, dict):
        raise ValueError("receipt must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event", choices=("ensure-schema", "start", "finish", "interrupt"))
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--tenant-id")
    parser.add_argument("--run-id")
    parser.add_argument("--operation-id")
    parser.add_argument("--command")
    parser.add_argument("--requested-actions", type=int)
    parser.add_argument("--worker-id")
    parser.add_argument("--controller-pid", type=int)
    parser.add_argument("--status")
    parser.add_argument("--controller-exit-code", type=int)
    parser.add_argument("--worker-exit-code", type=int)
    parser.add_argument("--termination-reason")
    parser.add_argument("--receipt", type=Path)
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
            start_run(
                connect,
                tenant_id=str(args.tenant_id or ""),
                run_id=str(args.run_id or ""),
                command=str(args.command or ""),
                requested_actions=int(args.requested_actions or 0),
                worker_id=str(args.worker_id or ""),
                controller_pid=int(args.controller_pid or 0),
                operation_id=str(args.operation_id or ""),
            )
        elif args.event == "finish":
            receipt = _load_receipt(args.receipt) if args.receipt is not None else _load_receipt_stdin()
            finish_run(
                connect,
                tenant_id=str(args.tenant_id or ""),
                run_id=str(args.run_id or ""),
                status=str(args.status or ""),
                controller_exit_code=args.controller_exit_code,
                worker_exit_code=args.worker_exit_code,
                termination_reason=args.termination_reason,
                receipt=receipt,
            )
        else:
            if args.receipt is None:
                raise ValueError("--receipt is required for interrupt")
            finish_interrupted_from_host(
                {
                    "dsn_env": args.dsn_env,
                    "tenant_id": str(args.tenant_id or ""),
                    "run_id": str(args.run_id or ""),
                    "command": str(args.command or ""),
                    "requested_actions": int(args.requested_actions or 0),
                    "worker_id": str(args.worker_id or ""),
                    "receipt_path": str(args.receipt),
                    "operation_id": args.operation_id,
                    "capacity_authorization": (
                        _load_receipt(args.capacity_authorization)
                        if args.capacity_authorization is not None else None
                    ),
                },
                args.worker_exit_code,
            )
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"run ledger failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
