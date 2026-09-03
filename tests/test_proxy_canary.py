from __future__ import annotations

import importlib.util
import re
import socket
import ssl
import urllib.error
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "proxy_canary.py"


def load_module():
    spec = importlib.util.spec_from_file_location("proxy_canary_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def config(**overrides):
    value = {
        "egress_profile": "proxy_sessions",
        "proxy_url": "http://proxy.example:10000",
        "proxy_username_env": "PROXY_USER",
        "proxy_password_env": "PROXY_PASS",
        "proxy_session_ports": [10000, 10001, 10002],
        "proxy_session_max_asins": 3,
        "proxy_product_session_scope": "bounded",
        "proxy_canary_url": "https://api.ipify.org?format=json",
        "proxy_canary_timeout_seconds": 5,
        "proxy_credential_generation": "test-generation-1",
    }
    value.update(overrides)
    return value


def test_all_sessions_succeed_and_public_result_contains_only_redacted_capacity(monkeypatch):
    module = load_module()
    monkeypatch.setenv("PROXY_USER", "private-user")
    monkeypatch.setenv("PROXY_PASS", "private-pass")
    monkeypatch.setenv("AMAZON_PROXY_CREDENTIAL_GENERATION", "test-generation-1")
    responses = iter(
        [
            {"ok": True, "egress_ip": "203.0.113.10", "http_status": 200, "latency_ms": 30.0},
            {"ok": True, "egress_ip": "203.0.113.11", "http_status": 200, "latency_ms": 10.0},
            {"ok": True, "egress_ip": "203.0.113.12", "http_status": 200, "latency_ms": 20.0},
        ]
    )

    result = module.run_proxy_canary(config(), requested_actions=9, probe_slot=lambda **_kwargs: next(responses))

    assert result == {
        "schema_version": "amazon-us-proxy-canary-v1",
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
        "p95_latency_ms": 30.0,
        "config_hash": module.capacity_config_hash(config()),
        "sessions": [
            {"session_id": "session-01", "status": "available", "usable": True, "auth_status": "succeeded", "connect_tls_status": "succeeded", "error_class": None, "http_status": 200, "latency_ms": 30.0},
            {"session_id": "session-02", "status": "available", "usable": True, "auth_status": "succeeded", "connect_tls_status": "succeeded", "error_class": None, "http_status": 200, "latency_ms": 10.0},
            {"session_id": "session-03", "status": "available", "usable": True, "auth_status": "succeeded", "connect_tls_status": "succeeded", "error_class": None, "http_status": 200, "latency_ms": 20.0},
        ],
    }
    rendered = repr(result)
    for forbidden in ("203.0.113", "10000", "10001", "10002", "private-user", "private-pass", "proxy.example"):
        assert forbidden not in rendered


def test_per_asin_product_scope_without_retry_counts_one_action_per_unique_proxy_session(monkeypatch):
    module = load_module()
    cfg = config(proxy_product_session_scope="per_asin", proxy_session_retry_per_asin=0)
    monkeypatch.setenv("PROXY_USER", "fixture-user")
    monkeypatch.setenv("PROXY_PASS", "fixture-pass")
    identities = iter(["203.0.113.10", "203.0.113.11", "203.0.113.12"])

    result = module.run_proxy_canary(
        cfg, requested_actions=3,
        probe_slot=lambda **_kwargs: {
            "ok": True, "egress_ip": next(identities), "http_status": 200,
            "latency_ms": 10.0, "auth_status": "succeeded", "connect_tls_status": "succeeded",
            "error_class": None,
        },
    )

    assert result["slot_budget"] == 1
    assert result["required_slots"] == 3
    assert result["slot_capacity"] == 3
    assert result["capacity_gate_status"] == "allowed"


def test_per_asin_canary_denies_twenty_actions_when_only_thirty_four_recovery_slots_exist(monkeypatch):
    module = load_module()
    cfg = config(
        proxy_product_session_scope="per_asin",
        proxy_session_retry_per_asin=1,
        proxy_session_ports=list(range(10000, 10034)),
    )
    monkeypatch.setenv("PROXY_USER", "fixture-user")
    monkeypatch.setenv("PROXY_PASS", "fixture-pass")
    identity = iter(f"203.0.113.{index}" for index in range(1, 35))

    result = module.run_proxy_canary(
        cfg, requested_actions=20,
        probe_slot=lambda **_kwargs: {
            "ok": True, "egress_ip": next(identity), "http_status": 200,
            "latency_ms": 10.0, "auth_status": "succeeded", "connect_tls_status": "succeeded",
            "error_class": None,
        },
    )

    assert result["required_slots"] == 20
    assert result["unique_egress_count"] == 34
    assert result["capacity_gate_status"] == "denied"
    assert result["capacity_gate_reason"] == "replacement_capacity_insufficient"


def test_partial_port_failure_denies_when_productive_capacity_lacks_reserved_replacement(monkeypatch):
    module = load_module()
    monkeypatch.setenv("PROXY_USER", "private-user")
    monkeypatch.setenv("PROXY_PASS", "private-pass")
    responses = iter(
        [
            {"ok": True, "egress_ip": "203.0.113.20", "http_status": 200, "latency_ms": 15.0},
            {"ok": False, "error_class": "connect_failed", "auth_status": "unknown", "connect_tls_status": "failed", "http_status": None, "latency_ms": 50.0},
            {"ok": True, "egress_ip": "203.0.113.21", "http_status": 200, "latency_ms": 25.0},
        ]
    )

    result = module.run_proxy_canary(config(), requested_actions=6, probe_slot=lambda **_kwargs: next(responses))

    assert result["canary_status"] == "partial"
    assert result["available_slots"] == 2
    assert result["unique_egress_count"] == 2
    assert result["slot_capacity"] == 6
    assert result["capacity_gate_status"] == "denied"
    assert result["capacity_gate_reason"] == "replacement_capacity_insufficient"
    assert result["sessions"][1] == {
        "session_id": "session-02",
        "status": "unavailable",
        "usable": False,
        "auth_status": "unknown",
        "connect_tls_status": "failed",
        "error_class": "connect_failed",
        "http_status": None,
        "latency_ms": 50.0,
    }


def test_missing_credentials_is_a_denied_unknown_fact_and_never_calls_the_probe(monkeypatch):
    module = load_module()
    monkeypatch.delenv("PROXY_USER", raising=False)
    monkeypatch.delenv("PROXY_PASS", raising=False)
    called = False

    def probe(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("probe must not run without credentials")

    result = module.run_proxy_canary(config(), requested_actions=3, probe_slot=probe)

    assert called is False
    assert result["canary_status"] == "unknown"
    assert result["tested_slots"] == 0
    assert result["available_slots"] is None
    assert result["unique_egress_count"] is None
    assert result["slot_capacity"] is None
    assert result["capacity_gate_status"] == "denied"
    assert result["capacity_gate_reason"] == "credentials_missing"
    assert result["sessions"] == []


def test_duplicate_egress_is_compared_only_in_memory_and_reduces_usable_capacity(monkeypatch):
    module = load_module()
    monkeypatch.setenv("PROXY_USER", "private-user")
    monkeypatch.setenv("PROXY_PASS", "private-pass")
    responses = iter(
        [
            {"ok": True, "egress_ip": "203.0.113.30", "http_status": 200, "latency_ms": 10.0},
            {"ok": True, "egress_ip": "203.0.113.30", "http_status": 200, "latency_ms": 11.0},
            {"ok": True, "egress_ip": "203.0.113.31", "http_status": 200, "latency_ms": 12.0},
        ]
    )

    result = module.run_proxy_canary(config(), requested_actions=9, probe_slot=lambda **_kwargs: next(responses))

    assert result["available_slots"] == 3
    assert result["unique_egress_count"] == 2
    assert result["duplicate_egress_count"] == 1
    assert result["slot_capacity"] == 6
    assert result["canary_status"] == "partial"
    assert result["capacity_gate_status"] == "denied"
    assert result["capacity_gate_reason"] == "duplicate_egress_capacity_insufficient"
    assert [item["usable"] for item in result["sessions"]] == [True, False, True]
    assert "203.0.113" not in repr(result)


def test_credential_generation_changes_hash_without_hashing_secret_values(monkeypatch):
    module = load_module()
    monkeypatch.setenv("PROXY_USER", "private-user-v1")
    monkeypatch.setenv("PROXY_PASS", "private-pass-v1")
    first = module.capacity_config_hash(config(proxy_credential_generation="vault-generation-1"))
    monkeypatch.setenv("PROXY_USER", "private-user-v2")
    monkeypatch.setenv("PROXY_PASS", "private-pass-v2")
    second = module.capacity_config_hash(config(proxy_credential_generation="vault-generation-2"))

    assert first != second
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    with pytest.raises(ValueError, match="credential generation"):
        module.capacity_config_hash(config(proxy_credential_generation=""))


def test_retry_policy_change_invalidates_prior_canary_config_hash():
    module = load_module()

    without_retry = module.capacity_config_hash(config(proxy_session_retry_per_asin=0))
    with_retry = module.capacity_config_hash(config(proxy_session_retry_per_asin=1))

    assert without_retry != with_retry


def test_physical_resource_slots_are_stable_across_capacity_policy_and_credential_rotation():
    module = load_module()
    first = module.capacity_resource_slot_ids(config(
        proxy_session_ports=[10000, 10001],
        proxy_session_max_asins=1,
        proxy_canary_timeout_seconds=5,
        proxy_credential_generation="vault-generation-1",
    ))
    second = module.capacity_resource_slot_ids(config(
        proxy_session_ports=[10000, 10001],
        proxy_session_max_asins=5,
        proxy_canary_timeout_seconds=30,
        proxy_credential_generation="vault-generation-2",
    ))

    assert first == second
    assert len(first) == 2
    assert len(set(first)) == 2
    assert all(value.startswith("resource-") and len(value) == 73 for value in first)
    assert "proxy.example" not in repr(first)


def test_all_sessions_failed_including_timeout_reports_known_zero_without_success(monkeypatch):
    module = load_module()
    monkeypatch.setenv("PROXY_USER", "private-user")
    monkeypatch.setenv("PROXY_PASS", "private-pass")
    responses = iter(
        [
            {"ok": False, "error_class": "proxy_auth_failed", "auth_status": "failed", "connect_tls_status": "unknown", "http_status": 407, "latency_ms": 5.0},
            {"ok": False, "error_class": "timeout", "auth_status": "unknown", "connect_tls_status": "failed", "http_status": None, "latency_ms": 5000.0},
            {"ok": False, "error_class": "tls_failed", "auth_status": "succeeded", "connect_tls_status": "failed", "http_status": None, "latency_ms": 20.0},
        ]
    )

    result = module.run_proxy_canary(config(), requested_actions=3, probe_slot=lambda **_kwargs: next(responses))

    assert result["canary_status"] == "failed"
    assert result["tested_slots"] == 3
    assert result["available_slots"] == 0
    assert result["unique_egress_count"] == 0
    assert result["slot_capacity"] == 0
    assert result["capacity_gate_status"] == "denied"
    assert result["capacity_gate_reason"] == "unique_capacity_insufficient"
    assert [item["error_class"] for item in result["sessions"]] == ["proxy_auth_failed", "timeout", "tls_failed"]


def test_canary_rejects_amazon_targets_before_any_probe(monkeypatch):
    module = load_module()
    monkeypatch.setenv("PROXY_USER", "private-user")
    monkeypatch.setenv("PROXY_PASS", "private-pass")
    called = False

    def probe(**_kwargs):
        nonlocal called
        called = True

    with pytest.raises(ValueError, match="approved non-Amazon allowlist"):
        module.run_proxy_canary(
            config(proxy_canary_url="https://www.amazon.com/robots.txt"),
            requested_actions=3,
            probe_slot=probe,
        )
    assert called is False


@pytest.mark.parametrize(
    "target",
    [
        "https://www.amazon.co.uk/robots.txt",
        "https://example.com/ip",
        "http://api.ipify.org?format=json",
        "https://api.ipify.org.evil.example/ip",
    ],
)
def test_canary_accepts_only_the_exact_non_amazon_https_allowlist(target, monkeypatch):
    module = load_module()
    monkeypatch.setenv("PROXY_USER", "private-user")
    monkeypatch.setenv("PROXY_PASS", "private-pass")
    called = False

    def probe(**_kwargs):
        nonlocal called
        called = True

    with pytest.raises(ValueError, match="approved non-Amazon allowlist"):
        module.run_proxy_canary(config(proxy_canary_url=target), requested_actions=3, probe_slot=probe)
    assert called is False


def test_probe_slot_proves_authenticated_https_tunnel_and_keeps_ip_internal():
    module = load_module()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"ip":"203.0.113.99"}'

        def getcode(self):
            return 200

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://api.ipify.org?format=json"
            assert timeout == 5
            return Response()

    captured = []

    def opener_factory(*handlers):
        captured.extend(handlers)
        return Opener()

    result = module.probe_proxy_slot(
        proxy_url="http://proxy.example:10000",
        target_url="https://api.ipify.org?format=json",
        timeout_seconds=5,
        username="private-user",
        password="private-pass",
        opener_factory=opener_factory,
        clock=iter([1.0, 1.025]).__next__,
    )

    assert result == {
        "ok": True,
        "egress_ip": "203.0.113.99",
        "http_status": 200,
        "latency_ms": 25.0,
        "auth_status": "succeeded",
        "connect_tls_status": "succeeded",
        "error_class": None,
    }
    assert any(handler.__class__.__name__ == "ProxyTunnelAuthHTTPSHandler" for handler in captured)


def test_probe_slot_rejects_a_redirected_final_amazon_url_without_exposing_ip():
    module = load_module()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _limit): return b'{"ip":"203.0.113.99"}'
        def getcode(self): return 200
        def geturl(self): return "https://www.amazon.com/redirected"

    class Opener:
        def open(self, _request, timeout):
            assert timeout == 5
            return Response()

    result = module.probe_proxy_slot(
        proxy_url="http://proxy.example:10000",
        target_url="https://api.ipify.org?format=json",
        timeout_seconds=5,
        username="private-user",
        password="private-pass",
        opener_factory=lambda *_handlers: Opener(),
        clock=iter([1.0, 1.010]).__next__,
    )

    assert result == {
        "ok": False,
        "http_status": 200,
        "latency_ms": 10.0,
        "auth_status": "succeeded",
        "connect_tls_status": "succeeded",
        "error_class": "redirect_not_allowed",
    }
    assert "203.0.113" not in repr(result)


