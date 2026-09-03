from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collection_console.py"


def load_module():
    spec = importlib.util.spec_from_file_location("collection_console_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Repository:
    tenant_id = "tenant-console"

    def __init__(self):
        self.list_args = None

    def load_overview(self, raw_html_dir=None):
        return {
            "tenant_id": self.tenant_id,
            "status_counts": {"pending": 2, "blocked": 1},
            "stage_counts": {"product": 3},
            "progress": {"total": 3, "processed": 1},
        }

    def load_identity(self):
        return {"tenant_id": self.tenant_id, "task_count": 3}

    def list_items(self, *, status=None, stage=None, query=None, limit=100, offset=0):
        self.list_args = {
            "status": status,
            "stage": stage,
            "query": query,
            "limit": limit,
            "offset": offset,
        }
        return {
            "total": 1,
            "items": [{"asin": "B00RCPDCQU", "status": status or "pending", "task_stage": stage or "product"}],
        }

    def load_detail(self, asin):
        if asin != "B00RCPDCQU":
            return None
        return {
            "asin": asin,
            "task": {"status": "reviews_pending"},
            "product": {"title": "Example"},
            "media": [{"asset_url": "https://images.example/item.jpg"}],
            "top_reviews": [{"title": "Good", "body": "Works"}],
            "evidence": [{"raw_html_path": "US/B00RCPDCQU/example.html"}],
        }

    def list_runs(self, limit=20):
        return [{"run_id": "run-1", "evidence_actions": 2, "blocked": 0}]

    def list_operations(self, limit=100):
        return [{"operation_id": "op-1", "operation_type": "run", "status": "failed", "preflight_status": "failed"}]

    def load_run(self, run_id):
        if run_id != "run-1":
            return None
        return {
            "run_id": run_id,
            "recorded_actions": 2,
            "inferred_actions": 1,
            "items": [
                {"asin": "B00RCPDCQU", "outcome": "succeeded", "attribution": "evidence"},
                {"asin": "B01MA232WY", "outcome": "failed", "attribution": "time_window_inference"},
                {"asin": "B01MSX7DPF", "outcome": "failed", "attribution": "evidence"},
            ],
        }


class MultiTenantRepository:
    def __init__(self, selected=None):
        self.tenant_id = selected

    def list_tenants(self):
        return [
            {"tenant_id": "tenant-a", "requested": 1, "recorded": 1},
            {"tenant_id": "tenant-b", "requested": 1, "recorded": 1},
        ]

    def for_tenant(self, tenant_id):
        if tenant_id not in {"tenant-a", "tenant-b"}:
            return None
        return MultiTenantRepository(tenant_id)

    def load_identity(self):
        return {"tenant_count": 2, "default_tenant_id": "tenant-a"}

    def load_overview(self, raw_html_dir=None):
        return {"tenant_id": self.tenant_id}

    def list_runs(self, limit=20):
        return [{"run_id": f"run-{self.tenant_id[-1]}", "recorded_actions": 1}]

    def list_operations(self, limit=100):
        return [{"operation_id": f"op-{self.tenant_id[-1]}", "tenant_id": self.tenant_id}]

    def load_run(self, run_id):
        if run_id != f"run-{self.tenant_id[-1]}":
            return None
        return {"tenant_id": self.tenant_id, "run_id": run_id, "items": []}

    def list_items(self, **_kwargs):
        return {"tenant_id": self.tenant_id, "total": 0, "items": []}

    def load_detail(self, _asin):
        return None


class BatchCursor:
    def __init__(self, ledger_only=False):
        self.rows = []
        self.ledger_only = ledger_only

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): return False

    def execute(self, sql, _params=()):
        if "FROM amazon_us.item_state" in sql:
            self.rows = [] if self.ledger_only else [
                {"tenant_id": "owned_us_asin_20260901_100_01", "status": "reviews_pending", "count": 92},
                {"tenant_id": "owned_us_asin_20260901_100_01", "status": "failed", "count": 8},
            ]
        elif "FROM amazon_us.product_latest" in sql:
            self.rows = [] if self.ledger_only else [{"tenant_id": "owned_us_asin_20260901_100_01", "count": 92}]
        elif "WITH latest AS" in sql:
            self.rows = [] if self.ledger_only else [{
                "tenant_id": "owned_us_asin_20260901_100_01", "recorded": 100,
                "latest_at": None, "blocked": 0, "variant_redirect": 8, "failed": 0,
            }]
        elif "COUNT(*) AS evidence_actions" in sql:
            self.rows = [] if self.ledger_only else [{
                "tenant_id": "owned_us_asin_20260901_100_01", "evidence_actions": 100,
                "known_transfer_records": 100, "known_transfer_bytes": 36019216,
                "started_at": None, "ended_at": None, "http_actions": 97, "firefox_actions": 3,
            }]
        elif "to_regclass('amazon_us.collection_run')" in sql:
            self.rows = [{"relation": "amazon_us.collection_run"}]
        elif "to_regclass('amazon_us.operation_run')" in sql:
            self.rows = [{"relation": None}]
        elif "DISTINCT ON (tenant_id)" in sql and "FROM amazon_us.collection_run" in sql:
            self.rows = [{
                "tenant_id": "ledger-only", "requested_actions": 10, "status": "interrupted",
                "started_at": None, "finished_at": None,
            }] if self.ledger_only else []
        elif "EXTRACT(EPOCH FROM (finished_at-started_at))" in sql:
            self.rows = []
        else:
            raise AssertionError(sql)

    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0]


