from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RefreshStorage:
    tenant_id = "tenant-agent"

    def __init__(self):
        self.claimed = False
        self.saved = []
        self.finished = []
        self.capacity_fresh = True
        self.capacity_reservation = None

    def reclaim_expired_leases(self):
        return 0

    def has_pending_refresh_task(self):
        return not self.claimed

    def count_pending_refresh_tasks(self, limit):
        assert limit == 5
        return 0 if self.claimed else 1

    def load_latest_proxy_capacity(self, *, max_age_seconds):
        assert max_age_seconds == 3600
        return {
            "canary_status": "succeeded",
            "unique_egress_count": 1,
            "slot_capacity": 5,
            "requested_capacity": 5,
            "capacity_config_hash": self.capacity_config_hash,
            "capacity_gate_reason": "capacity_sufficient",
            "is_fresh": True,
        }

    def reserve_proxy_capacity(self, **kwargs):
        if not self.capacity_fresh:
            return {"status": "denied", "reason": "capacity_evidence_stale", "reservation_id": kwargs["reservation_id"]}
        self.capacity_reservation = {
            "status": "active", "reason": "capacity_reserved", "reservation_id": kwargs["reservation_id"],
            "owner_id": kwargs["owner_id"], "canary_operation_id": "op-canary-agent",
            "capacity_config_hash": kwargs["capacity_config_hash"],
            "credential_generation": kwargs["credential_generation"],
            "requested_capacity": kwargs["requested_capacity"], "required_slots": kwargs["required_slots"],
            "reserved_slots": kwargs["reservation_slots"],
            "slot_ids": [f"session-{index + 1:02d}" for index in range(kwargs["reservation_slots"])],
            "fact_finished_at": "2026-09-03T01:00:00+00:00",
            "fact_expires_at": "2026-09-03T02:00:00+00:00",
            "reservation_expires_at": "2026-09-03T01:10:00+00:00",
            "capacity_snapshot": {"unique_egress_count": 2, "slot_capacity": 10},
        }
        return dict(self.capacity_reservation)

    def validate_proxy_capacity_reservation(self, *_args, **_kwargs):
        if not self.capacity_fresh:
            return {"status": "denied", "reason": "capacity_evidence_stale", "reservation_id": "capacity-agent"}
        return dict(self.capacity_reservation)

    def release_proxy_capacity(self, *_args):
        return True

    def claim_refresh_task(self, worker_id, lease_seconds=None):
        if self.claimed:
            return None
        self.claimed = True
        return {
            "job_id": "refresh-1", "asin": "B00RCPDCQU", "marketplace": "US",
            "url": "https://www.amazon.com/dp/B00RCPDCQU", "status": "running",
            "task_stage": "product", "lease_token": "token-1", "lease_owner": worker_id,
            "reported_review_count": 0, "fetched_review_count": 0, "review_pages_fetched": 0,
        }

    def claim_task(self, *args, **kwargs):
        raise AssertionError("agent service must not claim ordinary tasks")

    def save_product_result(self, **payload):
        self.saved.append(payload)
        return True

    def save_failure(self, **payload):
        self.saved.append(payload)
        return True

    def finish_refresh_request(self, job_id, status):
        self.finished.append((job_id, status))


class Adapter:
    source_type = "http_html"
    last_transfer_bytes = 321
    last_retry_after_seconds = None

    def configure_capacity_reservation(self, slot_ids, validator):
        self.capacity_slot_ids = list(slot_ids)
        self.capacity_validator = validator

    def release_capacity_reservation(self):
        return None

    def fetch(self, url):
        return """
        <html><head><link rel="canonical" href="https://www.amazon.com/dp/B00RCPDCQU"></head><body>
          <input id="ASIN" value="B00RCPDCQU"><span id="productTitle">Agent refreshed product</span>
        </body></html>
        """, 200

    def close(self):
        return None


class ExplodingAdapter(Adapter):
    def fetch(self, url):
        raise RuntimeError("provider detail must not escape")


class FailingRefreshStorage(RefreshStorage):
    def fail_claimed_refreshes(self, worker_id, reason):
        assert worker_id.startswith("agent-refresh-")
        self.finished.append(("refresh-1", "failed"))
        self.failure_reason = reason
        return 1


def test_background_service_executes_only_refresh_jobs_and_reports_health():
    service = load("agent_collection_service")
    worker_module = load("amazon_us_worker")
    storage = RefreshStorage()
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
    storage.capacity_config_hash = load("proxy_canary").capacity_config_hash(config)
    background = service.AgentRefreshWorker(
        storage=storage,
        adapter_factory=Adapter,
        config=config,
        poll_seconds=0.01,
        lease_seconds=120,
    )

    background.start()
    background.notify()
    deadline = time.monotonic() + 2
    while not storage.finished and time.monotonic() < deadline:
        time.sleep(0.01)
    background.stop()

    assert storage.finished == [("refresh-1", "completed")]
    assert storage.capacity_reservation["reserved_slots"] == 2
    assert storage.capacity_reservation["slot_ids"] == ["session-01", "session-02"]
    assert storage.saved[0]["product"]["title"] == "Agent refreshed product"
    status = background.status()
    assert status["state"] == "stopped"
    assert status["processed_actions"] == 1
    assert status["succeeded_actions"] == 1
    assert status["failed_actions"] == 0
    assert status["blocked_actions"] == 0
    assert status["completed_actions"] == 1
    assert status["last_error"] is None