@pytest.mark.parametrize(
    ("failure", "expected_class", "expected_status", "expected_auth"),
    [
        (urllib.error.HTTPError("https://api.ipify.org", 407, "proxy auth", {}, None), "proxy_auth_failed", 407, "failed"),
        (TimeoutError("private timeout detail"), "timeout", None, "unknown"),
        (ssl.SSLError("private tls detail"), "tls_failed", None, "unknown"),
        (urllib.error.URLError(socket.gaierror(11001, "private dns detail")), "dns_failed", None, "unknown"),
    ],
)
def test_probe_slot_classifies_failures_without_exception_detail(failure, expected_class, expected_status, expected_auth):
    module = load_module()

    class Opener:
        def open(self, _request, timeout):
            assert timeout == 5
            raise failure

    result = module.probe_proxy_slot(
        proxy_url="http://proxy.example:10000",
        target_url="https://api.ipify.org?format=json",
        timeout_seconds=5,
        username="private-user",
        password="private-pass",
        opener_factory=lambda *_handlers: Opener(),
        clock=iter([1.0, 1.050]).__next__,
    )

    assert result == {
        "ok": False,
        "http_status": expected_status,
        "latency_ms": 50.0,
        "auth_status": expected_auth,
        "connect_tls_status": "unknown" if expected_class == "proxy_auth_failed" else "failed",
        "error_class": expected_class,
    }
    assert "private" not in repr(result)


