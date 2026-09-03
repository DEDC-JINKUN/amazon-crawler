from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "operation_ledger.py"


def load_module():
    spec = importlib.util.spec_from_file_location("operation_ledger_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Cursor:
    def __init__(self):
        self.executed = []
        self.rowcount = 1
        self.current_status = None

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): return False
    def execute(self, sql, params=()): self.executed.append((sql, tuple(params)))
    def fetchone(self): return (self.current_status,) if self.current_status else None


class Connection:
    def __init__(self):
        self.cursor_instance = Cursor()
        self.commits = 0

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): return False
    def cursor(self): return self.cursor_instance
    def commit(self): self.commits += 1


def test_preflight_failure_remains_visible_as_terminal_operation():
    module = load_module()
    connection = Connection()
    connect = lambda: connection

    module.ensure_schema(connect)
    module.start_operation("op-1", "tenant-a", "run", "dataimpulse-us", "run-1", connect=connect)
    module.mark_preflight("op-1", "tenant-a", "failed", 2534.1, "network_error", connect=connect)
    module.finish_operation("op-1", "tenant-a", "failed", "preflight", "network_error", connect=connect)

    sql = "\n".join(statement for statement, _ in connection.cursor_instance.executed)
    assert "CREATE TABLE IF NOT EXISTS amazon_us.operation_run" in sql
    assert "preflight_status" in sql
    assert "failure_stage" in sql
    assert "error_class" in sql
    assert connection.commits == 4


def test_operation_fields_reject_secret_bearing_values():
    module = load_module()
    connection = Connection()
    with pytest.raises(ValueError):
        module.start_operation("op-1", "tenant-a", "egress", "http://user:pass@example:823", None, connect=lambda: connection)
    assert connection.commits == 0


def test_interrupted_operation_cannot_be_overwritten_as_failed():
    module = load_module()
    connection = Connection()
    connection.cursor_instance.rowcount = 0
    connection.cursor_instance.current_status = "interrupted"

    module.finish_operation(
        "op-1", "tenant-a", "failed", "worker", "worker_failed", connect=lambda: connection
    )

    assert connection.commits == 1
    assert "status='running'" in connection.cursor_instance.executed[0][0]


def test_canary_capacity_fact_is_persisted_without_egress_identity_or_unknown_zero():
    module = load_module()
    connection = Connection()
    connect = lambda: connection
    fact = {
        "schema_version": "amazon-us-proxy-canary-v1",
        "canary_status": "unknown",
        "planned_slots": 3,
        "tested_slots": 0,
        "available_slots": None,
        "unique_egress_count": None,
        "duplicate_egress_count": None,
        "requested_capacity": 3,
        "required_slots": 1,
        "slot_capacity": None,
        "capacity_gate_status": "denied",
        "capacity_gate_reason": "credentials_missing",
        "p95_latency_ms": None,
        "config_hash": "a" * 64,
        "sessions": [],
    }

    module.ensure_schema(connect)
    module.start_operation("op-canary-1", "tenant-a", "canary", "dataimpulse-us", None, connect=connect)
    module.finish_operation(
        "op-canary-1", "tenant-a", "failed", "capacity_gate", "credentials_missing",
        capacity_fact=fact, connect=connect,
    )

    sql = "\n".join(statement for statement, _ in connection.cursor_instance.executed)
    assert "canary_status" in sql
    assert "capacity_gate_status" in sql
    assert "capacity_detail_json" in sql
    finish_params = connection.cursor_instance.executed[-1][1]
    assert None in finish_params
    rendered = repr(finish_params)
    assert "203.0.113" not in rendered
    assert "proxy.example" not in rendered
    assert "secret" not in rendered
