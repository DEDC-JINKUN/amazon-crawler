from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
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
        operation = load_script("operation_ledger")
        connect = lambda: psycopg.connect(DSN)
        operation.ensure_schema(connect)
        ledger.ensure_schema(connect)

        def start_bound_operation(operation_id, run_id):
            operation.start_operation(operation_id, tenant_id, "run", "dataimpulse-us", run_id, connect=connect)
            operation.bind_capacity_authorization(
                operation_id,
                tenant_id,
                {
                    "status": "active", "reason": "capacity_reserved",
                    "reservation_id": f"reservation-{run_id}", "owner_id": "integration-worker",
                    "canary_operation_id": "op-canary-integration", "capacity_config_hash": "a" * 64,
                    "credential_generation": "integration-generation-1", "requested_capacity": 2,
                    "required_slots": 1, "reserved_slots": 1, "slot_ids": ["session-01"],
                    "fact_finished_at": "2026-09-03T01:00:00+00:00",
                    "fact_expires_at": "2026-09-03T02:00:00+00:00",
                    "reservation_expires_at": "2026-09-03T01:10:00+00:00",
                    "capacity_snapshot": {"unique_egress_count": 1, "slot_capacity": 3},
                },
                connect=connect,
            )

        start_bound_operation("op-ledger-run", "ledger-run")
        ledger.start_run(
            connect, tenant_id=tenant_id, run_id="ledger-run", command="run", requested_actions=2,
            worker_id="integration-worker", controller_pid=123, operation_id="op-ledger-run",
        )
        ledger.finish_run(
            connect, tenant_id=tenant_id, run_id="ledger-run", status="interrupted",
            controller_exit_code=130, worker_exit_code=-15, termination_reason="controller_exited",
            receipt={"status": "interrupted"},
        )
        start_bound_operation("op-ledger-run-2", "ledger-run-2")
        ledger.start_run(
            connect, tenant_id=tenant_id, run_id="ledger-run-2", command="run", requested_actions=2,
            worker_id="integration-worker", controller_pid=123, operation_id="op-ledger-run-2",
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
        start_bound_operation("op-terminal", "run-terminal")
        ledger.start_run(
            connect, tenant_id=tenant_id, run_id="run-terminal", command="run", requested_actions=1,
            worker_id="integration-worker", controller_pid=123, operation_id="op-terminal",
        )
        operation.finish_operation(
            "op-terminal", tenant_id, "interrupted", "worker", "controller_exited", connect=connect
        )
        operation.finish_operation(
            "op-terminal", tenant_id, "failed", "worker", "worker_failed", connect=connect
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
            raw_body = """
              <html><head><link rel='canonical' href='https://www.amazon.com/dp/B0B9ZFZZZZ'></head>
              <body><input id='ASIN' value='B0B9ZFZZZZ'><span id='productTitle'>Sibling</span>
              <script>var x={parentAsin:'B0PARENT01',landingAsin:'B0B9ZFZZZZ',
              dimensionValuesDisplayData:{'B0B9ZFDZNJ':['A'],'B0B9ZFZZZZ':['B']}};</script></body></html>
            """
            raw_path.write_bytes(raw_body.encode("utf-8"))
            with psycopg.connect(DSN) as connection:
                connection.execute(
                    "INSERT INTO amazon_us.collection_evidence"
                    "(tenant_id,marketplace,asin,subject_type,run_id,url,error_code,raw_html_path,content_hash,context_json) "
                    "VALUES (%s,'US','B0B9ZFDZNJ','own','identity-backfill','https://www.amazon.com/dp/B0B9ZFDZNJ',"
                    "'asin_mismatch',%s,%s,'{}'::jsonb)",
                    (tenant_id, str(raw_path), hashlib.sha256(raw_body.encode("utf-8")).hexdigest()),
                )
                connection.commit()
            backfill = load_script("backfill_identity_evidence")
            result = backfill.backfill_identity_evidence(
                lambda: psycopg.connect(DSN, row_factory=dict_row),
                tenant_id=tenant_id,
                raw_html_dir=Path(temporary),
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
            assert identity["canonical_valid_amazon"] is True

        batches = load_console().PostgresConsoleRepository(DSN).list_tenants()
        batch = next(item for item in batches if item["tenant_id"] == tenant_id)
        assert batch["requested"] == 2
        assert batch["recorded"] >= 2
        assert batch["product_succeeded"] == 1
        assert batch["active_duration_seconds"] == 193.08
    finally:
        with psycopg.connect(DSN) as connection:
            for table in (
                "proxy_capacity_reservation", "operation_run", "collection_run", "state_history", "review_page_state", "refresh_request", "collection_evidence", "media_asset", "content_module",
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

        def configure_capacity_reservation(self, slot_ids, validator):
            self.capacity_slot_ids = list(slot_ids)
            self.capacity_validator = validator

        def release_capacity_reservation(self): return None

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
        "proxy_session_ports": [10000, 10001],
        "proxy_session_max_asins": 5,
        "proxy_credential_generation": "test-generation-1",
    })
    capacity_fact = {
        "schema_version": "amazon-us-proxy-canary-v1",
        "canary_status": "succeeded",
        "planned_slots": 2,
        "tested_slots": 2,
        "available_slots": 2,
        "unique_egress_count": 2,
        "duplicate_egress_count": 0,
        "requested_capacity": 1,
        "required_slots": 1,
        "slot_budget": 1,
        "slot_capacity": 2,
        "capacity_gate_status": "allowed",
        "capacity_gate_reason": "capacity_sufficient",
        "credential_generation": "test-generation-1",
        "p95_latency_ms": 10.0,
        "config_hash": canary_module.capacity_config_hash(config),
        "sessions": [
            {
                "session_id": f"session-{index:02d}",
                "status": "available",
                "usable": True,
                "auth_status": "succeeded",
                "connect_tls_status": "succeeded",
                "error_class": None,
                "http_status": 200,
                "latency_ms": 10.0,
            }
            for index in range(1, 3)
        ],
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
        assert item["result"]["latest_evidence"]["context_json"]["capacity_authorization"]["canary_operation_id"] == operation_id
        assert item["result"]["latest_evidence"]["context_json"]["capacity_authorization"]["reservation_id"]
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
                "collection_api_audit", "proxy_capacity_reservation", "operation_run", "collection_run", "state_history", "review_page_state",
                "refresh_request", "collection_evidence", "media_asset", "content_module", "review_summary",
                "review_record", "product_snapshot", "item_state", "asin_master",
            ):
                connection.execute(f"DELETE FROM amazon_us.{table} WHERE tenant_id=%s", (tenant_id,))
            connection.commit()


@pytest.mark.skipif(not DSN, reason="AMAZON_TEST_POSTGRES_DSN is not configured")
def test_variant_resolution_is_terminal_for_normal_claim_but_explicit_refresh_can_recheck():
    import psycopg

    storage_module = load_storage()
    collection_module = load_script("collection_storage")
    tenant_id = f"variant-resolution-{uuid.uuid4().hex}"
    connect = lambda: psycopg.connect(DSN)
    with connect() as connection:
        connection.execute((ROOT / "schema" / "postgres_schema.sql").read_text(encoding="utf-8"))
        connection.commit()
    storage = storage_module.PostgresWorkerStorage(DSN, tenant_id=tenant_id, subject_type="own")
    storage.initialize_manifest([{
        "asin": "B0B9ZFDZNJ", "url": "https://www.amazon.com/dp/B0B9ZFDZNJ",
        "marketplace": "US", "source_site_label": "variant-test", "source_workbook": "fixture",
    }])
    with connect() as connection:
        connection.execute(
            "UPDATE amazon_us.item_state SET status='running',lease_token='variant-token',"
            "lease_owner='variant-worker',lease_expires_at=CURRENT_TIMESTAMP+INTERVAL '5 minutes' "
            "WHERE tenant_id=%s AND asin='B0B9ZFDZNJ'",
            (tenant_id,),
        )
        connection.commit()
    evidence = {
        "run_id": "run-variant", "url": "https://www.amazon.com/dp/B0B9ZFDZNJ",
        "http_status": 200, "transfer_bytes": 100, "source_type": "http_html",
        "content_hash": "a" * 64, "raw_html_path": None, "block_reason": None,
        "parser_version": "amazon-us-v3", "error_code": "asin_mismatch",
        "context_json": {"identity": {
            "requested_asin": "B0B9ZFDZNJ", "observed_asin": "B0B9ZFZZZZ",
            "canonical_asin": "B0B9ZFZZZZ", "canonical_valid_amazon": True,
            "parent_asin": "B0PARENT01", "child_asins": ["B0B9ZFDZNJ", "B0B9ZFZZZZ"],
        }},
    }

    try:
        assert storage.save_failure(
            task={"asin": "B0B9ZFDZNJ", "lease_token": "variant-token", "lease_owner": "variant-worker"},
            reason="variant_redirect", error=None, evidence=evidence,
            next_status="succeeded", state_fields={"task_stage": "complete", "resume_status": None},
            increment_attempts=False,
        )
        with connect() as connection:
            state = connection.execute(
                "SELECT status,attempts,last_error FROM amazon_us.item_state WHERE tenant_id=%s AND asin='B0B9ZFDZNJ'",
                (tenant_id,),
            ).fetchone()
            snapshot_count = connection.execute(
                "SELECT COUNT(*) FROM amazon_us.product_snapshot WHERE tenant_id=%s AND asin='B0B9ZFDZNJ'",
                (tenant_id,),
            ).fetchone()[0]
        assert state == ("succeeded", 0, None)
        assert snapshot_count == 0
        assert storage.claim_task("ordinary-worker") is None

        repository = collection_module.PostgresCollectionRepository(DSN, tenant_id=tenant_id)
        job = repository.request_refresh("US", "B0B9ZFDZNJ", "refresh-agent", "variant_recheck")
        claimed = storage.claim_refresh_task("refresh-worker", lease_seconds=120)
        assert claimed["job_id"] == job["job_id"]
        assert claimed["asin"] == "B0B9ZFDZNJ"
    finally:
        with connect() as connection:
            for table in (
                "collection_api_audit", "proxy_capacity_reservation", "operation_run", "collection_run",
                "state_history", "review_page_state", "refresh_request", "collection_evidence",
                "media_asset", "content_module", "review_summary", "review_record", "product_snapshot",
                "item_state", "asin_master",
            ):
                connection.execute(f"DELETE FROM amazon_us.{table} WHERE tenant_id=%s", (tenant_id,))
            connection.commit()


@pytest.mark.skipif(not DSN, reason="AMAZON_TEST_POSTGRES_DSN is not configured")
def test_proxy_capacity_reservations_are_atomic_across_tenants_and_recheck_canary_ttl():
    import psycopg

    operation = load_script("operation_ledger")
    canary = load_script("proxy_canary")
    storage_module = load_storage()
    base_config = {
        "proxy_url": "http://proxy.example:10000",
        "proxy_username_env": "PROXY_USER",
        "proxy_password_env": "PROXY_PASS",
        "proxy_session_ports": [10000],
        "proxy_session_max_asins": 1,
        "proxy_canary_url": "https://api.ipify.org?format=json",
        "proxy_canary_timeout_seconds": 5,
        "proxy_canary_max_age_seconds": 3600,
    }
    configs = [
        {**base_config, "proxy_credential_generation": "integration-generation-1", "proxy_canary_timeout_seconds": 5},
        {**base_config, "proxy_credential_generation": "integration-generation-2", "proxy_canary_timeout_seconds": 10},
    ]
    config_hashes = [canary.capacity_config_hash(value) for value in configs]
    resource_slot_ids = canary.capacity_resource_slot_ids(configs[0])
    tenants = [f"capacity-a-{uuid.uuid4().hex}", f"capacity-b-{uuid.uuid4().hex}"]
    operation_ids = [f"op-canary-{uuid.uuid4().hex}" for _ in tenants]
    fact = {
        "schema_version": "amazon-us-proxy-canary-v1",
        "canary_status": "succeeded",
        "planned_slots": 1,
        "tested_slots": 1,
        "available_slots": 1,
        "unique_egress_count": 1,
        "duplicate_egress_count": 0,
        "requested_capacity": 1,
        "required_slots": 1,
        "slot_budget": 1,
        "slot_capacity": 1,
        "capacity_gate_status": "allowed",
        "capacity_gate_reason": "capacity_sufficient",
        "credential_generation": None,
        "p95_latency_ms": 10.0,
        "config_hash": None,
        "sessions": [{
            "session_id": "session-01", "status": "available", "usable": True,
            "auth_status": "succeeded", "connect_tls_status": "succeeded",
            "error_class": None, "http_status": 200, "latency_ms": 10.0,
        }],
    }
    connect = lambda: psycopg.connect(DSN)
    operation.ensure_schema(connect)
    for index, (tenant_id, operation_id) in enumerate(zip(tenants, operation_ids)):
        tenant_fact = {**fact, "credential_generation": configs[index]["proxy_credential_generation"], "config_hash": config_hashes[index]}
        operation.start_operation(operation_id, tenant_id, "canary", "dataimpulse-us", None, connect=connect)
        operation.finish_operation(operation_id, tenant_id, "succeeded", None, None, capacity_fact=tenant_fact, connect=connect)
    storages = [storage_module.PostgresWorkerStorage(DSN, tenant_id=tenant_id) for tenant_id in tenants]
    barrier = threading.Barrier(2)

    def reserve(index):
        barrier.wait()
        return storages[index].reserve_proxy_capacity(
            reservation_id=f"reservation-{index}-{uuid.uuid4().hex}",
            owner_id=f"worker-{index}",
            capacity_config_hash=config_hashes[index],
            credential_generation=configs[index]["proxy_credential_generation"],
            resource_slot_ids=resource_slot_ids,
            requested_capacity=1,
            required_slots=1,
            reservation_slots=1,
            slot_budget=1,
            max_age_seconds=3600,
            lease_seconds=600,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reserve, (0, 1)))
        assert sorted(item["status"] for item in results) == ["active", "denied"]
        denied = next(item for item in results if item["status"] == "denied")
        assert denied["reason"] == "capacity_reserved_elsewhere"
        active = next(item for item in results if item["status"] == "active")
        active_index = int(active["owner_id"].split("-")[-1])
        assert active["slot_ids"] == ["session-01"]

        with psycopg.connect(DSN) as connection:
            connection.execute(
                "UPDATE amazon_us.operation_run SET finished_at=CURRENT_TIMESTAMP-INTERVAL '2 hours' "
                "WHERE operation_id=%s",
                (active["canary_operation_id"],),
            )
            connection.commit()
        expired = storages[active_index].validate_proxy_capacity_reservation(
            active["reservation_id"], active["owner_id"], max_age_seconds=3600, lease_seconds=600
        )
        assert expired["status"] == "denied"
        assert expired["reason"] == "capacity_evidence_stale"
    finally:
        with psycopg.connect(DSN) as connection:
            connection.execute("DELETE FROM amazon_us.proxy_capacity_reservation WHERE tenant_id=ANY(%s)", (tenants,))
            connection.execute("DELETE FROM amazon_us.operation_run WHERE tenant_id=ANY(%s)", (tenants,))
            connection.commit()


@pytest.mark.skipif(not DSN, reason="AMAZON_TEST_POSTGRES_DSN is not configured")
def test_released_proxy_capacity_reservations_rotate_all_usable_slots_before_reuse():
    import psycopg

    operation = load_script("operation_ledger")
    canary = load_script("proxy_canary")
    storage_module = load_storage()
    tenant_id = f"capacity-rotation-{uuid.uuid4().hex}"
    operation_id = f"op-canary-{uuid.uuid4().hex}"
    config = {
        "proxy_url": "http://proxy.example:15000",
        "proxy_username_env": "PROXY_USER", "proxy_password_env": "PROXY_PASS",
        "proxy_session_ports": [15000, 15001, 15002], "proxy_session_max_asins": 1,
        "proxy_product_session_scope": "per_asin",
        "proxy_canary_url": "https://api.ipify.org?format=json",
        "proxy_canary_timeout_seconds": 5,
        "proxy_credential_generation": "integration-generation-rotation",
    }
    config_hash = canary.capacity_config_hash(config)
    resource_slot_ids = canary.capacity_resource_slot_ids(config)
    fact = {
        "schema_version": "amazon-us-proxy-canary-v1", "canary_status": "succeeded",
        "planned_slots": 3, "tested_slots": 3, "available_slots": 3, "unique_egress_count": 3,
        "duplicate_egress_count": 0, "requested_capacity": 1, "required_slots": 1,
        "slot_budget": 1, "slot_capacity": 3, "capacity_gate_status": "allowed",
        "capacity_gate_reason": "capacity_sufficient",
        "credential_generation": config["proxy_credential_generation"], "p95_latency_ms": 10.0,
        "config_hash": config_hash,
        "sessions": [
            {"session_id": f"session-{index:02d}", "status": "available", "usable": True,
             "auth_status": "succeeded", "connect_tls_status": "succeeded",
             "error_class": None, "http_status": 200, "latency_ms": 10.0}
            for index in range(1, 4)
        ],
    }
    connect = lambda: psycopg.connect(DSN)
    operation.ensure_schema(connect)
    operation.start_operation(operation_id, tenant_id, "canary", "dataimpulse-us", None, connect=connect)
    operation.finish_operation(operation_id, tenant_id, "succeeded", None, None, capacity_fact=fact, connect=connect)
    repository = storage_module.PostgresWorkerStorage(DSN, tenant_id=tenant_id)

    try:
        selected = []
        for index in range(4):
            reservation = repository.reserve_proxy_capacity(
                reservation_id=f"reservation-rotation-{uuid.uuid4().hex}",
                owner_id=f"worker-rotation-{index}", capacity_config_hash=config_hash,
                credential_generation=config["proxy_credential_generation"],
                resource_slot_ids=resource_slot_ids, requested_capacity=1, required_slots=1,
                reservation_slots=1, slot_budget=1, max_age_seconds=3600, lease_seconds=600,
            )
            selected.append(reservation["slot_ids"])
            assert repository.release_proxy_capacity(reservation["reservation_id"], reservation["owner_id"])
        assert selected == [["session-01"], ["session-02"], ["session-03"], ["session-01"]]
    finally:
        with psycopg.connect(DSN) as connection:
            connection.execute("DELETE FROM amazon_us.proxy_capacity_reservation WHERE tenant_id=%s", (tenant_id,))
            connection.execute("DELETE FROM amazon_us.operation_run WHERE tenant_id=%s", (tenant_id,))
            connection.commit()


@pytest.mark.skipif(not DSN, reason="AMAZON_TEST_POSTGRES_DSN is not configured")
def test_all_healthy_but_insufficient_canary_fact_is_persisted_as_denied():
    import psycopg

    operation = load_script("operation_ledger")
    canary = load_script("proxy_canary")
    tenant_id = f"capacity-denied-{uuid.uuid4().hex}"
    operation_id = f"op-canary-{uuid.uuid4().hex}"
    config = {
        "proxy_url": "http://proxy.example:10000", "proxy_username_env": "PROXY_USER",
        "proxy_password_env": "PROXY_PASS", "proxy_session_ports": [10000],
        "proxy_session_max_asins": 1, "proxy_canary_url": "https://api.ipify.org?format=json",
        "proxy_canary_timeout_seconds": 5, "proxy_credential_generation": "integration-generation-denied",
    }
    fact = {
        "schema_version": "amazon-us-proxy-canary-v1", "canary_status": "succeeded",
        "planned_slots": 1, "tested_slots": 1, "available_slots": 1, "unique_egress_count": 1,
        "duplicate_egress_count": 0, "requested_capacity": 2, "required_slots": 2,
        "slot_budget": 1, "slot_capacity": 1, "capacity_gate_status": "denied",
        "capacity_gate_reason": "unique_capacity_insufficient", "credential_generation": "integration-generation-denied",
        "p95_latency_ms": 10.0, "config_hash": canary.capacity_config_hash(config),
        "sessions": [{"session_id": "session-01", "status": "available", "usable": True,
                      "auth_status": "succeeded", "connect_tls_status": "succeeded",
                      "error_class": None, "http_status": 200, "latency_ms": 10.0}],
    }
    connect = lambda: psycopg.connect(DSN)
    try:
        operation.ensure_schema(connect)
        operation.start_operation(operation_id, tenant_id, "canary", "dataimpulse-us", None, connect=connect)
        operation.finish_operation(
            operation_id, tenant_id, "failed", "capacity_gate", "unique_capacity_insufficient",
            capacity_fact=fact, connect=connect,
        )
        with psycopg.connect(DSN) as connection:
            assert connection.execute(
                "SELECT canary_status,capacity_gate_status,slot_capacity FROM amazon_us.operation_run WHERE operation_id=%s",
                (operation_id,),
            ).fetchone() == ("succeeded", "denied", 1)
    finally:
        with psycopg.connect(DSN) as connection:
            connection.execute("DELETE FROM amazon_us.operation_run WHERE tenant_id=%s", (tenant_id,))
            connection.commit()