def test_background_service_gives_one_agent_batch_to_one_bounded_pool_run():
    service = load("agent_collection_service")
    worker_module = load("amazon_us_worker")
    calls = []

    class FivePendingStorage(RefreshStorage):
        def count_pending_refresh_tasks(self, limit):
            assert limit == 5
            return 5

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return 0

    background = service.AgentRefreshWorker(
        storage=FivePendingStorage(),
        adapter_factory=Adapter,
        config=dict(worker_module.DEFAULTS),
        poll_seconds=0.01,
        lease_seconds=120,
    )
    with patch.object(service, "run_postgres_actions", side_effect=fake_run):
        background.start()
        deadline = time.monotonic() + 2
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        background.stop()

    assert calls
    assert calls[0]["limit"] == 5
    assert "enforce_capacity_gate" not in calls[0]


def test_background_service_scopes_capacity_to_actual_pending_refresh_count():
    service = load("agent_collection_service")
    worker_module = load("amazon_us_worker")
    calls = []

    class ThreePendingStorage(RefreshStorage):
        def count_pending_refresh_tasks(self, limit):
            assert limit == service.MAX_AGENT_REFRESH_BATCH
            return 3

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return 0

    background = service.AgentRefreshWorker(
        storage=ThreePendingStorage(), adapter_factory=Adapter,
        config=dict(worker_module.DEFAULTS), poll_seconds=0.01, lease_seconds=120,
    )
    with patch.object(service, "run_postgres_actions", side_effect=fake_run):
        background.start()
        deadline = time.monotonic() + 2
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        background.stop()

    assert calls
    assert calls[0]["limit"] == 3


def test_agent_health_separates_processed_success_failed_and_blocked_actions():
    service = load("agent_collection_service")
    worker_module = load("amazon_us_worker")

    class OnePendingStorage(RefreshStorage):
        def count_pending_refresh_tasks(self, limit):
            return 0 if self.finished else 1

    def fake_run(storage, *_args, **_kwargs):
        storage.save_failure(task={"asin": "B000000001"}, reason="fetch_error", next_status="failed")
        storage.finish_refresh_request("job-1", "failed")
        return 1

    background = service.AgentRefreshWorker(
        storage=OnePendingStorage(), adapter_factory=Adapter,
        config=dict(worker_module.DEFAULTS), poll_seconds=0.01, lease_seconds=120,
    )
    with patch.object(service, "run_postgres_actions", side_effect=fake_run):
        background.start()
        deadline = time.monotonic() + 2
        while background.status().get("processed_actions", 0) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        status = background.status()
        background.stop()

    assert status["processed_actions"] == 1
    assert status["succeeded_actions"] == 0
    assert status["failed_actions"] == 1
    assert status["blocked_actions"] == 0
    assert status["completed_actions"] == 0


