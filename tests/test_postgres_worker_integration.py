from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
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


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not DSN, reason="AMAZON_TEST_POSTGRES_DSN is not configured")
def test_two_workers_claim_distinct_tasks_from_real_postgres():
    import psycopg
    from psycopg.rows import dict_row

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

        ledger = load_script("postgres_run_ledger")
        connect = lambda: psycopg.connect(DSN)
        ledger.start_run(
            connect, tenant_id=tenant_id, run_id="ledger-run", command="run", requested_actions=2,
            worker_id="integration-worker", controller_pid=123,
        )
        ledger.finish_run(
            connect, tenant_id=tenant_id, run_id="ledger-run", status="interrupted",
            controller_exit_code=130, worker_exit_code=-15, termination_reason="controller_exited",
            receipt={"status": "interrupted"},
        )
        ledger.start_run(
            connect, tenant_id=tenant_id, run_id="ledger-run-2", command="run", requested_actions=2,
            worker_id="integration-worker", controller_pid=123,
        )
        ledger.finish_run(
            connect, tenant_id=tenant_id, run_id="ledger-run-2", status="completed",
            controller_exit_code=0, worker_exit_code=0, termination_reason=None,
            receipt={"status": "completed", "elapsed_seconds": 150.0},
        )
        with psycopg.connect(DSN) as connection:
            connection.execute(
                "UPDATE amazon_us.collection_run SET started_at='2026-09-02T03:33:23Z',"
                "finished_at='2026-09-02T03:34:16.62Z' WHERE tenant_id=%s AND run_id='ledger-run'",
                (tenant_id,),
            )
            connection.execute(
                "UPDATE amazon_us.collection_run SET started_at='2026-09-02T03:40:34Z',"
                "finished_at='2026-09-02T03:42:53.46Z' WHERE tenant_id=%s AND run_id='ledger-run-2'",
                (tenant_id,),
            )
            connection.commit()
        ledger_detail = console.load_run("ledger-run")
        assert ledger_detail["requested_actions"] == 2
        assert ledger_detail["recorded_actions"] == 0
        assert ledger_detail["terminal_status"] == "interrupted"

        operation = load_script("operation_ledger")
        operation.ensure_schema(connect)
        before_operation = next(
            item for item in load_console().PostgresConsoleRepository(DSN).list_tenants()
            if item["tenant_id"] == tenant_id
        )
        operation.start_operation("op-integration", tenant_id, "egress", "dataimpulse-us", None, connect=connect)
        operation.finish_operation(
            "op-integration", tenant_id, "failed", "egress", "network_error",
            probe_elapsed_ms=2534.1, response_bytes=0, connect=connect,
        )
        operations = console.list_operations()
        assert operations[0]["operation_id"] == "op-integration"
        assert operations[0]["error_class"] == "network_error"
        after_operation = next(
            item for item in load_console().PostgresConsoleRepository(DSN).list_tenants()
            if item["tenant_id"] == tenant_id
        )
        assert (after_operation["requested"], after_operation["recorded"]) == (
            before_operation["requested"], before_operation["recorded"]
        )
        operation.start_operation("op-terminal", tenant_id, "run", "dataimpulse-us", "run-terminal", connect=connect)
        operation.finish_operation(
            "op-terminal", tenant_id, "interrupted", "worker", "controller_exited", connect=connect
        )
        operation.finish_operation(
            "op-terminal", tenant_id, "failed", "worker", "worker_failed", connect=connect
        )
        ledger.start_run(
            connect, tenant_id=tenant_id, run_id="run-terminal", command="run", requested_actions=1,
            worker_id="integration-worker", controller_pid=123,
        )
        ledger.finish_run(
            connect, tenant_id=tenant_id, run_id="run-terminal", status="interrupted",
            controller_exit_code=130, worker_exit_code=130, termination_reason="controller_exited",
            receipt={"status": "interrupted"},
        )
        ledger.finish_run(
            connect, tenant_id=tenant_id, run_id="run-terminal", status="failed",
            controller_exit_code=2, worker_exit_code=130, termination_reason="controller_exception",
            receipt={"status": "failed"},
        )
        with psycopg.connect(DSN) as connection:
            assert connection.execute(
                "SELECT status FROM amazon_us.operation_run WHERE tenant_id=%s AND operation_id='op-terminal'",
                (tenant_id,),
            ).fetchone() == ("interrupted",)
            assert connection.execute(
                "SELECT status FROM amazon_us.collection_run WHERE tenant_id=%s AND run_id='run-terminal'",
                (tenant_id,),
            ).fetchone() == ("interrupted",)
            connection.execute(
                "DELETE FROM amazon_us.collection_run WHERE tenant_id=%s AND run_id='run-terminal'",
                (tenant_id,),
            )
            connection.commit()

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            raw_path = Path(temporary) / "mismatch.html"
            raw_path.write_text("""
              <html><head><link rel='canonical' href='https://www.amazon.com/dp/B0B9ZFZZZZ'></head>
              <body><input id='ASIN' value='B0B9ZFZZZZ'><span id='productTitle'>Sibling</span>
              <script>var x={parentAsin:'B0PARENT01',landingAsin:'B0B9ZFZZZZ',
              dimensionValuesDisplayData:{'B0B9ZFDZNJ':['A'],'B0B9ZFZZZZ':['B']}};</script></body></html>
            """, encoding="utf-8")
            with psycopg.connect(DSN) as connection:
                connection.execute(
                    "INSERT INTO amazon_us.collection_evidence"
                    "(tenant_id,marketplace,asin,subject_type,run_id,url,error_code,raw_html_path,context_json) "
                    "VALUES (%s,'US','B0B9ZFDZNJ','own','identity-backfill','https://www.amazon.com/dp/B0B9ZFDZNJ',"
                    "'asin_mismatch',%s,'{}'::jsonb)",
                    (tenant_id, str(raw_path)),
                )
                connection.commit()
            backfill = load_script("backfill_identity_evidence")
            result = backfill.backfill_identity_evidence(
                lambda: psycopg.connect(DSN, row_factory=dict_row)
            )
            assert result["updated"] >= 1
            with psycopg.connect(DSN) as connection:
                identity = connection.execute(
                    "SELECT context_json->'identity' FROM amazon_us.collection_evidence "
                    "WHERE tenant_id=%s AND run_id='identity-backfill'",
                    (tenant_id,),
                ).fetchone()[0]
            assert identity["requested_asin"] == "B0B9ZFDZNJ"
            assert identity["observed_asin"] == "B0B9ZFZZZZ"

        batches = load_console().PostgresConsoleRepository(DSN).list_tenants()
        batch = next(item for item in batches if item["tenant_id"] == tenant_id)
        assert batch["requested"] == 2
        assert batch["recorded"] >= 2
        assert batch["product_succeeded"] == 1
        assert batch["active_duration_seconds"] == 193.08
    finally:
        with psycopg.connect(DSN) as connection:
            for table in (
                "operation_run", "collection_run", "state_history", "review_page_state", "refresh_request", "collection_evidence", "media_asset", "content_module",
                "review_summary", "review_record", "product_snapshot", "item_state", "asin_master",
            ):
                connection.execute(f"DELETE FROM amazon_us.{table} WHERE tenant_id=%s", (tenant_id,))
            connection.commit()