class BatchConnection:
    def __init__(self, ledger_only=False): self.cursor_instance = BatchCursor(ledger_only)
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): return False
    def cursor(self): return self.cursor_instance


def start_server(module, repository=None, api_key=""):
    server = module.ConsoleServer(("127.0.0.1", 0), repository or Repository(), api_key=api_key)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_console_is_loopback_only():
    module = load_module()
    with pytest.raises(ValueError, match="loopback"):
        module.ConsoleServer(("0.0.0.0", 0), Repository())


def test_console_traffic_summary_separates_http_firefox_and_proxy_unknown():
    module = load_module()
    rows = [
        {"source_type": "http_html", "transfer_bytes": 123, "context_json": {"traffic": {"http_compressed_response_bytes": 123}}},
        {
            "source_type": "selenium_dom",
            "transfer_bytes": None,
            "context_json": {
                "fallback_reason": "context_mismatch",
                "traffic": {
                    "http_compressed_response_bytes": 77,
                    "firefox_main_document_bytes": None,
                    "firefox_subresource_bytes": 456,
                    "firefox_main_document_unknown_count": 1,
                    "firefox_subresource_unknown_count": 0,
                },
            },
        },
    ]

    summary = module.summarize_traffic(rows)

    assert summary["http_compressed_response"] == {"bytes": 200, "known_records": 2, "unknown_records": 0}
    assert summary["firefox_main_document"] == {"bytes": None, "known_records": 0, "unknown_records": 1}
    assert summary["firefox_subresources"] == {"bytes": 456, "known_records": 1, "unknown_records": 0}
    assert summary["proxy_dashboard_bill"] == {"bytes": None, "known_records": 0, "unknown_records": 1}


def test_console_traffic_uses_firefox_context_keys_when_final_source_is_http():
    module = load_module()
    rows = [{
        "source_type": "http_html",
        "transfer_bytes": 1147,
        "context_json": {
            "traffic": {
                "http_compressed_response_bytes": 1147,
                "firefox_main_document_bytes": None,
                "firefox_subresource_bytes": None,
                "firefox_main_document_unknown_count": 3,
                "firefox_subresource_unknown_count": 3,
            }
        },
    }]

    summary = module.summarize_traffic(rows)

    assert summary["http_compressed_response"] == {"bytes": 1147, "known_records": 1, "unknown_records": 0}
    assert summary["firefox_main_document"] == {"bytes": None, "known_records": 0, "unknown_records": 3}
    assert summary["firefox_subresources"] == {"bytes": None, "known_records": 0, "unknown_records": 3}


