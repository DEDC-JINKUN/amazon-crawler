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
from product_watch.delivery import LocalReceiptChannel
from product_watch.delivery_repository import WatchDeliveryRepository


@pytest.mark.skipif(not DSN, reason="AMAZON_TEST_POSTGRES_DSN is not configured")
def test_watch_outbox_recovers_configuration_crash_and_tenant_boundaries(tmp_path: Path):
    import psycopg
    with psycopg.connect(DSN) as connection:
        for name in ("schema/postgres_schema.sql", "schema/migrations/20260905_product_watch.sql", "schema/migrations/20260905_search_rank.sql", "schema/migrations/20260905_watch_delivery.sql"):
            sql = (ROOT / name).read_text(encoding="utf-8")
            connection.execute(sql); connection.execute(sql)
        connection.commit()
    tenant = f"d-watch-{uuid.uuid4().hex}"
    before = Observation(tenant_id=tenant, marketplace="US", subject_type="competitor", requested_asin="B000000001", observed_asin="B000000001", kind="price", observed_at=datetime.now(timezone.utc), source_evidence_ids=("fixture-before",), value={"amount": "10", "currency": "USD"}, context={"currency": "USD"})
    after = Observation(tenant_id=tenant, marketplace="US", subject_type="competitor", requested_asin="B000000001", observed_asin="B000000001", kind="price", observed_at=before.observed_at + timedelta(minutes=1), source_evidence_ids=("fixture-after",), value={"amount": "12", "currency": "USD"}, context={"currency": "USD"})
    watch = ProductWatchRepository(DSN)
    assert watch.persist_observations([before, after]) == 2
    event = ChangeEvent(event_type="price_increase", watch_id="fixture", before_observation_id=before.observation_id, after_observation_id=after.observation_id or "", old_value=before.value, new_value=after.value, delta={"amount": "2"}, comparison_context=after.comparison_identity(), observed_window={"before": before.observed_at.isoformat(), "after": after.observed_at.isoformat()}, quality="valid", evidence_ids=("fixture-before", "fixture-after"), severity="important")
    assert watch.persist_events([event]) == 1
    delivery = WatchDeliveryRepository(DSN)
    # No configured outlet creates a reviewable pending record, never an
    # ambiguous unknown send. A later actual target receives a fresh outbox row.
    assert delivery.sync_event_reports(tenant_id=tenant, marketplace="US", config={"config_version": "v1"}) == 1
    first = delivery.list_deliveries(tenant_id=tenant)[0]
    assert first["channel"] == "local_review" and first["status"] == "pending"
    assert delivery.process_pending(tenant_id=tenant, channel=LocalReceiptChannel(tmp_path))["claimed"] == 0
    assert delivery.sync_event_reports(tenant_id=tenant, marketplace="US", config={"config_version": "v2", "channel": "local_receipt", "recipient": "approved-test"}) == 1
    sent = delivery.process_pending(tenant_id=tenant, channel=LocalReceiptChannel(tmp_path))
    assert sent["sent"] == 1
    claimed = delivery.claim_pending(tenant_id=tenant)
    assert claimed == []
    with psycopg.connect(DSN) as connection:
        connection.execute("UPDATE amazon_us.watch_delivery SET status='sending',claimed_at=CURRENT_TIMESTAMP - INTERVAL '16 minutes' WHERE tenant_id=%s AND channel='local_receipt'", (tenant,)); connection.commit()
    assert delivery.reclaim_abandoned_sending(tenant_id=tenant) == 1
    assert any(item["status"] == "unknown" for item in delivery.list_deliveries(tenant_id=tenant))
    report_id = delivery.list_events(tenant_id=tenant, marketplace="US")[0]["report_id"]
    assert delivery.load_report(report_id, tenant_id="different-tenant") is None
    assert delivery.acknowledge_event(tenant_id="different-tenant", event_id=event.event_id or "", actor="watch-manager") is False
