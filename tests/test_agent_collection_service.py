from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_effective_worker_count_backpressures_to_fresh_capacity_and_process_quota():
    service=load('agent_collection_service')
    assert service.effective_worker_count(1,None)==1
    assert service.effective_worker_count(12,{'unique_egress_count':5},process_limit=8)==2
    assert service.effective_worker_count(12,{'unique_egress_count':50},process_limit=6)==6


def valid_three_worker_capacity(service):
    config = {
        "egress_profile": "proxy_sessions",
        "proxy_url": "http://proxy.example",
        "proxy_session_ports": list(range(10000, 10030)),
        "proxy_product_session_scope": "per_asin",
        "proxy_session_max_asins": 3,
        "proxy_session_retry_per_asin": 1,
        "proxy_canary_url": "https://api.ipify.org?format=json",
        "proxy_canary_timeout_seconds": 15,
        "proxy_credential_generation": "test-generation-20260914",
    }
    canary = load("proxy_canary")
    fact = {
        "is_fresh": True,
        "capacity_config_hash": canary.capacity_config_hash(config),
        "canary_status": "succeeded",
        "capacity_gate_status": "allowed",
        "capacity_gate_reason": "capacity_sufficient",
        "planned_slots": 30,
        "tested_slots": 30,
        "available_slots": 30,
        "unique_egress_count": 30,
        "duplicate_egress_count": 0,
        "requested_capacity": 15,
        "required_slots": 15,
        "slot_budget": 1,
        "slot_capacity": 30,
    }
    return config, fact


def test_three_worker_count_and_runtime_readiness_fail_closed_on_stale_or_denied_capacity():
    service = load("agent_collection_service")
    config, fact = valid_three_worker_capacity(service)
    assert service.effective_worker_count(3, fact, config) == 3

    stale = {**fact, "is_fresh": False}
    denied = {**fact, "capacity_gate_status": "denied", "capacity_gate_reason": "capacity_insufficient"}
    assert service.effective_worker_count(3, stale, config) == 1
    assert service.effective_worker_count(3, denied, config) == 1

    decision = service.pool_capacity_decision(stale, config, effective_workers=3)
    status = service.worker_pool_status(
        type("Worker", (), {"status": lambda self: {"state": "running"}})(),
        [],
        requested=3,
        effective=3,
        capacity_decision=decision,
    )
    assert decision["status"] == "denied"
    assert status["state"] == "blocked"
    assert status["last_error"] == "capacity_evidence_stale"


def test_embedded_worker_readiness_rejects_a_stale_business_heartbeat():
    service = load("agent_collection_service")
    stale = (__import__('datetime').datetime.now(__import__('datetime').timezone.utc) - __import__('datetime').timedelta(seconds=5)).isoformat()

    class Worker:
        def status(self):
            return {"state": "running", "worker_heartbeat_at": stale}

    status = service.worker_pool_status(
        Worker(), [], requested=1, effective=1, worker_heartbeat_timeout=0.1,
    )
    assert status["state"] == "blocked"
    assert status["last_error"] == "worker_heartbeat_stale"


def test_worker_pool_status_blocks_requested_effective_capacity_downshift():
    service = load("agent_collection_service")

    class EmbeddedWorker:
        def status(self):
            return {"state": "running", "processed_actions": 3}

    status = service.worker_pool_status(EmbeddedWorker(), [], requested=2, effective=1)

    assert status["state"] == "blocked"
    assert status["last_error"] == "worker_capacity_downshifted"
    assert status["processed_actions"] == 3
    assert status["requested_workers"] == 2
    assert status["effective_workers"] == 1
    assert status["alive_workers"] == 1
    assert status["capacity_backpressure"] is True


