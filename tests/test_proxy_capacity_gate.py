from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def config(**overrides):
    value = {
        "proxy_url": "http://proxy.example:10000",
        "proxy_username_env": "PROXY_USER",
        "proxy_password_env": "PROXY_PASS",
        "proxy_session_ports": [10000, 10001, 10002],
        "proxy_session_max_asins": 3,
        "proxy_canary_url": "https://api.ipify.org?format=json",
        "proxy_canary_max_age_seconds": 3600,
    }
    value.update(overrides)
    return value


def fact(module, cfg, **overrides):
    value = {
        "canary_status": "succeeded",
        "planned_slots": 3,
        "tested_slots": 3,
        "available_slots": 3,
        "unique_egress_count": 3,
        "duplicate_egress_count": 0,
        "requested_capacity": 9,
        "required_slots": 3,
        "slot_capacity": 9,
        "capacity_gate_status": "allowed",
        "capacity_gate_reason": "capacity_sufficient",
        "capacity_config_hash": module.capacity_config_hash(cfg),
        "is_fresh": True,
    }
    value.update(overrides)
    return value


def test_capacity_gate_allows_only_fresh_matching_evidence_covering_the_requested_stage():
    module = load("proxy_capacity_gate")
    cfg = config()

    decision = module.evaluate_capacity_fact(fact(module, cfg), cfg, requested_actions=9)

    assert decision == {
        "status": "allowed",
        "reason": "capacity_sufficient",
        "requested_capacity": 9,
        "required_slots": 3,
        "available_unique_slots": 3,
        "slot_capacity": 9,
    }


@pytest.mark.parametrize(
    ("overrides", "requested", "reason"),
    [
        ({"is_fresh": False}, 3, "capacity_evidence_stale"),
        ({"capacity_config_hash": "b" * 64}, 3, "capacity_config_mismatch"),
        ({"requested_capacity": 3}, 20, "canary_scope_insufficient"),
        ({"unique_egress_count": None, "slot_capacity": None}, 3, "capacity_unknown"),
        ({"canary_status": "unknown", "unique_egress_count": None, "slot_capacity": None, "capacity_gate_reason": "credentials_missing"}, 3, "credentials_missing"),
        ({"unique_egress_count": 1, "slot_capacity": 3}, 9, "unique_capacity_insufficient"),
    ],
)
def test_capacity_gate_denies_with_stable_auditable_reason(overrides, requested, reason):
    module = load("proxy_capacity_gate")
    cfg = config()

    decision = module.evaluate_capacity_fact(fact(module, cfg, **overrides), cfg, requested_actions=requested)

    assert decision["status"] == "denied"
    assert decision["reason"] == reason
    assert "proxy.example" not in repr(decision)


def test_capacity_gate_denies_missing_fact_and_missing_session_configuration():
    module = load("proxy_capacity_gate")

    assert module.evaluate_capacity_fact(None, config(), requested_actions=3)["reason"] == "capacity_evidence_missing"
    assert module.evaluate_capacity_fact(None, config(proxy_session_ports=[]), requested_actions=3)["reason"] == "proxy_sessions_not_configured"


def test_production_runner_denies_before_adapter_begin_claim_or_amazon_fetch():
    gate = load("proxy_capacity_gate")
    worker = load("amazon_us_worker")
    cfg = {**worker.DEFAULTS, **config(), "max_actions_per_run": 20}

    class Storage:
        tenant_id = "tenant-a"
        claims = 0

        def load_latest_proxy_capacity(self, *, max_age_seconds):
            return fact(gate, cfg, unique_egress_count=1, slot_capacity=3, requested_capacity=20)

        def claim_task(self, *_args, **_kwargs):
            self.claims += 1
            raise AssertionError("capacity denial must happen before task claim")

    class Adapter:
        begins = 0
        fetches = 0

        def begin_run(self, *_args):
            self.begins += 1

        def fetch(self, _url):
            self.fetches += 1
            raise AssertionError("capacity denial must happen before Amazon fetch")

    storage = Storage()
    adapter = Adapter()
    with pytest.raises(worker.ProxyCapacityGateDenied, match="unique_capacity_insufficient"):
        worker.run_postgres_actions(
            storage,
            adapter,
            cfg,
            limit=20,
            run_id="run-capacity-denied",
            worker_id="worker-a",
            enforce_capacity_gate=True,
        )

    assert storage.claims == 0
    assert adapter.begins == 0
    assert adapter.fetches == 0


def test_capacity_fact_read_failure_is_redacted_to_a_stable_denial():
    module = load("proxy_capacity_gate")

    class Storage:
        def load_latest_proxy_capacity(self, *, max_age_seconds):
            raise RuntimeError("private database detail")

    with pytest.raises(module.ProxyCapacityGateDenied, match="capacity_fact_unavailable") as caught:
        module.enforce_capacity_gate(Storage(), config(), requested_actions=3)

    assert "private" not in str(caught.value)
