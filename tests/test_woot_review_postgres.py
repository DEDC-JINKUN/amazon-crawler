from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from test_console_woot_reviews import build_aggregate


SCRIPT = ROOT / "scripts" / "import_woot_reviews.py"
SPEC = importlib.util.spec_from_file_location("import_woot_reviews_test", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["import_woot_reviews_test"] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeDatabase:
    def __init__(self, existing_hash=None, identity=("postgres", "amazon-reviews-schema-v1")):
        self.import_hash = existing_hash
        self.identity = identity
        self.executed = []
        self.many = []
        self.closed = 0

    def connect(self, _dsn):
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, database):
        self.database = database

    def transaction(self):
        return nullcontext()

    def cursor(self):
        return FakeCursor(self.database)

    def close(self):
        self.database.closed += 1


class FakeCursor:
    def __init__(self, database):
        self.database = database
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        assert sql.count("%s") == len(params), (sql, params)
        normalized = " ".join(sql.split())
        self.database.executed.append((normalized, params))
        if normalized.startswith("SELECT current_database()"):
            self.row = self.database.identity
        elif normalized.startswith("SELECT manifest_sha256"):
            self.row = None if self.database.import_hash is None else (self.database.import_hash,)
        elif "INSERT INTO amazon_reviews.review_import_batch" in normalized:
            self.database.import_hash = params[7]
            self.row = None
        else:
            self.row = None
        return self

    def fetchone(self):
        return self.row

    def executemany(self, sql, rows):
        values = list(rows)
        assert all(sql.count("%s") == len(row) for row in values), (sql, values[:1])
        self.database.many.append((" ".join(sql.split()), values))
        return self


def test_migration_is_independent_and_protects_immutable_evidence():
    sql = (ROOT / "schema" / "migrations" / "20260914_woot_reviews.sql").read_text(encoding="utf-8")
    assert "CREATE SCHEMA IF NOT EXISTS amazon_reviews" in sql
    for table in (
        "review_import_batch", "review_collection_run", "review_job_state",
        "review_query_checkpoint", "review_raw_evidence", "review_record", "review_observation",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "review_raw_evidence_append_only" in sql
    assert "review_observation_append_only" in sql
    assert "review_import_batch_append_only" in sql
    assert "amazon_us.item_state" not in sql
    assert "amazon_us.product_snapshot" not in sql


def test_validate_plan_uses_verified_aggregate_without_database(tmp_path):
    root = build_aggregate(tmp_path / "aggregate")
    plan = MODULE.build_import_plan(root, tenant_id="tenant-a", import_batch_id="batch-1")
    assert plan["asin_count"] == 2
    assert plan["review_count"] == 3
    assert plan["database_writes_performed"] == 0
    assert plan["pdp_truth_written"] is False
    assert len(plan["evidence_files"]) == 4


def test_import_is_atomic_counted_and_same_hash_is_idempotent(tmp_path):
    root = build_aggregate(tmp_path / "aggregate")
    database = FakeDatabase()
    first = MODULE.import_aggregate(
        root, tenant_id="tenant-a", import_batch_id="batch-1",
        connection_factory=database.connect,
    )
    assert first.idempotent_replay is False
    assert first.asin_rows == 2
    assert first.review_rows == 3
    assert first.observation_rows == 3
    assert first.evidence_rows == 4
    sql_text = "\n".join(sql for sql, _params in database.executed)
    many_text = "\n".join(sql for sql, _rows in database.many)
    assert "amazon_reviews.review_collection_run" in sql_text
    assert "amazon_reviews.review_import_batch" in sql_text
    assert "amazon_reviews.review_job_state" in many_text
    assert "amazon_reviews.review_record" in many_text
    assert "amazon_reviews.review_observation" in many_text
    assert "amazon_us.item_state" not in sql_text + many_text
    job_rows = next(rows for sql, rows in database.many if "review_job_state" in sql)
    assert {row[3] for row in job_rows} == {"source_limited"}
    run_insert = next(params for sql, params in database.executed if "review_collection_run" in sql)
    assert run_insert[3] == "partial_success"
    many_before = len(database.many)
    replay = MODULE.import_aggregate(
        root, tenant_id="tenant-a", import_batch_id="batch-1",
        connection_factory=database.connect,
    )
    assert replay.idempotent_replay is True
    assert replay.review_rows == 0
    assert len(database.many) == many_before
    assert database.closed == 2


def test_import_batch_same_id_with_different_hash_fails_closed(tmp_path):
    root = build_aggregate(tmp_path / "aggregate")
    database = FakeDatabase(existing_hash="b" * 64)
    with pytest.raises(MODULE.ReviewImportConflictError, match="different manifest hash"):
        MODULE.import_aggregate(
            root, tenant_id="tenant-a", import_batch_id="batch-1",
            connection_factory=database.connect,
        )


def test_import_refuses_wrong_database_identity_before_writes(tmp_path):
    root = build_aggregate(tmp_path / "aggregate")
    database = FakeDatabase(identity=("wrong", "amazon-reviews-schema-v1"))
    with pytest.raises(RuntimeError, match="identity mismatch"):
        MODULE.import_aggregate(
            root,
            tenant_id="tenant-a",
            import_batch_id="batch-1",
            connection_factory=database.connect,
        )
    assert database.many == []
    assert len(database.executed) == 1


def test_validate_only_cli_does_not_require_dsn(tmp_path, monkeypatch, capsys):
    root = build_aggregate(tmp_path / "aggregate")
    monkeypatch.delenv("AMAZON_US_POSTGRES_DSN", raising=False)
    result = MODULE.main([
        str(root), "--tenant-id", "tenant-a", "--import-batch-id", "batch-1", "--validate-only",
    ])
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["review_count"] == 3