def test_console_context_quality_counts_partial_without_marking_it_failed():
    module = load_module()
    rows = [
        {"error_code": None, "block_reason": None, "context_json": {"context_quality": "full"}},
        {
            "error_code": None,
            "block_reason": None,
            "context_json": {
                "context_quality": "partial",
                "postal_confirmed": False,
                "expected_postal": "90001",
                "observed_postal": "97230",
            },
        },
        {"error_code": "context_mismatch:currency_mismatch", "block_reason": None, "context_json": {"context_quality": "invalid"}},
        {"error_code": None, "block_reason": None, "context_json": {}},
    ]

    assert module.summarize_context_quality(rows) == {"full": 1, "partial": 1, "invalid": 1, "unknown": 1}


def test_variant_redirect_requires_explicit_same_parent_sibling_evidence():
    module = load_module()
    explicit = {
        "error_code": "asin_mismatch",
        "context_json": {
            "identity": {
                "requested_asin": "B0B9ZFDZNJ",
                "observed_asin": "B0B9ZFZZZZ",
                "canonical_asin": "B0B9ZFZZZZ",
                "parent_asin": "B0PARENT01",
                "child_asins": ["B0B9ZFDZNJ", "B0B9ZFZZZZ"],
            }
        },
    }
    ambiguous = {"error_code": "asin_mismatch", "context_json": {}}

    assert module.classify_evidence_outcome(explicit) == "variant_redirect"
    assert module.classify_evidence_outcome(ambiguous) == "failed"


def test_price_status_distinguishes_unavailable_without_buy_box_from_missing():
    module = load_module()

    assert module.project_price_status({
        "price": None,
        "availability": "Currently unavailable. We don't know when or if this item will be back in stock.",
        "buy_box": {},
    }) == "unavailable"
    assert module.project_price_status({"price": None, "availability": None, "buy_box": {}}) == "missing"
    assert module.project_price_status({"price": "$19.99", "availability": "In Stock", "buy_box": {}}) == "available"
    assert module.project_price_status({
        "price": "",
        "availability": "We don't know when or if this item will be back in stock. Currently unavailable.",
        "buy_box": {"text": "Currently unavailable. Deliver to Los Angeles 90001. Add to List"},
    }) == "unavailable"


def test_batch_summary_projects_real_100_asin_acceptance_counts():
    module = load_module()
    repository = module.PostgresConsoleRepository("postgresql://example")
    repository._connect = lambda: BatchConnection()

    batch = repository.list_tenants()[0]

    assert batch["tenant_id"] == "owned_us_asin_20260901_100_01"
    assert (batch["requested"], batch["recorded"]) == (100, 100)
    assert batch["product_succeeded"] == 92
    assert batch["variant_redirect"] == 8
    assert batch["failed"] == 0
    assert batch["blocked"] == 0
    assert (batch["pending"], batch["running"]) == (0, 0)
    assert batch["terminal_status"] == "complete"


def test_tenant_list_includes_ledger_only_interrupted_run():
    module = load_module()
    repository = module.PostgresConsoleRepository("postgresql://example")
    repository._connect = lambda: BatchConnection(ledger_only=True)

    assert repository.list_tenants() == [{
        "tenant_id": "ledger-only", "requested": 10, "recorded": 0, "product_succeeded": 0,
        "variant_redirect": 0, "failed": 0, "blocked": 0, "pending": 10, "running": 0,
        "evidence_actions": 0, "known_transfer_bytes": 0, "unknown_transfer_records": 0,
        "http_actions": 0, "firefox_actions": 0, "started_at": None, "ended_at": None,
        "duration_seconds": None, "active_duration_seconds": None, "wall_span_seconds": None,
        "wall_span_includes_idle": False, "terminal_status": "interrupted",
    }]