def test_campaign_worker_supervisor_restarts_one_crash_and_requires_fresh_child_heartbeat(tmp_path):
    service = load("agent_collection_service")

    class Process:
        def __init__(self, pid, returncode):
            self.pid = pid
            self.returncode = returncode
        def poll(self): return self.returncode
        def terminate(self): self.returncode = 0
        def wait(self, timeout=None): return self.returncode
        def kill(self): self.returncode = -9

    class Factory:
        def __init__(self): self.processes = []
        def __call__(self, command, **_kwargs):
            process = Process(1000 + len(self.processes), 2 if not self.processes else None)
            self.processes.append((list(command), process))
            return process

    factory = Factory()
    supervisor = service.CampaignWorkerSupervisor(
        ["python", "campaign_worker_process.py"],
        count=1,
        cwd=tmp_path,
        environment={},
        status_dir=tmp_path / "status",
        spawn=factory,
        monitor_interval=0.01,
        heartbeat_timeout=1.0,
        restart_limit=1,
    )
    supervisor.start()
    try:
        deadline = time.monotonic() + 2
        while len(factory.processes) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(factory.processes) == 2
        replacement = factory.processes[-1][1]
        status_path = tmp_path / "status" / "worker-1.json"
        status_path.write_text(__import__('json').dumps({
            "schema": "amazon-campaign-worker-status-v1",
            "pid": replacement.pid,
            "state": "running",
            "last_error": None,
            "heartbeat_at": __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
            "worker_heartbeat_at": __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
        }), encoding="utf-8")
        status = supervisor.status()
        assert status["state"] == "running"
        assert status["alive_workers"] == 1
        assert status["healthy_workers"] == 1
        assert status["restart_count"] == 1
        assert "--status-file" in factory.processes[-1][0]
    finally:
        supervisor.stop()


def test_campaign_worker_supervisor_fails_after_restart_budget_and_does_not_restart_blocked_child(tmp_path):
    service = load("agent_collection_service")

    class Process:
        def __init__(self, pid, returncode): self.pid, self.returncode = pid, returncode
        def poll(self): return self.returncode
        def terminate(self): self.returncode = 0
        def wait(self, timeout=None): return self.returncode
        def kill(self): self.returncode = -9

    def run_case(returncode):
        processes = []
        def spawn(_command, **_kwargs):
            process = Process(2000 + len(processes), returncode)
            processes.append(process)
            return process
        supervisor = service.CampaignWorkerSupervisor(
            ["python", "campaign_worker_process.py"], count=1, cwd=tmp_path,
            environment={}, status_dir=tmp_path / f"status-{returncode}", spawn=spawn,
            monitor_interval=0.01, heartbeat_timeout=1, restart_limit=1,
        )
        supervisor.start()
        deadline = time.monotonic() + 2
        expected = "blocked" if returncode == 3 else "failed"
        try:
            while supervisor.status()["state"] != expected and time.monotonic() < deadline:
                time.sleep(0.01)
            return supervisor.status(), len(processes)
        finally:
            supervisor.stop()

    failed, failed_spawns = run_case(2)
    blocked, blocked_spawns = run_case(3)
    assert failed["state"] == "failed" and failed_spawns == 2
    assert blocked["state"] == "blocked" and blocked_spawns == 1


def test_service_parser_exposes_one_managed_primary_console():
    service=load('agent_collection_service')
    args=service.build_parser().parse_args([
        '--config','config.toml','--output-dir','data','--tenant-id','amazon_us_main',
        '--console-port','8770','--console-tenant-id','amazon_us_main',
        '--operator-base-url','http://127.0.0.1:8772',
    ])
    assert args.console_port==8770
    assert args.console_tenant_id=='amazon_us_main'
    assert args.operator_base_url=='http://127.0.0.1:8772'


