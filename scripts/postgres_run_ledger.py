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
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id)
);
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
) -> None:
    if not tenant_id or not run_id or requested_actions < 1:
        raise ValueError("tenant_id, run_id and positive requested_actions are required")
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO amazon_us.collection_run
              (tenant_id,run_id,command,requested_actions,status,worker_id,controller_pid,started_at,updated_at)
            VALUES (%s,%s,%s,%s,'running',%s,%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            ON CONFLICT (tenant_id,run_id) DO NOTHING
            """,
            (tenant_id, run_id, command, requested_actions, worker_id, controller_pid),
        )
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
            WHERE tenant_id=%s AND run_id=%s
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
    parser.add_argument("--command")
    parser.add_argument("--requested-actions", type=int)
    parser.add_argument("--worker-id")
    parser.add_argument("--controller-pid", type=int)
    parser.add_argument("--status")
    parser.add_argument("--controller-exit-code", type=int)
    parser.add_argument("--worker-exit-code", type=int)
    parser.add_argument("--termination-reason")
    parser.add_argument("--receipt", type=Path)
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
                    "receipt_path": str(args.receipt),
                },
                args.worker_exit_code,
            )
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"run ledger failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
