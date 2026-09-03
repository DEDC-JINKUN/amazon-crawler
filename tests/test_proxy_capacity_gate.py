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
        "proxy_credential_generation": "test-generation-1",
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
        "slot_budget": 3,
        "slot_capacity": 9,
        "capacity_gate_status": "allowed",
        "capacity_gate_reason": "capacity_sufficient",
        "credential_generation": "test-generation-1",
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
        ({"requested_capacity": 3, "required_slots": 1}, 20, "canary_scope_insufficient"),
        ({"unique_egress_count": None, "slot_capacity": None}, 3, "capacity_fact_inconsistent"),
        ({"canary_status": "unknown", "unique_egress_count": None, "slot_capacity": None, "capacity_gate_reason": "credentials_missing"}, 3, "credentials_missing"),
        ({"canary_status": "partial", "available_slots": 1, "unique_egress_count": 1, "slot_capacity": 3, "requested_capacity": 3, "required_slots": 1}, 9, "canary_scope_insufficient"),
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

        def reserve_proxy_capacity(self, **kwargs):
            return {"status": "denied", "reason": "canary_scope_insufficient", "reservation_id": kwargs["reservation_id"]}

        def validate_proxy_capacity_reservation(self, *_args, **_kwargs):
            raise AssertionError("denied reservation must not validate")

        def release_proxy_capacity(self, *_args):
            raise AssertionError("denied reservation must not release")

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
    with pytest.raises(worker.ProxyCapacityGateDenied, match="canary_scope_insufficient"):
        worker.run_postgres_actions(
            storage,
            adapter,
            cfg,
            limit=20,
            run_id="run-capacity-denied",
            worker_id="worker-a",
            )

    assert storage.claims == 0
    assert adapter.begins == 0
    assert adapter.fetches == 0


def test_capacity_reservation_failure_is_redacted_to_a_stable_denial():
    module = load("proxy_capacity_gate")

    class Storage:
        def reserve_proxy_capacity(self, **_kwargs):
            raise RuntimeError("private database detail")

    with pytest.raises(module.ProxyCapacityGateDenied, match="capacity_reservation_unavailable") as caught:
        module.acquire_capacity_reservation(
            Storage(), config(), requested_actions=3, owner_id="worker-a", lease_seconds=600
        )

    assert "private" not in str(caught.value)


def test_unknown_denied_fact_with_sufficient_counts_still_fails_closed():
    module = load("proxy_capacity_gate")
    cfg = config()
    inconsistent = fact(
        module,
        cfg,
        canary_status="unknown",
        capacity_gate_status="denied",
        capacity_gate_reason="credentials_missing",
    )

    decision = module.evaluate_capacity_fact(inconsistent, cfg, requested_actions=9)

    assert decision["status"] == "denied"
    assert decision["reason"] == "canary_fact_denied"


@pytest.mark.parametrize(
    "overrides",
    [
        {"tested_slots": 2, "available_slots": 3},
        {"available_slots": 3, "unique_egress_count": 2, "duplicate_egress_count": 0},
        {"unique_egress_count": 2, "slot_capacity": 9},
        {"requested_capacity": 9, "required_slots": 2},
        {"canary_status": "succeeded", "unique_egress_count": 2, "duplicate_egress_count": 1, "slot_capacity": 6},
    ],
)
def test_cross_field_inconsistent_capacity_facts_fail_closed(overrides):
    module = load("proxy_capacity_gate")
    cfg = config()

    decision = module.evaluate_capacity_fact(fact(module, cfg, **overrides), cfg, requested_actions=3)

    assert decision["status"] == "denied"
    assert decision["reason"] == "capacity_fact_inconsistent"


def test_capacity_acquisition_delegates_one_atomic_reservation_with_safe_identity():
    module = load("proxy_capacity_gate")
    cfg = config(proxy_session_max_asins=3)

    class Storage:
        def __init__(self): self.calls = []

        def reserve_proxy_capacity(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "status": "active",
                "reason": "capacity_reserved",
                "reservation_id": kwargs["reservation_id"],
                "owner_id": kwargs["owner_id"],
                "canary_operation_id": "op-canary-1",
                "capacity_config_hash": kwargs["capacity_config_hash"],
                "credential_generation": kwargs["credential_generation"],
                "requested_capacity": kwargs["requested_capacity"],
                "required_slots": kwargs["required_slots"],
                "reserved_slots": 2,
                "slot_ids": ["session-01", "session-03"],
                "fact_finished_at": "2026-09-03T01:00:00+00:00",
                "fact_expires_at": "2026-09-03T02:00:00+00:00",
                "reservation_expires_at": "2026-09-03T01:10:00+00:00",
                "capacity_snapshot": {"unique_egress_count": 3, "slot_capacity": 9},
            }

    storage = Storage()
    reservation = module.acquire_capacity_reservation(
        storage,
        cfg,
        requested_actions=6,
        owner_id="worker-a",
        lease_seconds=600,
        reservation_id="reservation-1",
    )

    assert reservation["reservation_id"] == "reservation-1"
    assert reservation["canary_operation_id"] == "op-canary-1"
    assert reservation["slot_ids"] == ["session-01", "session-03"]
    assert storage.calls == [{
        "reservation_id": "reservation-1",
        "owner_id": "worker-a",
        "capacity_config_hash": module.capacity_config_hash(cfg),
        "credential_generation": "test-generation-1",
        "requested_capacity": 6,
        "required_slots": 2,
        "slot_budget": 3,
        "max_age_seconds": 3600,
        "lease_seconds": 600,
    }]
    assert "proxy.example" not in repr(reservation)


