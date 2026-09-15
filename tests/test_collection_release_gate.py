from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
DSN = os.environ.get("AMAZON_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="isolated PostgreSQL required")


def _prepare_schema() -> None:
    import psycopg

    with psycopg.connect(DSN) as conn:
        conn.execute((ROOT / "schema" / "postgres_schema.sql").read_text(encoding="utf-8"))
        conn.execute((ROOT / "schema" / "migrations" / "20260904_recovery.sql").read_text(encoding="utf-8"))
        conn.execute((ROOT / "schema" / "migrations" / "20260906_campaign.sql").read_text(encoding="utf-8"))
        migration = (ROOT / "schema" / "migrations" / "20260915_release_gate.sql").read_text(encoding="utf-8")
        conn.execute(migration)
        conn.execute(migration)


def _campaign(tenant: str, count: int = 21):
    from collection_storage import PostgresCollectionRepository
    from postgres_worker_storage import PostgresWorkerStorage

    asins = [f"B0G{index:07d}" for index in range(count)]
    storage = PostgresWorkerStorage(DSN, tenant)
    storage.initialize_manifest([
        {"asin": asin, "url": f"https://www.amazon.com/dp/{asin}", "source_type": "release-gate-fixture"}
        for asin in asins
    ])
    repository = PostgresCollectionRepository(DSN, tenant_id=tenant)
    campaign = repository.submit_campaign(
        asins=asins,
        idempotency_key="release-gate-original-campaign",
        requested_by="release-gate-test",
        max_members=100,
        deadline_seconds=3600,
    )
    return asins, campaign


def test_release_gate_migration_and_start_freeze_exactly_twenty_existing_members():
    import psycopg
    from collection_release_gate import CollectionReleaseGateStore

    _prepare_schema()
    tenant = "release-gate-" + uuid.uuid4().hex
    asins, campaign = _campaign(tenant)
    store = CollectionReleaseGateStore(DSN, tenant_id=tenant)

    first = store.start(
        campaign_id=campaign["campaign_id"],
        gate_id="gate-fixed-20",
        idempotency_key="gate-start-fixed-20",
        release_id="candidate-fc1a45f",
        selection_limit=20,
        created_by="release-admin",
    )
    duplicate = store.start(
        campaign_id=campaign["campaign_id"],
        gate_id="gate-fixed-20",
        idempotency_key="gate-start-fixed-20",
        release_id="candidate-fc1a45f",
        selection_limit=20,
        created_by="release-admin",
    )

    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    assert first["campaign_id"] == campaign["campaign_id"]
    assert first["selected_count"] == 20
    assert first["selection_hash"] == duplicate["selection_hash"]
    assert [item["asin"] for item in first["members"]] == asins[:20]
    assert [item["release_gate_ordinal"] for item in first["members"]] == list(range(20))

    with psycopg.connect(DSN) as conn:
        parent = conn.execute(
            "SELECT idempotency_key,release_gate_id FROM amazon_us.collection_campaign "
            "WHERE tenant_id=%s AND campaign_id=%s",
            (tenant, campaign["campaign_id"]),
        ).fetchone()
        assert parent == ("release-gate-original-campaign", "gate-fixed-20")
        assert conn.execute(
            "SELECT count(*) FROM amazon_us.collection_campaign_member "
            "WHERE tenant_id=%s AND campaign_id=%s AND release_gate_id IS NOT NULL",
            (tenant, campaign["campaign_id"]),
        ).fetchone()[0] == 20
        assert conn.execute(
            "SELECT count(*) FROM amazon_us.collection_campaign WHERE tenant_id=%s",
            (tenant,),
        ).fetchone()[0] == 1


def test_gate_scoped_worker_claims_only_frozen_members_and_scope_survives_restart():
    import psycopg
    from collection_release_gate import CollectionReleaseGateStore
    from postgres_worker_storage import PostgresWorkerStorage

    _prepare_schema()
    tenant = "release-claim-" + uuid.uuid4().hex
    asins, campaign = _campaign(tenant)
    gate_id = "gate-claim-fixed-20"
    CollectionReleaseGateStore(DSN, tenant_id=tenant).start(
        campaign_id=campaign["campaign_id"],
        gate_id=gate_id,
        idempotency_key="gate-claim-start-fixed-20",
        release_id="candidate-fc1a45f",
        selection_limit=20,
        created_by="release-admin",
    )

    ordinary = PostgresWorkerStorage(DSN, tenant)
    ordinary.configure_recovery({})
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE amazon_us.refresh_request SET reason='direct:attempted-bypass' "
            "WHERE tenant_id=%s AND asin=%s",
            (tenant, asins[20]),
        )
    assert ordinary.count_pending_refresh_tasks(3) == 0
    assert ordinary.claim_refresh_task("ordinary-worker") is None
    assert PostgresWorkerStorage(DSN, tenant).claim_task("ordinary-fallback-worker") is None

    claimed = []
    for index in range(20):
        gated = PostgresWorkerStorage(DSN, tenant, release_gate_id=gate_id)
        gated.configure_recovery({})
        task = gated.claim_refresh_task(f"gate-worker-{index}", lease_seconds=120)
        assert task is not None
        claimed.append(task["asin"])
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "UPDATE amazon_us.refresh_request SET status='completed',completed_at=CURRENT_TIMESTAMP "
                "WHERE tenant_id=%s AND job_id=%s",
                (tenant, task["job_id"]),
            )
            conn.execute(
                "UPDATE amazon_us.item_state SET status='succeeded',lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL "
                "WHERE tenant_id=%s AND asin=%s AND subject_type='own'",
                (tenant, task["asin"]),
            )
            conn.execute(
                "UPDATE amazon_us.recovery_job SET terminal=true,outcome='completed',lease_token=NULL "
                "WHERE tenant_id=%s AND asin=%s AND subject_type='own' AND stage='product'",
                (tenant, task["asin"]),
            )

    restarted = PostgresWorkerStorage(DSN, tenant, release_gate_id=gate_id)
    restarted.configure_recovery({})
    assert claimed == asins[:20]
    assert restarted.count_pending_refresh_tasks(3) == 0
    assert restarted.claim_refresh_task("gate-worker-restarted") is None
    with psycopg.connect(DSN) as conn:
        remaining = conn.execute(
            "SELECT asin,status FROM amazon_us.refresh_request WHERE tenant_id=%s AND status='queued'",
            (tenant,),
        ).fetchall()
    assert remaining == [(asins[20], "queued")]


