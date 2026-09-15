from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
DSN = os.environ.get("AMAZON_REVIEW_TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="fresh isolated review PostgreSQL required")


def test_review_migration_is_idempotent_and_concurrent_workers_stay_in_review_schema():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    from postgres_review_storage import PostgresReviewStorage

    info = conninfo_to_dict(DSN)
    assert info.get("host") == "127.0.0.1"
    assert info.get("dbname") == "amazon_reviews_goal1"
    migration = (ROOT / "schema" / "migrations" / "20260914_woot_reviews.sql").read_text(encoding="utf-8")
    with psycopg.connect(DSN) as conn:
        assert conn.execute("SELECT to_regnamespace('amazon_us')").fetchone()[0] is None
        conn.execute(migration)
        conn.execute(migration)
        assert conn.execute(
            "SELECT obj_description('amazon_reviews'::regnamespace,'pg_namespace')"
        ).fetchone()[0] == "amazon-reviews-schema-v1"

    tenant = "goal1-review-" + uuid.uuid4().hex
    connect = lambda: psycopg.connect(DSN)
    storage = PostgresReviewStorage(
        connect, tenant, expected_database="amazon_reviews_goal1",
    )
    assert storage.seed_jobs(["B0REV00001", "B0REV00002"]) == 2

    def claim(index: int):
        return PostgresReviewStorage(
            connect, tenant, expected_database="amazon_reviews_goal1",
        ).claim_job(f"review-worker-{index}", lease_seconds=120)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(claim, range(2)))
    assert all(claimed)
    assert {task["asin"] for task in claimed} == {"B0REV00001", "B0REV00002"}
    assert len({task["lease_token"] for task in claimed}) == 2

    blocked = storage.save_failure(claimed[0], error_code="provider_blocked")
    retryable = storage.save_failure(
        claimed[1], error_code="network_error", retry_after_seconds=60,
    )
    assert blocked["status"] == "blocked"
    assert retryable["status"] == "retryable"

    with psycopg.connect(DSN) as conn:
        states = dict(conn.execute(
            "SELECT asin,status FROM amazon_reviews.review_job_state WHERE tenant_id=%s",
            (tenant,),
        ).fetchall())
        assert states == {claimed[0]["asin"]: "blocked", claimed[1]["asin"]: "retryable"}
        assert conn.execute("SELECT to_regnamespace('amazon_us')").fetchone()[0] is None
