from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "operation_ledger.py"


def test_operation_ledger_cli_help_builds_capacity_authorization_parser():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "--capacity-authorization" in result.stdout
    assert "NameError" not in result.stderr


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
    assert "operation_canary_counts_v2_check" in sql
    assert "operation_canary_state_v4_check" in sql
    assert "DROP CONSTRAINT operation_canary_state_v2_check" in sql
    assert "operation_capacity_binding_v2_check" in sql
    assert connection.commits == 4


def test_canary_state_constraint_allows_all_healthy_but_capacity_insufficient():
    module = load_module()
    normalized = " ".join(module.OPERATION_SCHEMA_SQL.split())

    assert "canary_status='succeeded'" in normalized
    assert "capacity_gate_reason='replacement_capacity_insufficient'" in normalized
    assert "capacity_gate_status='allowed' AND slot_capacity>=requested_capacity" in normalized


def test_capacity_fact_accepts_fail_closed_replacement_capacity_denial():
    module = load_module()
    fact = {
        "schema_version": "amazon-us-proxy-canary-v1", "canary_status": "succeeded",
        "planned_slots": 1, "tested_slots": 1, "available_slots": 1, "unique_egress_count": 1,
        "duplicate_egress_count": 0, "requested_capacity": 1, "required_slots": 1,
        "slot_budget": 1, "slot_capacity": 1, "capacity_gate_status": "denied",
        "capacity_gate_reason": "replacement_capacity_insufficient",
        "credential_generation": "test-generation-1", "p95_latency_ms": 1.0,
        "config_hash": "a" * 64,
        "sessions": [{"session_id": "session-01", "status": "available", "usable": True,
                      "auth_status": "succeeded", "connect_tls_status": "succeeded",
                      "error_class": None, "http_status": 200, "latency_ms": 1.0}],
    }

    assert module._validated_capacity_fact(fact)["capacity_gate_reason"] == "replacement_capacity_insufficient"


def test_capacity_fact_rejects_out_of_range_session_ids():
    module = load_module()
    fact = {
        "schema_version": "amazon-us-proxy-canary-v1", "canary_status": "succeeded",
        "planned_slots": 1, "tested_slots": 1, "available_slots": 1, "unique_egress_count": 1,
        "duplicate_egress_count": 0, "requested_capacity": 1, "required_slots": 1,
        "slot_budget": 1, "slot_capacity": 1, "capacity_gate_status": "allowed",
        "capacity_gate_reason": "capacity_sufficient", "credential_generation": "test-generation-1",
        "p95_latency_ms": 1.0, "config_hash": "a" * 64,
        "sessions": [{"session_id": "session-00", "status": "available", "usable": True,
                      "auth_status": "succeeded", "connect_tls_status": "succeeded",
                      "error_class": None, "http_status": 200, "latency_ms": 1.0}],
    }

    with pytest.raises(ValueError, match="session ids"):
        module._validated_capacity_fact(fact)


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
        "slot_budget": 3,
        "slot_capacity": None,
        "capacity_gate_status": "denied",
        "capacity_gate_reason": "credentials_missing",
        "credential_generation": "test-generation-1",
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


def test_run_operation_binds_one_specific_capacity_reservation_snapshot():
    module = load_module()
    connection = Connection()
    authorization = {
        "status": "active",
        "reason": "capacity_reserved",
        "reservation_id": "reservation-1",
        "owner_id": "worker-a",
        "canary_operation_id": "op-canary-1",
        "capacity_config_hash": "a" * 64,
        "credential_generation": "test-generation-1",
        "requested_capacity": 3,
        "required_slots": 1,
        "reserved_slots": 3,
        "slot_ids": ["session-01", "session-02", "session-03"],
        "fact_finished_at": "2026-09-03T01:00:00+00:00",
        "fact_expires_at": "2026-09-03T02:00:00+00:00",
        "reservation_expires_at": "2026-09-03T01:10:00+00:00",
        "capacity_snapshot": {"unique_egress_count": 3, "slot_capacity": 9},
    }

    module.bind_capacity_authorization(
        "op-run-1", "tenant-a", authorization, connect=lambda: connection
    )

    sql, params = connection.cursor_instance.executed[0]
    assert "authorizing_canary_operation_id" in sql
    assert "capacity_reservation_id" in sql
    assert "capacity_authorization_json" in sql
    assert "op-canary-1" in params and "reservation-1" in params
    rendered = repr(params)
    assert "proxy.example" not in rendered and "203.0.113" not in rendered


def test_operation_ledger_rejects_unknown_denied_fact_with_success_counts():
    module = load_module()
    fact = {
        "schema_version": "amazon-us-proxy-canary-v1",
        "canary_status": "unknown",
        "planned_slots": 1,
        "tested_slots": 1,
        "available_slots": 1,
        "unique_egress_count": 1,
        "duplicate_egress_count": 0,
        "requested_capacity": 1,
        "required_slots": 1,
        "slot_budget": 1,
        "slot_capacity": 1,
        "capacity_gate_status": "denied",
        "capacity_gate_reason": "credentials_missing",
        "credential_generation": "test-generation-1",
        "p95_latency_ms": 10.0,
        "config_hash": "a" * 64,
        "sessions": [{
            "session_id": "session-01", "status": "available", "usable": True,
            "auth_status": "succeeded", "connect_tls_status": "succeeded",
            "error_class": None, "http_status": 200, "latency_ms": 10.0,
        }],
    }

    with pytest.raises(ValueError, match="unknown capacity fact"):
        module.finish_operation(
            "op-canary-1", "tenant-a", "failed", "capacity_gate", "credentials_missing",
            capacity_fact=fact, connect=lambda: Connection(),
        )
