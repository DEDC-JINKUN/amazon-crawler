from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import sys
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collection_mcp_server.py"


def load_module():
    spec = importlib.util.spec_from_file_location("collection_mcp_server_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = []

    async def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        return self.responses.popleft()


def test_module_import_boundary_excludes_crawler_database_and_proxy_modules():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    assert names.isdisjoint({"amazon_us_worker", "collection_storage", "postgres_worker_storage", "psycopg", "proxy_canary", "proxy_session_pool"})


def test_settings_require_explicit_loopback_origin_and_scoped_credentials():
    module = load_module()
    valid = {
        module.BASE_URL_ENV: "http://127.0.0.1:18765",
        module.AGENT_ID_ENV: "refresh-agent",
        module.AGENT_KEY_ENV: "x" * 32,
    }
    assert module.load_settings(valid).base_url == "http://127.0.0.1:18765"
    for bad in ("https://example.com:443", "http://user:pass@127.0.0.1:18765", "http://127.0.0.1:18765/v1"):
        rejected = {**valid, module.BASE_URL_ENV: bad}
        try:
            module.load_settings(rejected)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe MCP base URL accepted: {bad}")


def test_tool_discovery_annotations_and_natural_batch_submission():
    module = load_module()
    response = module.ToolEnvelope(ok=True, http_status=202, data={"campaign": {"campaign_id": "campaign-" + "1" * 32, "status": "active", "total": 6, "terminal": 0, "counts": {"pending": 6}, "members": []}})
    fake = FakeClient([response])
    server = module.create_server(fake)

    async def scenario():
        async with Client(server, raise_exceptions=True) as client:
            listed = await client.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            assert {"collection_capabilities", "product_get", "batch_get", "campaign_submit", "campaign_status", "campaign_wait", "job_get", "campaign_cancel"} <= set(tools)
            assert tools["campaign_status"].annotations.read_only_hint is True
            assert tools["campaign_wait"].annotations.read_only_hint is True
            assert tools["campaign_submit"].annotations.read_only_hint is False
            assert tools["campaign_submit"].annotations.idempotent_hint is True
            assert tools["campaign_cancel"].annotations.destructive_hint is True
            schema = tools["campaign_submit"].input_schema
            assert schema["properties"]["priority"]["maximum"] == 100
            assert schema["properties"]["deadline_seconds"]["minimum"] == 60
            invalid = await client.call_tool("campaign_submit", {
                "asins": ["not-an-asin"], "idempotency_key": "mcp-natural-batch-001",
            })
            assert invalid.is_error and not fake.calls
            result = await client.call_tool("campaign_submit", {
                "asins": [f"B0MCP{i:05d}" for i in range(6)],
                "idempotency_key": "mcp-natural-batch-001",
                "subject_type": "own",
            })
            assert not result.is_error
            assert result.structured_content["http_status"] == 202
            assert result.structured_content["data"]["campaign"]["total"] == 6

    asyncio.run(scenario())
    assert len(fake.calls) == 1
    assert fake.calls[0][1] == "/v1/campaigns"
    assert len(fake.calls[0][2]["asins"]) == 6


def test_campaign_wait_honors_retry_after_and_reports_terminal_progress(monkeypatch):
    module = load_module()
    fake = FakeClient([
        module.ToolEnvelope(ok=False, http_status=503, error=module.ActionableError(code="worker_unavailable", message="x", action="wait")),
        module.ToolEnvelope(ok=False, http_status=429, error=module.ActionableError(code="rate_limited", message="x", action="wait"), retry_after_seconds=4.0),
        module.ToolEnvelope(ok=True, http_status=200, data={"campaign": {"campaign_id": "campaign-" + "2" * 32, "status": "active", "total": 1, "terminal": 0, "counts": {"pending": 1}, "members": []}}),
        module.ToolEnvelope(ok=True, http_status=200, data={"campaign": {"campaign_id": "campaign-" + "2" * 32, "status": "completed", "total": 1, "terminal": 1, "counts": {"succeeded": 1}, "members": []}}),
    ])
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    server = module.create_server(fake)

    async def scenario():
        async with Client(server, raise_exceptions=True) as client:
            result = await client.call_tool("campaign_wait", {
                "campaign_id": "campaign-" + "2" * 32,
                "max_wait_seconds": 30,
                "poll_interval_seconds": 1.1,
            })
            assert result.structured_content["data"]["campaign"]["status"] == "completed"
            assert result.structured_content["data"]["wait"]["terminal"] is True

    asyncio.run(scenario())
    assert sleeps[:2] == [1.1, 4.0]
    assert len(fake.calls) == 4


def test_safe_projections_drop_internal_paths_context_and_credentials():
    module = load_module()
    raw = {
        "schema_version": "v1", "marketplace": "US", "asin": "B0MCP00000", "quality_status": "failed",
        "product": {"asin": "B0MCP00000", "title": "Fixture", "price": "$1", "raw_html_path": "private/file"},
        "task": {"status": "failed", "last_error": "access_control", "lease_token": "secret"},
        "evidence": {"error_code": "captcha", "context_json": {"password": "secret"}, "raw_html_path": "private/file"},
    }
    rendered = json.dumps(module._product_view(raw), sort_keys=True)
    assert "Fixture" in rendered and "access_control" in rendered and "captcha" in rendered
    for forbidden in ("raw_html_path", "context_json", "lease_token", "password", "private/file"):
        assert forbidden not in rendered
    job = module._job_view({
        "job": {"job_id": "refresh-1", "status": "failed", "lease_token": "secret"},
        "result": {
            "quality_status": "failed",
            "product": {"asin": "B0MCP00000", "title": "Fixture", "raw_html_path": "private/file"},
            "evidence": {"error": "captcha", "context_json": {"password": "secret"}},
            "traffic": {"transfer_bytes": 5, "proxy_url": "secret"},
        },
    })
    job_rendered = json.dumps(job, sort_keys=True)
    assert "Fixture" in job_rendered and "captcha" in job_rendered
    for forbidden in ("raw_html_path", "context_json", "lease_token", "password", "proxy_url", "private/file"):
        assert forbidden not in job_rendered


class FixtureAPI(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def _send(self, status, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("X-Collection-Agent") != "refresh-agent" or self.headers.get("X-Collection-Agent-Key") != "k" * 32:
            self._send(401, {"error": "unauthorized"}); return
        if self.path == "/v1/capabilities":
            self._send(200, {"schema_version": "v1", "transport": "loopback_http", "execution": "asynchronous_jobs", "limits": {"campaign_asins": 5000}, "tools": {"submit_campaign": {}}}); return
        if self.path == "/rate":
            self._send(429, {"error": "rate_limited"}, {"Retry-After": "4"}); return
        if self.path == "/redirect":
            self._send(302, {"error": "redirect"}, {"Location": "http://example.invalid/"}); return
        if self.path == "/leakerror":
            self._send(400, {"error": "bad /private/path secret"}); return
        if self.path.startswith("/v1/jobs/") and self.path.endswith("?view=summary"):
            job_id = self.path.split("/", 3)[-1].split("?", 1)[0]
            self._send(200, {"schema_version": "v1", "job": {"job_id": job_id, "asin": "B0MCP00000", "status": "failed"}, "result": {"quality_status": "failed", "evidence": {"error": "access_control"}}}); return
        self._send(404, {"error": "not_found"})


def test_http_client_preserves_status_backpressure_refuses_redirects_and_ignores_proxy_env(monkeypatch):
    module = load_module()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    api = ThreadingHTTPServer(("127.0.0.1", 0), FixtureAPI)
    thread = threading.Thread(target=api.serve_forever, daemon=True); thread.start()

    async def scenario():
        settings = module.Settings(f"http://127.0.0.1:{api.server_port}", "refresh-agent", "k" * 32)
        client = module.CollectionAPIClient(settings)
        rate = await client.request("GET", "/rate")
        assert rate.http_status == 429 and rate.retry_after_seconds == 4.0
        assert rate.error.code == "rate_limited"
        redirect = await client.request("GET", "/redirect")
        assert redirect.http_status == 302 and redirect.error.code == "redirect_refused"
        safe = await client.request("GET", "/leakerror")
        assert safe.http_status == 400 and safe.error.code == "bad__private_path_secret"
        assert "/" not in safe.model_dump_json()

    try:
        asyncio.run(scenario())
    finally:
        api.shutdown(); api.server_close(); thread.join(2)


def test_campaign_poll_timeout_after_retry_after_does_not_make_extra_request():
    module = load_module()
    fake = FakeClient([
        module.ToolEnvelope(ok=False, http_status=429, error=module.ActionableError(code="rate_limited", message="x", action="wait"), retry_after_seconds=4.0),
    ])
    server = module.create_server(fake)

    async def scenario():
        async with Client(server, raise_exceptions=True) as client:
            result = await client.call_tool("campaign_wait", {
                "campaign_id": "campaign-" + "3" * 32,
                "max_wait_seconds": 1,
                "poll_interval_seconds": 1.1,
            })
            assert result.structured_content["http_status"] == 429
            assert result.structured_content["retry_after_seconds"] == 4.0
            assert result.structured_content["error"]["code"] == "poll_timeout"
            assert result.structured_content["data"]["wait"]["timed_out"] is True
            assert result.structured_content["data"]["wait"]["next_action"] == "call_campaign_wait_again"

    asyncio.run(scenario())
    assert len(fake.calls) == 1


def test_campaign_wait_reports_redirect_as_unrecoverable_without_retrying():
    module = load_module()
    fake = FakeClient([
        module.ToolEnvelope(
            ok=False,
            http_status=302,
            error=module.ActionableError(code="redirect_refused", message="x", action="operator"),
        ),
    ])
    server = module.create_server(fake)

    async def scenario():
        async with Client(server, raise_exceptions=True) as client:
            result = await client.call_tool("campaign_wait", {
                "campaign_id": "campaign-" + "5" * 32,
                "max_wait_seconds": 30,
            })
            assert result.structured_content["http_status"] == 302
            assert result.structured_content["error"]["code"] == "redirect_refused"
            assert result.structured_content["data"]["wait"]["next_action"] == "report_unrecoverable_service_error"

    asyncio.run(scenario())
    assert len(fake.calls) == 1


def test_status_is_one_snapshot_and_repeated_wait_calls_reach_real_terminal_state():
    module = load_module()
    assert module._campaign_all_terminal({"status": "cancelled", "total": 2, "terminal": 1}) is False
    assert module._campaign_all_terminal({"status": "cancelled", "total": 2, "terminal": 2}) is True
    campaign_id = "campaign-" + "4" * 32

    def active():
        return module.ToolEnvelope(ok=True, http_status=200, data={"campaign": {
            "campaign_id": campaign_id, "status": "active", "total": 2, "terminal": 0,
            "counts": {"deferred": 2}, "members": [],
        }})

    def terminal():
        return module.ToolEnvelope(ok=True, http_status=200, data={"campaign": {
            "campaign_id": campaign_id, "status": "completed", "total": 2, "terminal": 2,
            "counts": {"succeeded": 1, "failed": 1}, "members": [],
        }})

    fake = FakeClient([active(), active(), active(), terminal()])
    server = module.create_server(fake)

    async def scenario():
        async with Client(server, raise_exceptions=True) as client:
            snapshot = await client.call_tool("campaign_status", {"campaign_id": campaign_id})
            assert snapshot.structured_content["data"]["progress"]["terminal"] is False
            assert snapshot.structured_content["data"]["progress"]["next_action"] == "call_campaign_wait_if_user_requested_results"
            for _ in range(2):
                waiting = await client.call_tool("campaign_wait", {
                    "campaign_id": campaign_id, "max_wait_seconds": 1, "poll_interval_seconds": 1.1,
                })
                assert waiting.structured_content["data"]["wait"]["next_action"] == "call_campaign_wait_again"
                assert waiting.structured_content["data"]["wait"]["terminal"] is False
            done = await client.call_tool("campaign_wait", {
                "campaign_id": campaign_id, "max_wait_seconds": 1, "poll_interval_seconds": 1.1,
            })
            assert done.structured_content["data"]["wait"]["terminal"] is True
            assert done.structured_content["data"]["wait"]["next_action"] == "inspect_terminal_members_with_job_get"

    asyncio.run(scenario())
    assert [path for _method, path, _payload in fake.calls] == [f"/v1/campaigns/{campaign_id}"] * 4


def test_real_stdio_protocol_lists_and_calls_tools_with_minimal_environment():
    api = ThreadingHTTPServer(("127.0.0.1", 0), FixtureAPI)
    thread = threading.Thread(target=api.serve_forever, daemon=True)
    thread.start()

    async def scenario():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SCRIPT)],
            cwd=str(ROOT),
            env={
                "AMAZON_COLLECTION_MCP_BASE_URL": f"http://127.0.0.1:{api.server_port}",
                "AMAZON_COLLECTION_AGENT_ID": "refresh-agent",
                "AMAZON_COLLECTION_AGENT_KEY": "k" * 32,
            },
        )
        async with Client(params) as client:
            listed = await client.list_tools()
            assert "campaign_submit" in {tool.name for tool in listed.tools}
            capability = await client.call_tool("collection_capabilities", {})
            assert capability.structured_content["ok"] is True
            job = await client.call_tool("job_get", {"job_id": "refresh-protocol-1"})
            assert job.structured_content["http_status"] == 200
            assert job.structured_content["data"]["result"]["evidence"]["error"] == "access_control"

    try:
        asyncio.run(scenario())
    finally:
        api.shutdown(); api.server_close(); thread.join(2)