@pytest.mark.skipif(not DSN, reason="AMAZON_TEST_POSTGRES_DSN is not configured")
def test_agent_api_refresh_is_consumed_and_returned_from_real_postgres():
    import psycopg

    service = load_script("agent_collection_service")
    api = load_script("collection_api")
    client_module = load_script("amazon_collection_client")
    worker_module = load_script("amazon_us_worker")
    canary_module = load_script("proxy_canary")
    operation_module = load_script("operation_ledger")
    storage_module = load_storage()
    tenant_id = f"agent-e2e-{uuid.uuid4().hex}"
    schema = (ROOT / "schema" / "postgres_schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(DSN) as connection:
        connection.execute(schema)
        connection.commit()

    storage = storage_module.PostgresWorkerStorage(DSN, tenant_id=tenant_id, subject_type="own")
    storage.initialize_manifest([{
        "asin": "B00RCPDCQU", "url": "https://www.amazon.com/dp/B00RCPDCQU",
        "marketplace": "US", "source_site_label": "agent-e2e", "source_workbook": "fixture",
    }])
    repository = load_script("collection_storage").PostgresCollectionRepository(DSN, tenant_id=tenant_id)

    class Adapter:
        source_type = "http_html"
        last_transfer_bytes = 789
        last_retry_after_seconds = None

        def fetch(self, url):
            return """
            <html><head><link rel="canonical" href="https://www.amazon.com/dp/B00RCPDCQU"></head><body>
              <input id="ASIN" value="B00RCPDCQU"><span id="productTitle">Agent E2E Product</span>
            </body></html>
            """, 200

        def close(self): return None

    config = dict(worker_module.DEFAULTS)
    config.update({
        "max_actions_per_run": 5,
        "raw_html_dir": None,
        "context": {},
        "proxy_url": "http://proxy.example:10000",
        "proxy_session_ports": [10000],
        "proxy_session_max_asins": 5,
    })
    capacity_fact = {
        "schema_version": "amazon-us-proxy-canary-v1",
        "canary_status": "succeeded",
        "planned_slots": 1,
        "tested_slots": 1,
        "available_slots": 1,
        "unique_egress_count": 1,
        "duplicate_egress_count": 0,
        "requested_capacity": 5,
        "required_slots": 1,
        "slot_capacity": 5,
        "capacity_gate_status": "allowed",
        "capacity_gate_reason": "capacity_sufficient",
        "p95_latency_ms": 10.0,
        "config_hash": canary_module.capacity_config_hash(config),
        "sessions": [{
            "session_id": "session-01",
            "status": "available",
            "auth_status": "succeeded",
            "connect_tls_status": "succeeded",
            "error_class": None,
            "http_status": 200,
            "latency_ms": 10.0,
        }],
    }
    connect = lambda: psycopg.connect(DSN)
    operation_id = f"op-canary-{uuid.uuid4().hex}"
    operation_module.ensure_schema(connect)
    operation_module.start_operation(operation_id, tenant_id, "canary", "dataimpulse-us", None, connect=connect)
    operation_module.finish_operation(
        operation_id, tenant_id, "succeeded", None, None, capacity_fact=capacity_fact, connect=connect
    )
    background = service.AgentRefreshWorker(
        storage=storage, adapter_factory=Adapter, config=config, poll_seconds=0.01, lease_seconds=120
    )
    server = api.CollectionServer(
        ("127.0.0.1", 0), repository=repository, api_key="agent-e2e-master",
        refresh_notifier=background.notify, refresh_worker_status=background.status,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    background.start()
    thread.start()
    try:
        client = client_module.AmazonCollectionClient(
            f"http://127.0.0.1:{server.server_port}", "refresh-agent",
            api.derive_agent_key("agent-e2e-master", "refresh-agent"),
        )
        result = client.refresh_and_wait(["B00RCPDCQU"], reason="postgres_e2e", timeout_seconds=5, poll_seconds=0.02)
        item = result["results"][0]
        assert item["job"]["status"] == "completed"
        assert item["result"]["product"]["product"]["title"] == "Agent E2E Product"
        assert item["result"]["latest_evidence"]["transfer_bytes"] == 789
        assert item["result"]["evidence_after_request"] is True
        with psycopg.connect(DSN) as connection:
            audit = connection.execute(
                "SELECT agent_id,action,outcome FROM amazon_us.collection_api_audit "
                "WHERE tenant_id=%s AND action='request_refresh_batch' ORDER BY id DESC LIMIT 1",
                (tenant_id,),
            ).fetchone()
        assert audit == ("refresh-agent", "request_refresh_batch", "accepted")

        background.stop()
        failed_job = repository.request_refresh("US", "B00RCPDCQU", "refresh-agent", "forced_worker_error")
        claimed = storage.claim_refresh_task("agent-refresh-failure", lease_seconds=120)
        assert claimed["job_id"] == failed_job["job_id"]
        assert storage.fail_claimed_refreshes("agent-refresh-failure", "agent_refresh_worker_failed") == 1
        failed_result = repository.load_refresh_request(failed_job["job_id"])
        assert failed_result["status"] == "failed"
        with psycopg.connect(DSN) as connection:
            state = connection.execute(
                "SELECT status,lease_token,lease_owner,last_error FROM amazon_us.item_state "
                "WHERE tenant_id=%s AND asin='B00RCPDCQU'",
                (tenant_id,),
            ).fetchone()
        assert state == ("failed", None, None, "agent_refresh_worker_failed")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        background.stop()
        with psycopg.connect(DSN) as connection:
            for table in (
                "collection_api_audit", "operation_run", "collection_run", "state_history", "review_page_state",
                "refresh_request", "collection_evidence", "media_asset", "content_module", "review_summary",
                "review_record", "product_snapshot", "item_state", "asin_master",
            ):
                connection.execute(f"DELETE FROM amazon_us.{table} WHERE tenant_id=%s", (tenant_id,))
            connection.commit()