def test_agent_health_counts_one_blocked_asin_without_stopping_service():
    service = load("agent_collection_service")
    worker_module = load("amazon_us_worker")

    class OnePendingStorage(RefreshStorage):
        def count_pending_refresh_tasks(self, limit):
            return 0 if self.finished else 1

    def fake_run(storage, *_args, **_kwargs):
        storage.save_failure(
            task={"asin": "B000000001"}, reason="captcha", next_status="blocked",
            state_fields={"block_reason": "captcha"},
        )
        storage.finish_refresh_request("job-1", "failed")
        return 1

    background = service.AgentRefreshWorker(
        storage=OnePendingStorage(), adapter_factory=Adapter,
        config=dict(worker_module.DEFAULTS), poll_seconds=0.01, lease_seconds=120,
    )
    with patch.object(service, "run_postgres_actions", side_effect=fake_run):
        background.start()
        deadline = time.monotonic() + 2
        while background.status()["processed_actions"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        status = background.status()
        background.stop()

    assert status["state"] == "running"
    assert status["processed_actions"] == 1
    assert status["succeeded_actions"] == 0
    assert status["failed_actions"] == 0
    assert status["blocked_actions"] == 1
    assert status["completed_actions"] == 0


def test_refresh_storage_counts_variant_resolution_as_processed_not_product_success_or_failure():
    service = load("agent_collection_service")

    class Storage:
        def save_failure(self, **_payload): return True
        def finish_refresh_request(self, _job_id, _status): return None

    view = service.RefreshOnlyStorageView(Storage())
    view.begin_batch_metrics()
    assert view.save_failure(
        task={"asin": "B000000001"}, reason="variant_redirect",
        error=None, next_status="succeeded", increment_attempts=False,
    ) is True
    view.finish_refresh_request("job-1", "completed")

    assert view.batch_metrics() == {
        "processed": 1, "succeeded": 0, "variant_redirect": 1,
        "failed": 0, "blocked": 0,
    }


def test_agent_stops_only_when_runner_reports_global_access_circuit():
    service = load("agent_collection_service")
    worker_module = load("amazon_us_worker")

    def fake_run(*_args, **_kwargs): return -1

    background = service.AgentRefreshWorker(
        storage=RefreshStorage(), adapter_factory=Adapter,
        config=dict(worker_module.DEFAULTS), poll_seconds=0.01, lease_seconds=120,
    )
    with patch.object(service, "run_postgres_actions", side_effect=fake_run):
        background.start()
        deadline = time.monotonic() + 2
        while background.status()["state"] != "blocked" and time.monotonic() < deadline:
            time.sleep(0.01)
        status = background.status()
        background.stop()

    assert status["state"] == "blocked"
    assert status["last_error"] == "access_blocked"


def test_agent_capacity_denial_claims_nothing_and_recovers_after_fresh_canary():
    service = load("agent_collection_service")
    worker_module = load("amazon_us_worker")
    storage = RefreshStorage()
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
    storage.capacity_config_hash = load("proxy_canary").capacity_config_hash(config)
    storage.capacity_fresh = False
    background = service.AgentRefreshWorker(
        storage=storage,
        adapter_factory=Adapter,
        config=config,
        poll_seconds=0.01,
        lease_seconds=120,
    )

    background.start()
    deadline = time.monotonic() + 2
    while background.status()["state"] != "blocked" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert background.status()["last_error"] == "capacity_evidence_stale"
    assert background.status()["capacity_decision"]["reason"] == "capacity_evidence_stale"
    assert storage.claimed is False

    storage.capacity_fresh = True
    background.notify()
    deadline = time.monotonic() + 2
    while not storage.finished and time.monotonic() < deadline:
        time.sleep(0.01)
    background.stop()

    assert storage.finished == [("refresh-1", "completed")]


def test_service_cli_is_postgres_only_and_loopback_only():
    service = load("agent_collection_service")
    parser = service.build_parser()
    args = parser.parse_args([
        "--tenant-id", "tenant-agent", "--config", "config/amazon_us.windows.toml",
        "--output-dir", "data/tenant-agent",
        "--host", "127.0.0.1", "--port", "8765",
    ])

    assert args.tenant_id == "tenant-agent"
    assert args.host == "127.0.0.1"
    assert args.port == 8765


def test_service_cli_builds_the_same_proxy_session_adapter_as_batch_workers(tmp_path):
    service = load("agent_collection_service")
    worker_module = load("amazon_us_worker")
    config = {
        **worker_module.DEFAULTS,
        "proxy_url": "http://proxy.local:10000",
        "proxy_session_ports": [10000, 10001],
    }
    marker = object()
    captured = {}

    class FakeWorker:
        def __init__(self, *, adapter_factory, **kwargs):
            captured["adapter"] = adapter_factory()

        def start(self):
            return None

        def stop(self):
            return None

        def notify(self):
            return None

        def status(self):
            return {"state": "running"}

    class FakeServer:
        server_port = 8765

        def __init__(self, *args, **kwargs):
            return None

        def serve_forever(self):
            return None

        def server_close(self):
            return None

    environment = {
        "AMAZON_US_POSTGRES_DSN": "postgresql://fixture",
        "AMAZON_COLLECTION_API_KEY": "fixture-key",
    }
    with (
        patch.dict(os.environ, environment, clear=False),
        patch.object(service, "load_config", return_value=config),
        patch.object(service, "PostgresWorkerStorage", return_value=object()),
        patch.object(service, "PostgresCollectionRepository", return_value=object()),
        patch.object(service, "AgentRefreshWorker", FakeWorker),
        patch.object(service, "CollectionServer", FakeServer),
        patch.object(service, "_build_http_adapter", return_value=marker, create=True) as shared_builder,
    ):
        result = service.main([
            "--tenant-id", "tenant-agent",
            "--config", str(tmp_path / "config.toml"),
            "--output-dir", str(tmp_path / "output"),
        ])

    assert result == 0
    assert captured["adapter"] is marker
    shared_builder.assert_called_once_with(config)


def test_unexpected_worker_error_terminalizes_claimed_refresh_without_detail_leak():
    service = load("agent_collection_service")
    worker_module = load("amazon_us_worker")
    storage = FailingRefreshStorage()
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
    storage.capacity_config_hash = load("proxy_canary").capacity_config_hash(config)
    background = service.AgentRefreshWorker(
        storage=storage, adapter_factory=ExplodingAdapter, config=config, poll_seconds=0.01, lease_seconds=120
    )

    background.start()
    deadline = time.monotonic() + 2
    while background.status()["state"] not in {"failed", "blocked"} and time.monotonic() < deadline:
        time.sleep(0.01)
    status = background.status()
    background.stop()

    assert storage.finished == [("refresh-1", "failed")]
    assert storage.failure_reason == "agent_refresh_worker_failed"
    assert status["state"] == "failed"
    assert status["last_error"] == "RuntimeError"
    assert "provider detail" not in str(status)