def test_probe_slot_rejects_success_response_without_a_single_ip_identity():
    module = load_module()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _limit): return b'{"message":"ok"}'
        def getcode(self): return 200

    class Opener:
        def open(self, _request, timeout):
            assert timeout == 5
            return Response()

    result = module.probe_proxy_slot(
        proxy_url="http://proxy.example:10000",
        target_url="https://api.ipify.org?format=json",
        timeout_seconds=5,
        username="private-user",
        password="private-pass",
        opener_factory=lambda *_handlers: Opener(),
        clock=iter([1.0, 1.010]).__next__,
    )

    assert result["ok"] is False
    assert result["error_class"] == "invalid_canary_response"
    assert result["auth_status"] == "succeeded"
    assert result["connect_tls_status"] == "succeeded"
    assert "egress_ip" not in result


def test_execute_canary_records_the_same_sanitized_fact_in_operation_ledger(tmp_path, monkeypatch):
    module = load_module()
    config_path = tmp_path / "worker.toml"
    config_path.write_text(
        "[worker]\n"
        "egress_profile='proxy_sessions'\n"
        "proxy_url='http://proxy.example:10000'\n"
        "proxy_username_env='PROXY_USER'\n"
        "proxy_password_env='PROXY_PASS'\n"
        "proxy_session_ports=[10000]\n"
        "proxy_session_max_asins=3\n"
        "proxy_product_session_scope='bounded'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROXY_USER", "private-user")
    monkeypatch.setenv("PROXY_PASS", "private-pass")
    monkeypatch.setenv("AMAZON_PROXY_CREDENTIAL_GENERATION", "test-generation-1")

    class Cursor:
        rowcount = 1

        def __init__(self): self.executed = []
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, sql, params=()): self.executed.append((sql, tuple(params)))
        def fetchone(self): return None

    class Connection:
        def __init__(self): self.cursor_instance = Cursor(); self.commits = 0
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def cursor(self): return self.cursor_instance
        def commit(self): self.commits += 1

    connection = Connection()
    result = module.execute_canary(
        config_path,
        tenant_id="tenant-a",
        requested_actions=3,
        operation_id="op-canary-1",
        connect=lambda: connection,
        probe_slot=lambda **_kwargs: {
            "ok": True,
            "egress_ip": "203.0.113.44",
            "http_status": 200,
            "latency_ms": 12.0,
        },
    )

    assert result["capacity_gate_status"] == "allowed"
    assert connection.commits == 3
    sql = "\n".join(statement for statement, _ in connection.cursor_instance.executed)
    assert "INSERT INTO amazon_us.operation_run" in sql
    assert "capacity_detail_json" in sql
    persisted = repr(connection.cursor_instance.executed[-1][1])
    assert "203.0.113.44" not in persisted
    assert "private-user" not in persisted
    assert "private-pass" not in persisted
    assert "proxy.example" not in persisted
