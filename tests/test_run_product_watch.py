from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location("run_product_watch_test", ROOT / "scripts" / "run_product_watch.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_enabled_product_schedule_uses_existing_min_age_contract():
    calls = {}
    class Storage:
        def __init__(self, dsn, *, tenant_id, subject_type): calls.update(dsn=dsn, tenant_id=tenant_id, subject_type=subject_type)
        def enqueue_due_refreshes(self, *, min_age_hours, limit, asins): calls.update(min_age_hours=min_age_hours, limit=limit, asins=asins); return 3
    result = load_module().enqueue_due_products(dsn="postgresql://fixture", tenant_id="tenant-a", config={"enabled": True, "subject_type": "competitor", "asins": ["B000000001"], "max_age_hours": 12, "limit": 4}, storage_factory=Storage)
    assert result == 3
    assert calls == {"dsn": "postgresql://fixture", "tenant_id": "tenant-a", "subject_type": "competitor", "min_age_hours": 12, "limit": 1, "asins": ["B000000001"]}


def test_enabled_product_schedule_requires_a_small_explicit_asin_scope():
    import pytest
    with pytest.raises(ValueError, match="approved ASIN"):
        load_module().enqueue_due_products(dsn="postgresql://fixture", tenant_id="tenant-a", config={"enabled": True}, storage_factory=object)