def test_atomic_reservation_denial_raises_only_the_stable_reason():
    module = load("proxy_capacity_gate")

    class Storage:
        def reserve_proxy_capacity(self, **kwargs):
            return {
                "status": "denied",
                "reason": "capacity_reserved_elsewhere",
                "reservation_id": kwargs["reservation_id"],
                "canary_operation_id": "op-canary-1",
            }

    with pytest.raises(module.ProxyCapacityGateDenied, match="capacity_reserved_elsewhere") as caught:
        module.acquire_capacity_reservation(
            Storage(), config(), requested_actions=3, owner_id="worker-a", lease_seconds=600
        )
    assert "proxy" not in str(caught.value).replace("proxy_capacity_gate_denied", "")


def test_production_runner_reserves_validates_before_claim_and_releases_capacity():
    worker = load("amazon_us_worker")
    cfg = {**worker.DEFAULTS, **config(), "max_actions_per_run": 3}
    events = []
    reservation = {
        "status": "active", "reason": "capacity_reserved", "reservation_id": "reservation-1",
        "owner_id": "worker-a", "canary_operation_id": "op-canary-1",
        "capacity_config_hash": load("proxy_canary").capacity_config_hash(cfg),
        "credential_generation": "test-generation-1", "requested_capacity": 3,
        "required_slots": 1, "reserved_slots": 1, "slot_ids": ["session-02"],
        "fact_finished_at": "2026-09-03T01:00:00+00:00",
        "fact_expires_at": "2026-09-03T02:00:00+00:00",
        "reservation_expires_at": "2026-09-03T01:10:00+00:00",
        "capacity_snapshot": {"unique_egress_count": 3, "slot_capacity": 9},
    }

    class Storage:
        tenant_id = "tenant-a"

        def reserve_proxy_capacity(self, **_kwargs): events.append("reserve"); return dict(reservation)
        def validate_proxy_capacity_reservation(self, *_args, **_kwargs): events.append("validate"); return dict(reservation)
        def claim_task(self, *_args, **_kwargs): events.append("claim"); return None
        def release_proxy_capacity(self, *_args): events.append("release"); return True

    class Adapter:
        def configure_capacity_reservation(self, slot_ids, validator):
            events.append(("configure", tuple(slot_ids)))
            self.validator = validator

        def begin_run(self, *_args): events.append("begin")
        def close(self): return None

    assert worker.run_postgres_actions(
        Storage(), Adapter(), cfg, limit=3, run_id="run-1", worker_id="worker-a",
    ) == 0
    assert events == ["reserve", ("configure", ("session-02",)), "begin", "validate", "claim", "release"]


def test_expired_reservation_stops_before_claim_and_fetch():
    worker = load("amazon_us_worker")
    cfg = {**worker.DEFAULTS, **config(), "max_actions_per_run": 3}

    class Storage:
        tenant_id = "tenant-a"
        claims = 0

        def reserve_proxy_capacity(self, **kwargs):
            return {
                "status": "active", "reason": "capacity_reserved", "reservation_id": kwargs["reservation_id"],
                "owner_id": kwargs["owner_id"], "canary_operation_id": "op-canary-1",
                "capacity_config_hash": kwargs["capacity_config_hash"], "credential_generation": kwargs["credential_generation"],
                "requested_capacity": 3, "required_slots": 1, "reserved_slots": 1,
                "slot_ids": ["session-01"], "fact_finished_at": "2026-09-03T01:00:00+00:00",
                "fact_expires_at": "2026-09-03T02:00:00+00:00", "reservation_expires_at": "2026-09-03T01:10:00+00:00",
                "capacity_snapshot": {"unique_egress_count": 3, "slot_capacity": 9},
            }

        def validate_proxy_capacity_reservation(self, *_args, **_kwargs):
            return {"status": "denied", "reason": "capacity_evidence_stale", "reservation_id": "reservation-1"}

        def claim_task(self, *_args, **_kwargs): self.claims += 1; return None
        def release_proxy_capacity(self, *_args): return True

    class Adapter:
        fetches = 0
        def configure_capacity_reservation(self, _slots, _validator): return None
        def begin_run(self, *_args): return None
        def fetch(self, _url): self.fetches += 1

    storage = Storage()
    adapter = Adapter()
    with pytest.raises(worker.ProxyCapacityGateDenied, match="capacity_evidence_stale"):
        worker.run_postgres_actions(
            storage, adapter, cfg, limit=3, run_id="run-1", worker_id="worker-a"
        )
    assert storage.claims == 0
    assert adapter.fetches == 0
