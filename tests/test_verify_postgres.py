from __future__ import annotations

import importlib.util
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_postgres.py"


def load_module():
    spec = importlib.util.spec_from_file_location("verify_postgres_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_schema_contract_requires_retry_context_and_transfer_columns():
    module = load_module()
    item_columns = ["lease_expires_at", "lease_owner", "lease_token", "next_retry_at"]
    assert module.schema_ok({"item_state": item_columns, "collection_evidence": ["context_json", "transfer_bytes"]})
    assert not module.schema_ok({"item_state": ["next_retry_at"], "collection_evidence": ["context_json"]})


def test_main_reads_dsn_from_named_environment_variable(capsys):
    module = load_module()

    class Repository:
        def __init__(self, dsn, tenant_id="default"):
            assert dsn == "postgresql://from-env"

        def load_schema_contract(self):
            return {
                "item_state": ["lease_expires_at", "lease_owner", "lease_token", "next_retry_at"],
                "collection_evidence": ["context_json", "transfer_bytes"],
            }

        def load_job_status(self):
            return {"counts": {}}

    with patch.object(module, "PostgresCollectionRepository", Repository), patch.dict(
        "os.environ", {"TEST_POSTGRES_DSN": "postgresql://from-env"}, clear=True
    ):
        assert module.main(["--dsn-env", "TEST_POSTGRES_DSN", "--tenant-id", "tenant-a"]) == 0
    assert "postgresql://from-env" not in capsys.readouterr().out


def test_main_redacts_unexpected_database_errors(capsys):
    module = load_module()

    class BrokenRepository:
        def __init__(self, dsn, tenant_id="default"):
            pass

        def load_schema_contract(self):
            raise Exception("password=secret should not be printed")

    with patch.object(module, "PostgresCollectionRepository", BrokenRepository):
        assert module.main(["--dsn", "postgresql://example.invalid/db"]) == 1
    output = capsys.readouterr().out
    assert "PostgreSQL verification failed (Exception)" in output
    assert "secret" not in output