def test_campaign_child_receives_custom_dsn_and_minimal_environment(monkeypatch, tmp_path):
    service = load('agent_collection_service')
    monkeypatch.setenv("CUSTOM_CAMPAIGN_DSN", "postgresql://fixture")
    monkeypatch.setenv("AMAZON_COLLECTION_API_KEY", "must-not-leak")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    monkeypatch.setenv("PROXY_USER_SECRET", "proxy-user")
    monkeypatch.setenv("PROXY_PASS_SECRET", "proxy-pass")
    config={"proxy_username_env":"PROXY_USER_SECRET","proxy_password_env":"PROXY_PASS_SECRET"}
    environment=service.build_worker_environment("CUSTOM_CAMPAIGN_DSN",config)
    assert environment["CUSTOM_CAMPAIGN_DSN"]=="postgresql://fixture"
    assert environment["PROXY_USER_SECRET"]=="proxy-user"
    assert environment["PROXY_PASS_SECRET"]=="proxy-pass"
    assert "AMAZON_COLLECTION_API_KEY" not in environment
    assert "UNRELATED_SECRET" not in environment
    args=service.build_parser().parse_args(["--config",str(tmp_path/"worker.toml"),"--output-dir",str(tmp_path),"--tenant-id","campaign-test","--dsn-env","CUSTOM_CAMPAIGN_DSN"])
    command=service.worker_process_command(args,tmp_path/"campaign_worker_process.py",tmp_path)
    assert command[command.index("--dsn-env")+1]=="CUSTOM_CAMPAIGN_DSN"


def test_campaign_worker_process_survives_transient_windows_status_replace_lock(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT/'scripts'))
    process=load('campaign_worker_process'); events=[]
    replace_attempts=[]
    real_replace=process.os.replace

    def replace_with_one_transient_lock(source, destination):
        replace_attempts.append((source, destination))
        if len(replace_attempts)==1:
            raise PermissionError(13, 'status target is temporarily locked by the supervisor')
        return real_replace(source, destination)

    class Worker:
        def __init__(self, **_kwargs): pass
        def start(self): events.append('start')
        def status(self): return {
            'state':'failed','last_error':'RuntimeError',
            'worker_heartbeat_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
        }
        def stop(self): events.append('stop')

    class Event:
        def __init__(self): self.waits=0
        def set(self): pass
        def wait(self, _seconds):
            self.waits+=1
            return self.waits>1

    monkeypatch.setenv('TEST_WORKER_DSN','postgresql://fixture')
    monkeypatch.setattr(process,'load_config',lambda _path:{})
    monkeypatch.setattr(process,'PostgresWorkerStorage',lambda *_args,**_kwargs:object())
    monkeypatch.setattr(process,'AgentRefreshWorker',Worker)
    monkeypatch.setattr(process.threading,'Event',Event)
    monkeypatch.setattr(process.os,'replace',replace_with_one_transient_lock)
    monkeypatch.setattr(sys,'argv',[
        'campaign_worker_process.py','--config',str(tmp_path/'config.toml'),
        '--output-dir',str(tmp_path/'output'),'--tenant-id','tenant-a','--dsn-env','TEST_WORKER_DSN',
        '--status-file',str(tmp_path/'output'/'control'/'agent-workers'/'worker-1.json'),
    ])

    assert process.main()==2
    assert len(replace_attempts)==2
    assert events==['start','stop']
    status = __import__('json').loads((tmp_path/'output'/'control'/'agent-workers'/'worker-1.json').read_text(encoding='utf-8'))
    assert status['state']=='failed'
    assert status['last_error']=='RuntimeError'
    assert status['pid']==os.getpid()
    assert status['heartbeat_at']
    assert status['worker_heartbeat_at']


