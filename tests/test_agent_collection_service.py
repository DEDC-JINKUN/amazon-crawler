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

    def reclaim_expired_leases(self):
        return 0

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
        "proxy_session_ports": [10000],
        "proxy_session_max_asins": 5,
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
    assert storage.saved[0]["product"]["title"] == "Agent refreshed product"
    status = background.status()
    assert status["state"] == "stopped"
    assert status["completed_actions"] == 1
    assert status["last_error"] is None


def test_background_service_gives_one_agent_batch_to_one_bounded_pool_run():
    service = load("agent_collection_service")
    worker_module = load("amazon_us_worker")
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return 0

    background = service.AgentRefreshWorker(
        storage=RefreshStorage(),
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
    assert calls[0]["enforce_capacity_gate"] is True


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
        "proxy_session_ports": [10000],
        "proxy_session_max_asins": 5,
    })
    storage.capacity_config_hash = load("proxy_canary").capacity_config_hash(config)
    fresh = False
    original_reader = storage.load_latest_proxy_capacity

    def capacity(*, max_age_seconds):
        value = original_reader(max_age_seconds=max_age_seconds)
        value["is_fresh"] = fresh
        return value

    storage.load_latest_proxy_capacity = capacity
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
    assert storage.claimed is False

    fresh = True
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
        "proxy_session_ports": [10000],
        "proxy_session_max_asins": 5,
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