def test_release_gate_refuses_partial_selection_and_concurrent_start_is_idempotent():
    import psycopg
    from collection_release_gate import CollectionReleaseGateStore, ReleaseGateUnavailable

    _prepare_schema()
    short_tenant = "release-short-" + uuid.uuid4().hex
    _asins, short_campaign = _campaign(short_tenant, count=19)
    with pytest.raises(ReleaseGateUnavailable):
        CollectionReleaseGateStore(DSN, tenant_id=short_tenant).start(
            campaign_id=short_campaign["campaign_id"],
            gate_id="gate-insufficient",
            idempotency_key="gate-insufficient-fixed-20",
            release_id="candidate-fc1a45f",
            selection_limit=20,
            created_by="release-admin",
        )
    with psycopg.connect(DSN) as conn:
        assert conn.execute(
            "SELECT count(*) FROM amazon_us.collection_release_gate WHERE tenant_id=%s",
            (short_tenant,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM amazon_us.collection_campaign_member "
            "WHERE tenant_id=%s AND release_gate_id IS NOT NULL",
            (short_tenant,),
        ).fetchone()[0] == 0

    tenant = "release-concurrent-" + uuid.uuid4().hex
    _asins, campaign = _campaign(tenant)

    def start(_index):
        return CollectionReleaseGateStore(DSN, tenant_id=tenant).start(
            campaign_id=campaign["campaign_id"],
            gate_id="gate-concurrent-fixed-20",
            idempotency_key="gate-concurrent-start-fixed-20",
            release_id="candidate-fc1a45f",
            selection_limit=20,
            created_by="release-admin",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(start, range(2)))
    assert sorted(result["duplicate"] for result in results) == [False, True]
    assert len({result["selection_hash"] for result in results}) == 1
