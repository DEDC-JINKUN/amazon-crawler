from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
DSN = os.environ.get("AMAZON_TEST_POSTGRES_DSN", "")

from product_watch import ChangeEvent, Observation, ProductWatchRepository


@pytest.mark.skipif(not DSN, reason="AMAZON_TEST_POSTGRES_DSN is not configured")
def test_additive_migration_persistence_idempotency_and_restart_consistency():
    import psycopg

    tenant = f"product-watch-{uuid.uuid4().hex}"
    with psycopg.connect(DSN) as connection:
        connection.execute((ROOT / "schema" / "postgres_schema.sql").read_text(encoding="utf-8"))
        migration = (ROOT / "schema" / "migrations" / "20260905_product_watch.sql").read_text(encoding="utf-8")
        connection.execute(migration)
        connection.execute(migration)
        connection.commit()
    item = Observation(tenant_id=tenant, marketplace="US", subject_type="own", requested_asin="B000000000", observed_asin="B000000000", kind="price", observed_at=datetime.now(timezone.utc), source_evidence_ids=("fixture-evidence",), value={"amount": "9.99", "currency": "USD"}, context={"currency": "USD"})
    assert ProductWatchRepository(DSN).persist_observations([item]) == 1
    assert ProductWatchRepository(DSN).persist_observations([item]) == 0
    identity = {"tenant_id": tenant, "marketplace": "US", "subject_type": "own", "requested_asin": "B000000000", "observed_asin": "B000000000"}
    loaded = ProductWatchRepository(DSN).load_latest_effective(identity=identity, kind="price")
    assert loaded is not None and loaded.observation_id == item.observation_id and loaded.value["amount"] == "9.99"
    event = ChangeEvent(event_type="price_baseline", watch_id="watch-integration", before_observation_id=None, after_observation_id=item.observation_id or "", old_value=None, new_value=item.value, delta={}, comparison_context=item.comparison_identity(), observed_window={"after": item.observed_at.isoformat()}, quality="valid", evidence_ids=item.source_evidence_ids, reason="integration fixture")
    assert ProductWatchRepository(DSN).persist_events([event]) == 1
    assert ProductWatchRepository(DSN).persist_events([event]) == 0
    assert ProductWatchRepository(DSN).load_event_summary(tenant_id=tenant, marketplace="US")[0]["count"] == 1
    natural = Observation(tenant_id=tenant, marketplace="US", subject_type="own", requested_asin="B000000000", observed_asin="B000000000", kind="keyword_rank", observed_at=datetime.now(timezone.utc), source_evidence_ids=("keyword-natural",), value={"organic_position": 2, "ad_position": None}, context={"keyword": "ornament"})
    ad_only = Observation(tenant_id=tenant, marketplace="US", subject_type="own", requested_asin="B000000000", observed_asin="B000000000", kind="keyword_rank", observed_at=natural.observed_at + timedelta(seconds=1), source_evidence_ids=("keyword-ad",), value={"organic_position": None, "ad_position": 1}, context={"keyword": "ornament"})
    repository = ProductWatchRepository(DSN)
    assert repository.persist_observations([natural, ad_only]) == 2
    keyword_identity = {**identity}
    keyword_context = {"keyword": "ornament"}
    latest_natural = ProductWatchRepository(DSN).load_latest_effective(identity=keyword_identity, kind="keyword_rank", context=keyword_context)
    latest_ad = ProductWatchRepository(DSN).load_latest_effective(identity=keyword_identity, kind="keyword_rank", context=keyword_context, rank_field="ad")
    state = ProductWatchRepository(DSN).load_latest_state(identity=keyword_identity, kind="keyword_rank", context=keyword_context)
    assert latest_natural is not None and latest_natural.observation_id == natural.observation_id
    assert latest_ad is not None and latest_ad.observation_id == ad_only.observation_id
    assert state["latest_attempt"].observation_id == ad_only.observation_id and state["stale"] is True
