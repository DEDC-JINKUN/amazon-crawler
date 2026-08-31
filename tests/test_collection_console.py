from __future__ import annotations

import importlib.util
import json
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
            assert "default-src 'self'" in response.headers["Content-Security-Policy"]
            assert response.headers["Cache-Control"] == "no-store"
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/app.js", timeout=2) as response:
            assert response.headers["Content-Type"].startswith("text/javascript")
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/readyz", timeout=2) as response:
            ready = json.loads(response.read())
            assert ready["ok"] is True
            assert ready["tenant_id"] == "tenant-console"
            assert ready["task_count"] == 3
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
