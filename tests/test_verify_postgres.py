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
    assert module.schema_ok({"item_state": ["next_retry_at"], "collection_evidence": ["context_json", "transfer_bytes"]})
    assert not module.schema_ok({"item_state": ["next_retry_at"], "collection_evidence": ["context_json"]})


def test_main_redacts_unexpected_database_errors(capsys):
    module = load_module()

    class BrokenRepository:
        def __init__(self, dsn):
            pass

        def load_schema_contract(self):
            raise Exception("password=secret should not be printed")

    with patch.object(module, "PostgresCollectionRepository", BrokenRepository):
        assert module.main(["--dsn", "postgresql://example.invalid/db"]) == 1
    output = capsys.readouterr().out
    assert "PostgreSQL verification failed (Exception)" in output
    assert "secret" not in output