def test_supervisor_restarts_process_when_worker_thread_heartbeat_stalls(tmp_path):
    service = load("agent_collection_service")

    class Process:
        def __init__(self, pid): self.pid, self.returncode = pid, None
        def poll(self): return self.returncode
        def terminate(self): self.returncode = 0
        def wait(self, timeout=None): return self.returncode
        def kill(self): self.returncode = -9

    processes = []
    def spawn(_command, **_kwargs):
        process = Process(3000 + len(processes))
        processes.append(process)
        return process

    supervisor = service.CampaignWorkerSupervisor(
        ["python", "campaign_worker_process.py"], count=1, cwd=tmp_path,
        environment={}, status_dir=tmp_path / "stalled-status", spawn=spawn,
        monitor_interval=0.01, heartbeat_timeout=2, worker_heartbeat_timeout=0.05,
        restart_limit=1,
    )
    supervisor.start()
    try:
        first = processes[0]
        now = __import__('datetime').datetime.now(__import__('datetime').timezone.utc)
        (tmp_path / "stalled-status" / "worker-1.json").write_text(__import__('json').dumps({
            "schema": "amazon-campaign-worker-status-v1",
            "pid": first.pid,
            "state": "running",
            "heartbeat_at": now.isoformat(),
            "worker_heartbeat_at": (now - __import__('datetime').timedelta(seconds=1)).isoformat(),
            "last_error": None,
        }), encoding="utf-8")
        deadline = time.monotonic() + 2
        while len(processes) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(processes) == 2
        assert first.returncode == 0
        assert supervisor.status()["restart_count"] == 1
    finally:
        supervisor.stop()


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


def test_running_agent_reclaims_lease_that_expires_after_service_start():
    service=load('agent_collection_service')
    config={**load('amazon_us_worker').DEFAULTS,'max_actions_per_run':5,'raw_html_dir':None,'context':{},
            'proxy_url':'http://proxy.example:10000','proxy_session_ports':[10000,10001],
            'proxy_session_max_asins':5,'proxy_credential_generation':'test-generation-1'}
    class ExpiringStorage(RefreshStorage):
        def __init__(self):
            super().__init__(); self.claimed=True; self.expires=time.monotonic()+0.15; self.recovered=False
        def reclaim_expired_leases(self):
            if not self.recovered and time.monotonic()>=self.expires:
                self.claimed=False; self.recovered=True; return 1
            return 0
    storage=ExpiringStorage()
    storage.capacity_config_hash=load('proxy_canary').capacity_config_hash(config)
    background=service.AgentRefreshWorker(storage=storage,adapter_factory=Adapter,config=config,poll_seconds=0.05)
    background.start()
    try:
        deadline=time.monotonic()+2
        while not storage.finished and time.monotonic()<deadline: time.sleep(0.02)
        assert storage.finished==[('refresh-1','completed')]
    finally:
        background.stop()


def test_agent_worker_recovers_after_transient_postgres_operational_error():
    from psycopg.errors import ConnectionTimeout

    service=load('agent_collection_service')
    worker_module=load('amazon_us_worker')
    config={**worker_module.DEFAULTS,'max_actions_per_run':5,'raw_html_dir':None,'context':{},
            'proxy_url':'http://proxy.example:10000','proxy_session_ports':[10000,10001],
            'proxy_session_max_asins':5,'proxy_credential_generation':'test-generation-1'}

    class FlakyDatabaseStorage(RefreshStorage):
        def __init__(self):
            super().__init__(); self.reclaim_calls=0

        def reclaim_expired_leases(self):
            self.reclaim_calls += 1
            if self.reclaim_calls == 1:
                raise ConnectionTimeout('transient database connection failure')
            return 0

    storage=FlakyDatabaseStorage()
    storage.capacity_config_hash=load('proxy_canary').capacity_config_hash(config)
    background=service.AgentRefreshWorker(storage=storage,adapter_factory=Adapter,config=config,poll_seconds=0.01)
    background.start()
    try:
        deadline=time.monotonic()+2
        while not storage.finished and time.monotonic()<deadline: time.sleep(0.01)
        status=background.status()
        assert storage.finished==[('refresh-1','completed')]
        assert storage.reclaim_calls>=2
        assert status['state']=='running' and status['last_error'] is None
    finally:
        background.stop()


