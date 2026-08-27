from __future__ import annotations

import importlib.util
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