def test_tenant_summary_uses_database_aggregation_not_python_evidence_scan():
    source = SCRIPT.read_text(encoding="utf-8")
    method = source[source.index("    def list_tenants"):source.index("    def load_overview")]
    assert "WITH latest AS" in method and "classified AS" in method
    assert "latest_evidence =" not in method
    assert "idx_evidence_tenant_identity_latest" in (ROOT / "schema" / "postgres_schema.sql").read_text(encoding="utf-8")


def test_batch_active_duration_sums_runs_and_wall_span_is_separate():
    module = load_module()
    batch = module.project_batch_durations([
        {"started_at": "2026-09-02T03:33:23Z", "finished_at": "2026-09-02T03:34:17Z", "duration_seconds": 53.62},
        {"started_at": "2026-09-02T03:40:34Z", "finished_at": "2026-09-02T03:42:53Z", "duration_seconds": 139.46},
    ])

    assert batch["active_duration_seconds"] == 193.08
    assert batch["wall_span_seconds"] == 570.0
    assert batch["wall_span_includes_idle"] is True


def test_run_duration_caps_manual_backfill_timestamp_with_controller_receipt():
    module = load_module()
    durations = module.project_run_durations({
        "started_at": "2026-09-02T03:33:23Z",
        "finished_at": "2026-09-02T03:49:40Z",
        "receipt_json": {
            "elapsed_seconds": 53.62,
            "started_at": "2026-09-02T03:33:23Z",
            "finished_at": "2026-09-02T03:34:17Z",
        },
    })
    assert durations["worker_duration_seconds"] == 53.62
    assert durations["controller_duration_seconds"] == 53.62
    assert durations["duration_source"] == "receipt_json.elapsed_seconds_backfill_cap"
    assert durations["effective_finished_at"].isoformat() == "2026-09-02T03:34:17+00:00"


def test_operation_runs_api_is_tenant_scoped_and_separate_from_collection_runs():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "/api/operations" in source
    assert "list_operations" in source
    assert "amazon_us.operation_run" in source
    run_method = source[source.index("    def list_runs"):source.index("    def list_operations")]
    assert "operation_run" not in run_method


def test_canary_operation_projection_preserves_unknown_and_safe_capacity_details():
    module = load_module()
    operation = {
        "operation_id": "op-canary-1",
        "tenant_id": "tenant-a",
        "operation_type": "canary",
        "status": "failed",
        "preflight_status": "not_applicable",
        "preflight_duration_ms": None,
        "failure_stage": "capacity_gate",
        "error_class": "credentials_missing",
        "egress_id": "dataimpulse-us",
        "collection_run_id": None,
        "http_status": None,
        "response_bytes": None,
        "probe_elapsed_ms": None,
        "started_at": None,
        "finished_at": None,
        "duration_ms": None,
        "canary_status": "unknown",
        "planned_slots": 3,
        "tested_slots": 0,
        "available_slots": None,
        "unique_egress_count": None,
        "duplicate_egress_count": None,
        "requested_capacity": 3,
        "required_slots": 1,
        "slot_capacity": None,
        "capacity_gate_status": "denied",
        "capacity_gate_reason": "credentials_missing",
        "canary_p95_latency_ms": None,
        "capacity_detail_json": {"sessions": []},
    }

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, sql, _params=()):
            self.rows = [{"relation": "amazon_us.operation_run"}] if "to_regclass" in sql else [operation]
        def fetchone(self): return self.rows[0]
        def fetchall(self): return self.rows

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def cursor(self): return Cursor()

    repository = module.PostgresConsoleRepository("postgresql://fixture", "tenant-a")
    repository._connect = lambda: Connection()
    result = repository.list_operations()

    assert result[0]["canary_status"] == "unknown"
    assert result[0]["available_slots"] is None
    assert result[0]["unique_egress_count"] is None
    assert result[0]["slot_capacity"] is None
    assert result[0]["capacity_gate_reason"] == "credentials_missing"
    app = (ROOT / "console" / "app.js").read_text(encoding="utf-8")
    assert "canarySummary" in app
    assert "unknown" in app