def test_agent_worker_fails_closed_for_unclassified_operational_error():
    import psycopg

    service=load('agent_collection_service')
    worker_module=load('amazon_us_worker')

    class NonTransientDatabaseStorage(FailingRefreshStorage):
        def __init__(self):
            super().__init__(); self.reclaim_calls=0

        def reclaim_expired_leases(self):
            self.reclaim_calls += 1
            raise psycopg.OperationalError('unclassified database configuration failure')

    storage=NonTransientDatabaseStorage()
    background=service.AgentRefreshWorker(
        storage=storage,adapter_factory=Adapter,config=dict(worker_module.DEFAULTS),poll_seconds=0.01
    )
    background.start()
    try:
        deadline=time.monotonic()+2
        while background.status()['state']!='failed' and time.monotonic()<deadline: time.sleep(0.01)
        status=background.status()
        assert status['state']=='failed' and status['last_error']=='OperationalError'
        assert storage.reclaim_calls==1
        assert storage.failure_reason=='agent_refresh_worker_failed'
    finally:
        background.stop()


def test_agent_worker_requeues_own_claim_after_transient_database_failure():
    from psycopg.errors import ConnectionTimeout

    service=load('agent_collection_service')
    worker_module=load('amazon_us_worker')
    config=dict(worker_module.DEFAULTS)

    class ClaimedStorage(RefreshStorage):
        def __init__(self):
            super().__init__(); self.requeue_calls=[]

        def requeue_claimed_refreshes(self, worker_id, reason):
            self.requeue_calls.append((worker_id,reason,self.claimed))
            if self.claimed:
                self.claimed=False
                return 1
            return 0

    storage=ClaimedStorage(); runs=[]

    def run_actions(current_storage, *_args, **_kwargs):
        runs.append(len(runs)+1)
        if len(runs)==1:
            storage.claimed=True
            raise ConnectionTimeout('connection lost after claim')
        storage.claimed=True
        current_storage.finish_refresh_request('refresh-1','completed')
        return 1

    background=service.AgentRefreshWorker(storage=storage,adapter_factory=Adapter,config=config,poll_seconds=0.01)
    with patch.object(service,'run_postgres_actions',side_effect=run_actions):
        background.start()
        try:
            deadline=time.monotonic()+2
            while not storage.finished and time.monotonic()<deadline: time.sleep(0.01)
            assert storage.finished==[('refresh-1','completed')]
            assert len(runs)==2
            assert any(was_claimed for _worker,_reason,was_claimed in storage.requeue_calls)
        finally:
            background.stop()


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


def test_capacity_blocked_worker_routes_database_timeout_through_recovery():
    from psycopg.errors import ConnectionTimeout

    service=load('agent_collection_service')
    worker_module=load('amazon_us_worker')
    config=dict(worker_module.DEFAULTS)
    config.update({'proxy_url':'http://proxy.example:10000','proxy_session_ports':[10000,10001],
                   'proxy_session_max_asins':5,'proxy_credential_generation':'test-generation-1'})

    class Storage(RefreshStorage):
        def __init__(self):
            super().__init__(); self.capacity_fresh=False; self.fail_reclaim=False; self.timeout_count=0

        def reclaim_expired_leases(self):
            if self.fail_reclaim:
                self.fail_reclaim=False; self.timeout_count+=1
                raise ConnectionTimeout('capacity recovery database timeout')
            return 0

    storage=Storage();storage.capacity_config_hash=load('proxy_canary').capacity_config_hash(config)
    background=service.AgentRefreshWorker(storage=storage,adapter_factory=Adapter,config=config,poll_seconds=0.01)
    background.start()
    try:
        deadline=time.monotonic()+2
        while background.status()['state']!='blocked' and time.monotonic()<deadline: time.sleep(0.01)
        assert background.status()['state']=='blocked'
        storage.fail_reclaim=True; storage.capacity_fresh=True; background.notify()
        deadline=time.monotonic()+2
        while not storage.finished and time.monotonic()<deadline: time.sleep(0.01)
        assert storage.timeout_count==1
        assert storage.finished==[('refresh-1','completed')]
        assert background.status()['state']=='running'
    finally:
        background.stop()


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


