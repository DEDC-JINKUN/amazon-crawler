from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
from pathlib import Path
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("AMAZON_TEST_POSTGRES_DSN", "")


def load_storage():
    spec = importlib.util.spec_from_file_location(
        "postgres_worker_storage_integration", ROOT / "scripts" / "postgres_worker_storage.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_console():
    spec = importlib.util.spec_from_file_location(
        "collection_console_integration", ROOT / "scripts" / "collection_console.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not DSN, reason="AMAZON_TEST_POSTGRES_DSN is not configured")
def test_two_workers_claim_distinct_tasks_from_real_postgres():
    import psycopg

    storage = load_storage()
    tenant_id = f"test-{uuid.uuid4().hex}"
    schema = (ROOT / "schema" / "postgres_schema.sql").read_text(encoding="utf-8")
    rows = [
        {"asin": "B00RCPDCQU", "url": "https://www.amazon.com/dp/B00RCPDCQU", "marketplace": "US"},
        {"asin": "B00RCPDI50", "url": "https://www.amazon.com/dp/B00RCPDI50", "marketplace": "US"},
    ]

    with psycopg.connect(DSN) as connection:
        connection.execute(schema)
        connection.commit()

    repository = storage.PostgresWorkerStorage(DSN, tenant_id=tenant_id, subject_type="own")
    try:
        assert repository.initialize_manifest(rows) == 2
        with psycopg.connect(DSN) as connection:
            connection.execute(
                "UPDATE amazon_us.item_state SET status='reviews_pending',task_stage='reviews',"
                "next_review_url=%s,next_review_page=1,reported_review_count=1 WHERE tenant_id=%s AND asin=%s",
                ("https://www.amazon.com/product-reviews/B00RCPDI50", tenant_id, "B00RCPDI50"),
            )
            connection.execute(
                "UPDATE amazon_us.item_state SET last_error='old_error',block_reason='captcha',"
                "next_retry_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE tenant_id=%s AND asin='B00RCPDCQU'",
                (tenant_id,),
            )
            connection.commit()
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda worker_id: repository.claim_task(worker_id), ("worker-a", "worker-b")))

        assert all(task is not None for task in claimed)
        assert {task["asin"] for task in claimed} == {"B00RCPDCQU", "B00RCPDI50"}
        assert len({task["lease_token"] for task in claimed}) == 2

        first = next(task for task in claimed if task["asin"] == "B00RCPDCQU")
        assert repository.save_product_result(
            task=first,
            evidence={
                "run_id": "integration-run", "url": first["url"], "http_status": 200,
                "source_type": "http_html", "content_hash": "b" * 64, "context_json": {"postal_code": "90001"},
            },
            product={
                "canonical_url": first["url"], "title": "Integration product", "bullets": ["one"],
                "specs": {"color": "black"}, "buy_box": {}, "top_reviews": [],
                "aplus_present": False, "status": "product_done",
            },
            media=[{"placement": "gallery", "unique_key": f"US|{first['asin']}|gallery|1", "is_primary": True}],
            content_modules=[{"module_type": "bullet", "position": 1, "text": "one", "unique_key": f"US|{first['asin']}|bullet|1"}],
            review_summary={"status": "not_available"},
            next_status="succeeded",
            state_fields={"task_stage": "complete", "next_review_url": None, "next_review_page": None},
            reason="integration_success",
        ) is True
        with psycopg.connect(DSN) as connection:
            state = connection.execute(
                "SELECT status,lease_token,last_error,block_reason,next_retry_at FROM amazon_us.item_state "
                "WHERE tenant_id=%s AND asin=%s",
                (tenant_id, first["asin"]),
            ).fetchone()
            assert state == ("succeeded", None, None, None, None)
            assert connection.execute(
                "SELECT title FROM amazon_us.product_latest WHERE tenant_id=%s AND asin=%s",
                (tenant_id, first["asin"]),
            ).fetchone() == ("Integration product",)
            assert connection.execute(
                "SELECT count(*) FROM amazon_us.collection_evidence WHERE tenant_id=%s AND asin=%s",
                (tenant_id, first["asin"]),
            ).fetchone() == (1,)

        review_task = next(task for task in claimed if task["asin"] == "B00RCPDI50")
        assert repository.save_review_result(
            task=review_task,
            evidence={"run_id": "integration-run", "url": review_task["next_review_url"], "http_status": 200, "content_hash": "c" * 64, "context_json": {}},
            page={"page": 1, "url": review_task["next_review_url"], "status": "fetched", "next_url": None},
            records=[{"review_id": "R1", "title": "Good", "body": "Works", "review_images": [], "page": 1, "unique_key": "US|B00RCPDI50|R1"}],
            summary={"reported_review_count": 1, "fetched_count": 1, "pages_fetched": 1, "next_page": None, "status": "exhausted"},
            next_status="succeeded",
            state_fields={"task_stage": "complete", "next_review_url": None, "next_review_page": None, "fetched_review_count": 1, "review_pages_fetched": 1},
            reason="reviews_exhausted",
        ) is True
        with psycopg.connect(DSN) as connection:
            assert connection.execute(
                "SELECT status FROM amazon_us.item_state WHERE tenant_id=%s AND asin='B00RCPDI50'",
                (tenant_id,),
            ).fetchone() == ("succeeded",)
            assert connection.execute(
                "SELECT count(*) FROM amazon_us.review_record WHERE tenant_id=%s AND asin='B00RCPDI50'",
                (tenant_id,),
            ).fetchone() == (1,)

            connection.execute(
                "INSERT INTO amazon_us.refresh_request(job_id,tenant_id,marketplace,asin,subject_type,requested_by,reason,status) "
                "VALUES ('job-integration',%s,'US','B00RCPDCQU','own','test','refresh','queued')",
                (tenant_id,),
            )
            connection.commit()
        refreshed = repository.claim_refresh_task("worker-refresh", lease_seconds=120)
        assert refreshed["job_id"] == "job-integration"
        assert refreshed["asin"] == "B00RCPDCQU"
        assert refreshed["lease_owner"] == "worker-refresh"
        assert repository.update_task(
            refreshed["asin"], refreshed["lease_token"], refreshed["lease_owner"], "succeeded",
            reason="refresh_integration_complete",
        ) is True
        repository.finish_refresh_request("job-integration", "completed")
        with psycopg.connect(DSN) as connection:
            assert connection.execute(
                "SELECT status FROM amazon_us.refresh_request WHERE tenant_id=%s AND job_id='job-integration'",
                (tenant_id,),
            ).fetchone() == ("completed",)
            connection.execute(
                "UPDATE amazon_us.product_snapshot SET collected_at=CURRENT_TIMESTAMP-INTERVAL '2 hours' "
                "WHERE tenant_id=%s AND asin='B00RCPDCQU'",
                (tenant_id,),
            )
            connection.execute(
                "INSERT INTO amazon_us.refresh_request(job_id,tenant_id,marketplace,asin,subject_type,requested_by,reason,status) "
                "VALUES ('job-orphan',%s,'US','B00RCPDI50','own','test','orphan','queued')",
                (tenant_id,),
            )
            connection.commit()
        orphan = repository.claim_refresh_task("worker-crash", lease_seconds=120)
        assert orphan["job_id"] == "job-orphan"
        with psycopg.connect(DSN) as connection:
            connection.execute(
                "UPDATE amazon_us.item_state SET lease_expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' "
                "WHERE tenant_id=%s AND asin='B00RCPDI50'",
                (tenant_id,),
            )
            connection.commit()
        assert repository.reclaim_expired_leases() == 1
        with psycopg.connect(DSN) as connection:
            assert connection.execute(
                "SELECT status FROM amazon_us.refresh_request WHERE tenant_id=%s AND job_id='job-orphan'",
                (tenant_id,),
            ).fetchone() == ("queued",)
        assert repository.enqueue_due_refreshes(min_age_hours=1, limit=10) == 1
        assert repository.enqueue_due_refreshes(min_age_hours=1, limit=10) == 0

        console = load_console().PostgresConsoleRepository(DSN, tenant_id)
        assert console.load_identity() == {"tenant_id": tenant_id, "task_count": 2}
        overview = console.load_overview()
        assert overview["progress"]["total"] == 2
        assert overview["progress"]["successful_products"] == 1
        assert overview["table_counts"]["media"] == 1
        listing = console.list_items(limit=10)
        assert listing["total"] == 2
        assert {item["asin"] for item in listing["items"]} == {"B00RCPDCQU", "B00RCPDI50"}
        detail = console.load_detail("B00RCPDCQU")
        assert detail["product"]["title"] == "Integration product"
        assert len(detail["media"]) == 1
        assert len(detail["content_modules"]) == 1
        assert detail["evidence"][0]["run_id"] == "integration-run"
        runs = console.list_runs()
        assert runs[0]["run_id"] == "integration-run"
        assert runs[0]["evidence_actions"] == 2
        run_detail = console.load_run("integration-run")
        assert run_detail["recorded_actions"] == 2
        assert run_detail["inferred_actions"] == 0
        assert {item["asin"] for item in run_detail["items"]} == {"B00RCPDCQU", "B00RCPDI50"}
    finally:
        with psycopg.connect(DSN) as connection:
            for table in (
                "state_history", "review_page_state", "refresh_request", "collection_evidence", "media_asset", "content_module",
                "review_summary", "review_record", "product_snapshot", "item_state", "asin_master",
            ):
                connection.execute(f"DELETE FROM amazon_us.{table} WHERE tenant_id=%s", (tenant_id,))
            connection.commit()