def test_console_operations_include_bound_run_capacity_and_agent_reservation_denials():
    module = load_module()
    run_operation = {
        "operation_id": "op-run-1", "tenant_id": "tenant-a", "operation_type": "run",
        "status": "running", "preflight_status": "succeeded", "preflight_duration_ms": 10,
        "failure_stage": None, "error_class": None, "egress_id": "dataimpulse-us",
        "collection_run_id": "run-1", "http_status": None, "response_bytes": None,
        "probe_elapsed_ms": None, "started_at": None, "finished_at": None, "duration_ms": None,
        "canary_status": None, "planned_slots": None, "tested_slots": None, "available_slots": None,
        "unique_egress_count": None, "duplicate_egress_count": None, "requested_capacity": None,
        "required_slots": None, "slot_budget": None, "slot_capacity": None,
        "capacity_gate_status": None, "capacity_gate_reason": None, "canary_p95_latency_ms": None,
        "capacity_detail_json": None, "credential_generation": None,
        "authorizing_canary_operation_id": "op-canary-1", "capacity_reservation_id": "reservation-1",
        "capacity_fact_finished_at": None, "capacity_fact_expires_at": None, "reserved_slots": 1,
        "capacity_authorization_json": {"canary_operation_id": "op-canary-1", "reservation_id": "reservation-1"},
    }
    denied_reservation = {
        "reservation_id": "reservation-denied", "tenant_id": "tenant-a", "owner_id": "agent-refresh",
        "canary_operation_id": "op-canary-1", "requested_capacity": 5, "required_slots": 2,
        "reserved_slots": 0, "status": "denied", "reason": "capacity_reserved_elsewhere",
        "fact_finished_at": None, "fact_expires_at": None, "expires_at": None,
        "capacity_snapshot_json": {"unique_egress_count": 1, "slot_capacity": 3},
        "created_at": None, "released_at": None, "updated_at": None, "observed_at": None,
    }
    expired_reservation = {
        **denied_reservation,
        "reservation_id": "reservation-expired", "status": "active", "reason": "capacity_reserved",
        "reserved_slots": 1,
        "expires_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "observed_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
    }

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def execute(self, sql, _params=()):
            if "to_regclass('amazon_us.operation_run')" in sql:
                self.rows = [{"relation": "amazon_us.operation_run"}]
            elif "FROM amazon_us.operation_run" in sql:
                self.rows = [run_operation]
            elif "to_regclass('amazon_us.proxy_capacity_reservation')" in sql:
                self.rows = [{"relation": "amazon_us.proxy_capacity_reservation"}]
            elif "FROM amazon_us.proxy_capacity_reservation" in sql:
                self.rows = [denied_reservation, expired_reservation]
            else:
                raise AssertionError(sql)
        def fetchone(self): return self.rows[0]
        def fetchall(self): return self.rows

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def cursor(self): return Cursor()

    repository = module.PostgresConsoleRepository("postgresql://fixture", "tenant-a")
    repository._connect = lambda: Connection()
    items = repository.list_operations()

    assert {item["operation_type"] for item in items} == {"run", "capacity_reservation"}
    run = next(item for item in items if item["operation_type"] == "run")
    denial = next(item for item in items if item["operation_type"] == "capacity_reservation")
    expired = next(item for item in items if item.get("operation_id") == "reservation-expired")
    assert run["authorizing_canary_operation_id"] == "op-canary-1"
    assert run["capacity_reservation_id"] == "reservation-1"
    assert denial["status"] == "denied"
    assert denial["capacity_gate_reason"] == "capacity_reserved_elsewhere"
    assert denial["available_slots"] is None
    assert expired["status"] == "expired"
    assert expired["capacity_gate_status"] == "denied"
    assert expired["capacity_gate_reason"] == "reservation_expired"