def test_service_owns_primary_console_lifecycle_and_operator_adapter(tmp_path):
    service=load('agent_collection_service'); captured={}; events=[]

    class Worker:
        def __init__(self,**_kwargs): pass
        def start(self): events.append('worker-start')
        def stop(self): events.append('worker-stop')
        def notify(self): pass
        def status(self): return {'state':'running'}

    class ApiServer:
        server_port=8765
        def __init__(self,*_args,**_kwargs): pass
        def serve_forever(self): events.append('api-serve')
        def server_close(self): events.append('api-close')

    class PrimaryConsole:
        server_port=8770
        def __init__(self,address,repository,**kwargs): captured.update(address=address,repository=repository,kwargs=kwargs)
        def serve_forever(self): events.append('console-serve')
        def shutdown(self): events.append('console-shutdown')
        def server_close(self): events.append('console-close')

    console_repository=object();reporting=object();storage=object()
    environment={'AMAZON_US_POSTGRES_DSN':'postgresql://fixture','AMAZON_COLLECTION_API_KEY':'fixture-key'}
    with (
        patch.dict(os.environ,environment,clear=False),
        patch.object(service,'load_config',return_value={}),
        patch.object(service,'PostgresWorkerStorage',return_value=storage),
        patch.object(service,'PostgresCollectionRepository',return_value=object()),
        patch.object(service,'PostgresConsoleRepository',return_value=console_repository),
        patch.object(service,'WatchDeliveryRepository',return_value=reporting),
        patch.object(service,'AgentRefreshWorker',Worker),
        patch.object(service,'CollectionServer',ApiServer),
        patch.object(service,'ConsoleServer',PrimaryConsole),
    ):
        assert service.main([
            '--tenant-id','tenant-agent','--config',str(tmp_path/'config.toml'),'--output-dir',str(tmp_path/'output'),
            '--console-port','8770','--console-tenant-id','amazon_us_main','--operator-base-url','http://127.0.0.1:8772',
        ])==0

    assert captured['address']==('127.0.0.1',8770)
    assert captured['repository'] is console_repository
    assert captured['kwargs']['operator_base_url']=='http://127.0.0.1:8772'
    assert captured['kwargs']['api_key']=='fixture-key'
    assert {'console-serve','console-shutdown','console-close','worker-start','worker-stop'} <= set(events)


def test_console_bind_failure_closes_api_before_any_worker_process_starts(tmp_path):
    service = load("agent_collection_service")
    events = []

    class ApiServer:
        server_port = 8765
        def __init__(self, *_args, **_kwargs): pass
        def server_close(self): events.append("api-close")

    environment = {"AMAZON_US_POSTGRES_DSN": "postgresql://fixture", "AMAZON_COLLECTION_API_KEY": "fixture-key"}
    with (
        patch.dict(os.environ, environment, clear=False),
        patch.object(service, "load_config", return_value={}),
        patch.object(service, "PostgresWorkerStorage", return_value=object()),
        patch.object(service, "PostgresCollectionRepository", return_value=object()),
        patch.object(service, "PostgresConsoleRepository", return_value=object()),
        patch.object(service, "WatchDeliveryRepository", return_value=object()),
        patch.object(service, "effective_worker_count", return_value=2),
        patch.object(service, "CollectionServer", ApiServer),
        patch.object(service, "ConsoleServer", side_effect=OSError("address in use")),
        patch.object(service.subprocess, "Popen") as popen,
    ):
        with pytest.raises(OSError, match="address in use"):
            service.main([
                "--tenant-id", "tenant-agent", "--config", str(tmp_path / "config.toml"),
                "--output-dir", str(tmp_path / "output"), "--worker-count", "2",
                "--console-port", "8770", "--operator-base-url", "http://127.0.0.1:8772",
            ])

    popen.assert_not_called()
    assert events == ["api-close"]


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
