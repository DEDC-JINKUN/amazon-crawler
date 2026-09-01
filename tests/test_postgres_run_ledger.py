from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "postgres_run_ledger.py"


def load_module():
    spec = importlib.util.spec_from_file_location("postgres_run_ledger_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Cursor:
    def __init__(self):
        self.executed = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params)))


class Connection:
    def __init__(self):
        self.cursor_instance = Cursor()
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1


def test_run_ledger_schema_is_idempotent_and_postgres_is_the_receipt_truth():
    module = load_module()
    connection = Connection()

    module.ensure_schema(lambda: connection)
    module.ensure_schema(lambda: connection)

    statements = "\n".join(sql for sql, _ in connection.cursor_instance.executed)
    assert "CREATE TABLE IF NOT EXISTS amazon_us.collection_run" in statements
    assert "receipt_json jsonb" in statements
    assert connection.commits == 2


def test_run_ledger_start_and_finish_are_tenant_scoped():
    module = load_module()
    connection = Connection()
    connect = lambda: connection

    module.start_run(connect, tenant_id="tenant-a", run_id="run-1", command="run", requested_actions=97,
                     worker_id="worker-1", controller_pid=123)
    module.finish_run(connect, tenant_id="tenant-a", run_id="run-1", status="interrupted",
                      controller_exit_code=130, worker_exit_code=-15,
                      termination_reason="controller_exited", receipt={"status": "interrupted"})

    statements = connection.cursor_instance.executed
    assert any("INSERT INTO amazon_us.collection_run" in sql and "tenant-a" in params for sql, params in statements)
    assert any("UPDATE amazon_us.collection_run" in sql and "tenant-a" in params and "run-1" in params
               for sql, params in statements)
    assert connection.commits == 2


def test_run_ledger_finish_fails_closed_when_run_is_missing():
    module = load_module()
    connection = Connection()
    connection.cursor_instance.rowcount = 0

    try:
        module.finish_run(
            lambda: connection, tenant_id="tenant-a", run_id="missing", status="interrupted",
            controller_exit_code=130, worker_exit_code=-15, termination_reason="controller_exited",
            receipt={"status": "interrupted"},
        )
    except RuntimeError as exc:
        assert "did not match" in str(exc)
    else:
        raise AssertionError("missing PostgreSQL run must fail closed")
    assert connection.commits == 0