def test_console_tooltips_explain_all_operational_terms_accessibly():
    html = (ROOT / "console" / "index.html").read_text(encoding="utf-8")
    for term in (
        "Requested / Recorded", "商品成功", "Variant Redirect", "Failed", "Blocked",
        "Pending / Running", "流量", "活跃耗时", "墙钟跨度", "HTTP", "Firefox", "代理账单unknown",
    ):
        assert term in html
    assert html.count("title=") >= 12
    assert html.count("aria-label=") >= 12


def test_console_projects_proxy_connectivity_separately_from_amazon_business_outcomes():
    module = load_module()
    authorization = {
        "canary_operation_id": "op-canary-1", "fact_expires_at": "2026-09-03T10:00:00Z",
        "capacity_snapshot": {
            "canary_status": "partial", "tested_slots": 34, "available_slots": 32,
            "unique_egress_count": 32, "slot_capacity": 32,
            "capacity_gate_status": "allowed", "capacity_gate_reason": "capacity_sufficient",
        },
    }
    items = (
        [{"outcome": "completed"}] * 6
        + [{"outcome": "variant_redirect"}] * 3
        + [{"outcome": "blocked"}]
    )

    connectivity = module.project_proxy_connectivity(authorization)
    business = module.project_amazon_business(items, requested_actions=20, recorded_actions=10, unrequested_actions=10)

    assert connectivity == {
        "egress_profile": "proxy_sessions", "canary_operation_id": "op-canary-1",
        "canary_status": "partial", "tested_slots": 34, "available_slots": 32,
        "unique_egress_count": 32, "slot_capacity": 32, "gate_status": "allowed",
        "gate_reason": "capacity_sufficient", "fact_expires_at": "2026-09-03T10:00:00Z",
    }
    assert business == {
        "requested_actions": 20, "recorded_actions": 10, "completed_actions": 6,
        "variant_redirect_actions": 3, "failed_actions": 0, "blocked_actions": 1,
        "unrequested_actions": 10, "access_control_rate": 0.1,
    }


def test_variant_redirect_requires_explicit_same_parent_sibling_evidence():
    module = load_module()
    explicit = {
        "error_code": "asin_mismatch",
        "context_json": {
            "identity": {
                "requested_asin": "B0B9ZFDZNJ",
                "observed_asin": "B0B9ZFZZZZ",
                "canonical_asin": "B0B9ZFZZZZ",
                "parent_asin": "B0PARENT01",
                "child_asins": ["B0B9ZFDZNJ", "B0B9ZFZZZZ"],
            }
        },
    }
    ambiguous = {"error_code": "asin_mismatch", "context_json": {}}

    assert module.classify_evidence_outcome(explicit) == "variant_redirect"
    assert module.classify_evidence_outcome(ambiguous) == "failed"


def test_price_status_distinguishes_unavailable_without_buy_box_from_missing():
    module = load_module()

    assert module.project_price_status({
        "price": None,
        "availability": "Currently unavailable. We don't know when or if this item will be back in stock.",
        "buy_box": {},
    }) == "unavailable"
    assert module.project_price_status({"price": None, "availability": None, "buy_box": {}}) == "missing"
    assert module.project_price_status({"price": "$19.99", "availability": "In Stock", "buy_box": {}}) == "available"


def test_console_serves_static_ui_with_security_headers():
    module = load_module()
    server, thread = start_server(module)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=2) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert "Amazon Collection Console" in body
            assert 'id="httpTrafficMetric"' in body
            assert 'id="firefoxMainMetric"' in body
            assert 'id="firefoxSubresourceMetric"' in body
            assert 'id="proxyBillMetric"' in body
            assert 'id="partialMetric"' in body
            assert "default-src 'self'" in response.headers["Content-Security-Policy"]
            assert response.headers["Cache-Control"] == "no-store"
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/app.js", timeout=2) as response:
            script = response.read().decode("utf-8")
            assert response.headers["Content-Type"].startswith("text/javascript")
            assert "context_quality" in script
            assert "location_sensitive_fields_unverified" in script
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/readyz", timeout=2) as response:
            ready = json.loads(response.read())
            assert ready["ok"] is True
            assert ready["tenant_id"] == "tenant-console"
            assert ready["task_count"] == 3
            assert re.fullmatch(r"[0-9a-f]{64}", ready["runtime_fingerprint"])
    finally:
        stop_server(server, thread)


def test_console_overview_and_filtered_items_are_read_only_json():
    module = load_module()
    repository = Repository()
    server, thread = start_server(module, repository)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/overview", timeout=2) as response:
            payload = json.loads(response.read())
            assert payload["tenant_id"] == "tenant-console"
            assert payload["status_counts"]["blocked"] == 1
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/api/items?status=failed&stage=product&q=RCPD&limit=25&offset=5",
            timeout=2,
        ) as response:
            payload = json.loads(response.read())
            assert payload["items"][0]["asin"] == "B00RCPDCQU"
        assert repository.list_args == {
            "status": "failed",
            "stage": "product",
            "query": "RCPD",
            "limit": 25,
            "offset": 5,
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/overview", data=b"{}", method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        assert raised.value.code == 405
    finally:
        stop_server(server, thread)


def test_console_returns_asin_detail_and_404():
    module = load_module()
    server, thread = start_server(module)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/api/items/B00RCPDCQU", timeout=2
        ) as response:
            payload = json.loads(response.read())
            assert payload["product"]["title"] == "Example"
            assert payload["media"][0]["asset_url"].startswith("https://")
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/items/B000000000", timeout=2)
        assert raised.value.code == 404
    finally:
        stop_server(server, thread)


def test_console_lists_runs_and_returns_one_run_result():
    module = load_module()
    server, thread = start_server(module)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/runs", timeout=2) as response:
            payload = json.loads(response.read())
        assert payload["items"][0]["run_id"] == "run-1"
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/runs/run-1", timeout=2) as response:
            run = json.loads(response.read())
        assert len(run["items"]) == 3
        assert run["items"][1]["attribution"] == "time_window_inference"
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/runs/missing", timeout=2)
        assert raised.value.code == 404
    finally:
        stop_server(server, thread)


def test_console_tenant_selection_is_explicit_and_cross_tenant_runs_are_invisible():
    module = load_module()
    server, thread = start_server(module, MultiTenantRepository())
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/tenants", timeout=2) as response:
            tenants = json.loads(response.read())
        assert [item["tenant_id"] for item in tenants["items"]] == ["tenant-a", "tenant-b"]

        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/api/runs/run-a?tenant=tenant-a", timeout=2
        ) as response:
            run = json.loads(response.read())
        assert run["tenant_id"] == "tenant-a"

        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/runs/run-b?tenant=tenant-a", timeout=2
            )
        assert raised.value.code == 404
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/api/operations?tenant=tenant-a", timeout=2
        ) as response:
            operations = json.loads(response.read())
        assert operations["items"] == [{"operation_id": "op-a", "tenant_id": "tenant-a"}]
    finally:
        stop_server(server, thread)


def test_console_tenant_selection_is_explicit_and_cross_tenant_runs_are_invisible():
    module = load_module()
    server, thread = start_server(module, MultiTenantRepository())
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/tenants", timeout=2) as response:
            tenants = json.loads(response.read())
        assert [item["tenant_id"] for item in tenants["items"]] == ["tenant-a", "tenant-b"]

        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/api/runs/run-a?tenant=tenant-a", timeout=2
        ) as response:
            run = json.loads(response.read())
        assert run["tenant_id"] == "tenant-a"

        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/runs/run-b?tenant=tenant-a", timeout=2
            )
        assert raised.value.code == 404
    finally:
        stop_server(server, thread)


def test_console_optional_api_key_protects_api_not_static_shell():
    module = load_module()
    server, thread = start_server(module, api_key="console-secret")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=2) as response:
            assert response.status == 200
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/overview", timeout=2)
        assert raised.value.code == 401
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/overview",
            headers={"X-Collection-API-Key": "console-secret"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 200
    finally:
        stop_server(server, thread)
