from __future__ import annotations

import importlib.util
import json
import logging
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "amazon_us_worker.py"


def load_worker():
    spec = importlib.util.spec_from_file_location("amazon_us_worker_low_traffic_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _cookie_header(session, url: str) -> str:
    request = Request(url)
    session.jar.add_cookie_header(request)
    return request.get_header("Cookie") or ""


def test_run_cookie_session_filters_domain_path_secure_and_expiry(caplog):
    worker = load_worker()
    now = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)
    session = worker.RunScopedAmazonCookieSession("run-1", "tenant-a", "worker-a", now=lambda: now)
    cookies = [
        {"name": "host", "value": "host-secret", "domain": "www.amazon.com", "path": "/", "secure": False},
        {"name": "domain", "value": "domain-secret", "domain": ".amazon.com", "path": "/dp", "secure": True, "expiry": int(now.timestamp()) + 315_360_000},
        {"name": "expired", "value": "expired-secret", "domain": ".amazon.com", "path": "/", "expiry": int(now.timestamp()) - 1},
        {"name": "foreign", "value": "foreign-secret", "domain": ".example.com", "path": "/"},
        {"name": "bad_path", "value": "path-secret", "domain": ".amazon.com", "path": "dp"},
    ]

    with caplog.at_level(logging.DEBUG):
        accepted = session.sync_from_firefox(
            cookies,
            run_id="run-1",
            tenant_id="tenant-a",
            worker_id="worker-a",
            context_confirmed=True,
        )

    assert accepted == 2
    assert _cookie_header(session, "https://www.amazon.com/dp/B00RCPDCQU") == "domain=domain-secret; host=host-secret"
    assert _cookie_header(session, "https://www.amazon.com/gp/product/B00RCPDCQU") == "host=host-secret"
    assert _cookie_header(session, "http://www.amazon.com/dp/B00RCPDCQU") == "host=host-secret"
    audit = json.dumps(session.audit_summary(), sort_keys=True)
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    for secret in ("host-secret", "domain-secret", "expired-secret", "foreign-secret", "path-secret"):
        assert secret not in audit
        assert secret not in rendered_logs


def test_run_cookie_session_is_scope_isolated_and_destroyed_on_close():
    worker = load_worker()
    first = worker.RunScopedAmazonCookieSession("run-1", "tenant-a", "worker-a")
    second = worker.RunScopedAmazonCookieSession("run-2", "tenant-a", "worker-a")
    cookie = {"name": "session", "value": "isolated-secret", "domain": ".amazon.com", "path": "/"}

    assert first.sync_from_firefox(
        [cookie], run_id="run-1", tenant_id="tenant-a", worker_id="worker-a", context_confirmed=True
    ) == 1
    assert "isolated-secret" in _cookie_header(first, "https://www.amazon.com/")
    assert _cookie_header(second, "https://www.amazon.com/") == ""
    with pytest.raises(ValueError, match="scope"):
        first.sync_from_firefox(
            [cookie], run_id="run-2", tenant_id="tenant-a", worker_id="worker-a", context_confirmed=True
        )
    assert first.sync_from_firefox(
        [cookie], run_id="run-1", tenant_id="tenant-a", worker_id="worker-a", context_confirmed=False
    ) == 0

    first.close()
    assert _cookie_header(first, "https://www.amazon.com/") == ""
    assert first.audit_summary()["cookie_count"] == 0


def test_http_adapter_bridges_only_confirmed_isolated_firefox_cookies_into_current_run():
    worker = load_worker()

    class Browser:
        _context_initialized = True
        last_traffic = {
            "main_document_bytes": None,
            "subresource_bytes": None,
            "main_document_known_count": 0,
            "subresource_known_count": 0,
            "main_document_unknown_count": 1,
            "subresource_unknown_count": 1,
            "blocked_resource_counts": {},
        }

        def fetch(self, url):
            return "<html>browser</html>", 200

        def export_anonymous_amazon_cookies(self):
            return [{"name": "lc-main", "value": "bridge-secret", "domain": ".amazon.com", "path": "/"}]

        def close(self):
            pass

    with patch.object(worker.urllib.request, "build_opener"):
        adapter = worker.HttpFirstAdapter({**worker.DEFAULTS, "tenant_id": "tenant-a", "worker_id": "worker-a"})
        adapter.begin_run("run-1", "tenant-a", "worker-a")
        adapter.browser = Browser()
        body, status = adapter.fetch_browser(
            "https://www.amazon.com/dp/B00RCPDCQU",
            fallback_reason=worker.FallbackReason.CONTEXT_MISMATCH,
            run_id="run-1",
            asin="B00RCPDCQU",
        )

    assert (body, status) == ("<html>browser</html>", 200)
    assert _cookie_header(adapter.cookie_session, "https://www.amazon.com/") == ""
    assert adapter.commit_browser_context("run-1", context_confirmed=False) == 0
    assert adapter.commit_browser_context("run-1", context_confirmed=True) == 1
    assert "bridge-secret" in _cookie_header(adapter.cookie_session, "https://www.amazon.com/")
    assert adapter.last_fallback_reason == "context_mismatch"
    assert adapter.last_transfer_bytes is None
    assert "bridge-secret" not in json.dumps(worker._evidence_context({"postal_code": "90001"}, adapter))
    adapter.close()
    assert _cookie_header(adapter.cookie_session, "https://www.amazon.com/") == ""


def test_http_adapter_switching_run_destroys_browser_and_cookie_state():
    worker = load_worker()

    class Browser:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    with patch.object(worker.urllib.request, "build_opener"):
        adapter = worker.HttpFirstAdapter(worker.DEFAULTS)
        adapter.begin_run("run-1", "tenant-a", "worker-a")
        browser = Browser()
        adapter.browser = browser
        adapter.cookie_session.sync_from_firefox(
            [{"name": "session", "value": "run-one-secret", "domain": ".amazon.com", "path": "/"}],
            run_id="run-1",
            tenant_id="tenant-a",
            worker_id="worker-a",
            context_confirmed=True,
        )

        adapter.begin_run("run-2", "tenant-a", "worker-a")

    assert browser.closed is True
    assert adapter.browser is None
    assert adapter.cookie_session.scope == ("run-2", "tenant-a", "worker-a")
    assert _cookie_header(adapter.cookie_session, "https://www.amazon.com/") == ""
    adapter.close()


def test_firefox_adapter_installs_selenium_447_high_level_bidi_handlers():
    worker = load_worker()

    class FakeNetwork:
        def __init__(self):
            self.request_handlers = []
            self.event_handlers = []

        def add_request_handler(self, callback):
            self.request_handlers.append(callback)
            return "request-handler"

        def add_event_handler(self, event, callback):
            self.event_handlers.append((event, callback))
            return 7

        def remove_request_handler(self, handler_id):
            pass

        def remove_event_handler(self, event, handler_id):
            pass

    class FakeDriver:
        def __init__(self):
            self.network = FakeNetwork()

        def set_page_load_timeout(self, value):
            pass

        def quit(self):
            pass

    class FakeOptions:
        last = None

        def __init__(self):
            FakeOptions.last = self
            self.profile = None
            self.page_load_strategy = None
            self.enable_bidi = False

        def add_argument(self, value):
            pass

        def set_preference(self, name, value):
            pass

    driver = FakeDriver()
    webdriver = types.ModuleType("selenium.webdriver")
    webdriver.Firefox = lambda **kwargs: driver
    selenium = types.ModuleType("selenium")
    selenium.webdriver = webdriver
    proxy_module = types.ModuleType("selenium.webdriver.common.proxy")
    proxy_module.Proxy = lambda value: value
    options_module = types.ModuleType("selenium.webdriver.firefox.options")
    options_module.Options = FakeOptions
    service_module = types.ModuleType("selenium.webdriver.firefox.service")
    service_module.Service = lambda **kwargs: object()
    fake_modules = {
        "selenium": selenium,
        "selenium.webdriver": webdriver,
        "selenium.webdriver.common": types.ModuleType("selenium.webdriver.common"),
        "selenium.webdriver.common.proxy": proxy_module,
        "selenium.webdriver.firefox": types.ModuleType("selenium.webdriver.firefox"),
        "selenium.webdriver.firefox.options": options_module,
        "selenium.webdriver.firefox.service": service_module,
    }

    with patch.dict(sys.modules, fake_modules):
        adapter = worker.SeleniumFirefoxAdapter({**worker.DEFAULTS, "geckodriver_path": ""})

    assert FakeOptions.last.enable_bidi is True
    assert FakeOptions.last.page_load_strategy == "eager"
    assert len(driver.network.request_handlers) == 1
    assert [event for event, _ in driver.network.event_handlers] == ["response_completed", "fetch_error"]
    adapter.close()


@pytest.mark.parametrize("currency", [None, "HKD"])
def test_delivery_context_requires_explicit_usd(currency):
    worker = load_worker()

    class Element:
        def __init__(self, text="", value=None):
            self.text = text
            self.value = value

        def get_attribute(self, name):
            return self.value if name == "value" else None

    class Driver:
        def find_element(self, by, value):
            if value == "glow-ingress-line1":
                return Element("Delivering to Los Angeles 90001")
            if value == "glow-ingress-line2":
                return Element("Update location")
            if value == "currencyOfPreference" and currency is not None:
                return Element(value=currency)
            raise LookupError(value)

    assert worker.delivery_context_confirmed(Driver(), "90001") is False


def test_firefox_adapter_fails_closed_when_bidi_network_is_missing():
    worker = load_worker()

    class FakeDriver:
        quit_called = False

        def set_page_load_timeout(self, value):
            pass

        def quit(self):
            self.quit_called = True

    class FakeOptions:
        def __init__(self):
            self.profile = None

        def add_argument(self, value):
            pass

        def set_preference(self, name, value):
            pass

    driver = FakeDriver()
    webdriver = types.ModuleType("selenium.webdriver")
    webdriver.Firefox = lambda **kwargs: driver
    selenium = types.ModuleType("selenium")
    selenium.webdriver = webdriver
    proxy_module = types.ModuleType("selenium.webdriver.common.proxy")
    proxy_module.Proxy = lambda value: value
    options_module = types.ModuleType("selenium.webdriver.firefox.options")
    options_module.Options = FakeOptions
    service_module = types.ModuleType("selenium.webdriver.firefox.service")
    service_module.Service = lambda **kwargs: object()
    fake_modules = {
        "selenium": selenium,
        "selenium.webdriver": webdriver,
        "selenium.webdriver.common": types.ModuleType("selenium.webdriver.common"),
        "selenium.webdriver.common.proxy": proxy_module,
        "selenium.webdriver.firefox": types.ModuleType("selenium.webdriver.firefox"),
        "selenium.webdriver.firefox.options": options_module,
        "selenium.webdriver.firefox.service": service_module,
    }

    with patch.dict(sys.modules, fake_modules), pytest.raises(RuntimeError, match="BiDi"):
        worker.SeleniumFirefoxAdapter({**worker.DEFAULTS, "geckodriver_path": ""})

    assert driver.quit_called is True


def test_firefox_fetch_sanitizes_missing_window_and_cleans_invalid_session():
    worker = load_worker()
    from selenium.common.exceptions import NoSuchWindowException

    class FakeNetwork:
        def add_request_handler(self, callback):
            return "request-handler"

        def add_event_handler(self, event, callback):
            return 1 if event == "response_completed" else 2

        def remove_request_handler(self, handler_id):
            pass

        def remove_event_handler(self, event, handler_id):
            pass

    class FakeDriver:
        def __init__(self):
            self.network = FakeNetwork()
            self.quit_called = False

        @property
        def current_window_handle(self):
            raise NoSuchWindowException("Browsing context has been discarded webdriver-secret")

        def set_page_load_timeout(self, value):
            pass

        def quit(self):
            self.quit_called = True

    class FakeOptions:
        def __init__(self):
            self.profile = None

        def add_argument(self, value):
            pass

        def set_preference(self, name, value):
            pass

    driver = FakeDriver()
    webdriver = types.ModuleType("selenium.webdriver")
    webdriver.Firefox = lambda **kwargs: driver
    selenium_module = types.ModuleType("selenium")
    selenium_module.webdriver = webdriver
    proxy_module = types.ModuleType("selenium.webdriver.common.proxy")
    proxy_module.Proxy = lambda value: value
    options_module = types.ModuleType("selenium.webdriver.firefox.options")
    options_module.Options = FakeOptions
    service_module = types.ModuleType("selenium.webdriver.firefox.service")
    service_module.Service = lambda **kwargs: object()
    fake_modules = {
        "selenium": selenium_module,
        "selenium.webdriver": webdriver,
        "selenium.webdriver.common": types.ModuleType("selenium.webdriver.common"),
        "selenium.webdriver.common.proxy": proxy_module,
        "selenium.webdriver.firefox": types.ModuleType("selenium.webdriver.firefox"),
        "selenium.webdriver.firefox.options": options_module,
        "selenium.webdriver.firefox.service": service_module,
    }

    with patch.dict(sys.modules, fake_modules):
        adapter = worker.SeleniumFirefoxAdapter({**worker.DEFAULTS, "geckodriver_path": ""})
    profile_path = Path(adapter._temp_profile.name)

    with pytest.raises(worker.AdapterFetchError) as raised:
        adapter.fetch("https://www.amazon.com/dp/B00RCPDCQU")

    assert str(raised.value) == "Firefox browser session is unavailable"
    assert "webdriver-secret" not in str(raised.value)
    assert driver.quit_called is True
    assert profile_path.exists() is False
    assert adapter.last_traffic["main_document_bytes"] is None
    assert adapter.last_traffic["subresource_bytes"] is None


class FakeRequest:
    def __init__(self, resource_type: str, url: str, context: str | None = None):
        self.resource_type = resource_type
        self.url = url
        self.failed = False
        self._params = {"context": context} if context is not None else {}

    def fail(self):
        self.failed = True


@pytest.mark.parametrize(
    ("resource_type", "blocked"),
    [
        ("image", True),
        ("font", True),
        ("media", True),
        ("document", False),
        ("script", False),
        ("stylesheet", False),
        ("style", False),
        ("xhr", False),
        ("fetch", False),
    ],
)
def test_browser_network_policy_blocks_only_nonessential_resource_types(resource_type, blocked):
    worker = load_worker()
    ledger = worker.BrowserNetworkLedger()
    request = FakeRequest(resource_type, f"https://www.amazon.com/assets/{resource_type}")

    ledger.handle_request(request)

    assert request.failed is blocked


def test_browser_network_policy_blocks_explicit_ad_telemetry_but_keeps_business_requests():
    worker = load_worker()
    ledger = worker.BrowserNetworkLedger()
    ad = FakeRequest("script", "https://aax.amazon-adsystem.com/e/dtb/bid")
    telemetry = FakeRequest("xhr", "https://fls-na.amazon.com/1/batch/1/OP/")
    business = FakeRequest("xhr", "https://www.amazon.com/hz/location")
    business_same_path = FakeRequest("fetch", "https://www.amazon.com/1/batch/order-status")

    for request in (ad, telemetry, business, business_same_path):
        ledger.handle_request(request)

    assert ad.failed is True
    assert telemetry.failed is True
    assert business.failed is False
    assert business_same_path.failed is False


def test_browser_network_ledger_keeps_main_and_subresource_bytes_separate_and_unknown_nullable():
    worker = load_worker()
    ledger = worker.BrowserNetworkLedger()
    document = FakeRequest("document", "https://www.amazon.com/dp/B00RCPDCQU")
    script = FakeRequest("script", "https://www.amazon.com/assets/app.js")
    ledger.handle_request(document)
    ledger.handle_request(script)
    ledger.handle_response_completed({"response": {"url": document.url, "bytesReceived": 1200}})
    ledger.handle_response_completed({"response": {"url": script.url}})

    snapshot = ledger.snapshot()

    assert snapshot["main_document_bytes"] == 1200
    assert snapshot["subresource_bytes"] is None
    assert snapshot["main_document_unknown_count"] == 0
    assert snapshot["subresource_unknown_count"] == 1
    assert snapshot["blocked_resource_counts"] == {}


def test_browser_network_ledger_handles_fetch_error_pending_and_iframe_documents():
    worker = load_worker()
    ledger = worker.BrowserNetworkLedger()
    ledger.reset(top_context_id="top-context")
    top = FakeRequest("document", "https://www.amazon.com/dp/B00RCPDCQU", "top-context")
    iframe = FakeRequest("document", "https://www.amazon.com/widgets/frame", "frame-context")
    failed = FakeRequest("xhr", "https://www.amazon.com/api/failure", "top-context")
    pending = FakeRequest("script", "https://www.amazon.com/assets/pending.js", "top-context")
    for request in (top, iframe, failed, pending):
        ledger.handle_request(request)
    ledger.handle_response_completed({"response": {"url": top.url, "bytesReceived": 1000}})
    ledger.handle_response_completed({"response": {"url": iframe.url, "bytesReceived": 200}})
    ledger.handle_fetch_error({"request": {"url": failed.url}, "errorText": "connection reset"})

    snapshot = ledger.snapshot()

    assert snapshot["main_document_bytes"] == 1000
    assert snapshot["main_document_unknown_count"] == 0
    assert snapshot["subresource_bytes"] is None
    assert snapshot["subresource_known_count"] == 1
    assert snapshot["subresource_unknown_count"] == 2


def test_browser_network_ledger_marks_unmatched_redirect_response_unknown():
    worker = load_worker()
    ledger = worker.BrowserNetworkLedger()
    ledger.reset(top_context_id="top-context")
    top = FakeRequest("document", "https://www.amazon.com/dp/B00RCPDCQU", "top-context")
    ledger.handle_request(top)
    ledger.handle_response_completed({"response": {"url": top.url, "bytesReceived": 1000}})
    ledger.handle_response_completed(
        {"response": {"url": "https://www.amazon.com/gp/redirected/B00RCPDCQU", "bytesReceived": 50}}
    )

    snapshot = ledger.snapshot()

    assert snapshot["main_document_bytes"] is None
    assert snapshot["subresource_bytes"] is None
    assert snapshot["main_document_unknown_count"] == 1
    assert snapshot["subresource_unknown_count"] == 1


def test_fallback_reason_is_enumerated_and_deduplicated_by_run_asin_reason():
    worker = load_worker()
    ledger = worker.BrowserFallbackLedger()

    assert ledger.claim("run-1", "B00RCPDCQU", worker.FallbackReason.MISSING_TITLE) is True
    assert ledger.claim("run-1", "B00RCPDCQU", worker.FallbackReason.MISSING_TITLE) is False
    assert ledger.claim("run-2", "B00RCPDCQU", worker.FallbackReason.MISSING_TITLE) is True
    with pytest.raises((TypeError, ValueError)):
        ledger.claim("run-1", "B00RCPDCQU", "free-form-reason")


class OneProductStorage:
    tenant_id = "tenant-a"

    def __init__(self):
        self.claims = 0
        self.saved = []

    def claim_task(self, worker_id, lease_seconds=None):
        self.claims += 1
        if self.claims > 1:
            return None
        return {
            "asin": "B00RCPDCQU",
            "marketplace": "US",
            "url": "https://www.amazon.com/dp/B00RCPDCQU",
            "status": "running",
            "task_stage": "product",
            "lease_token": "token-1",
            "lease_owner": worker_id,
            "reported_review_count": 0,
            "fetched_review_count": 0,
            "review_pages_fetched": 0,
        }

    def save_product_result(self, **payload):
        self.saved.append(payload)
        return True

    def save_failure(self, **payload):
        self.saved.append(payload)
        return True


def test_postgres_http_and_browser_transport_failures_persist_action_evidence():
    worker = load_worker()

    class DoubleFailureAdapter:
        source_type = "http_html"
        last_transfer_bytes = 0
        last_retry_after_seconds = None

        def fetch(self, url):
            raise worker.AdapterFetchError("HTTP transport unavailable")

        def fetch_browser(self, url, *, fallback_reason, run_id, asin):
            raise worker.AdapterFetchError("Firefox browser session is unavailable")

    storage = OneProductStorage()
    config = {
        **worker.DEFAULTS,
        "max_actions_per_run": 1,
        "raw_html_dir": None,
        "context": {"expected_country": "US", "expected_currency": "USD", "postal_code": "90001"},
    }

    assert worker.run_postgres_actions(
        storage, DoubleFailureAdapter(), config, limit=1, run_id="run-window-loss", worker_id="worker-a"
    ) == 1

    payload = storage.saved[0]
    assert payload["reason"] == "fetch_error"
    assert payload["evidence"]["run_id"] == "run-window-loss"
    assert payload["evidence"]["error_code"] == "fetch_error"
    assert payload["evidence"]["source_type"] == "http_html"
    assert payload["evidence"]["context_json"]["fallback_reason"] == "http_transport_error"
    assert payload["evidence"]["context_json"]["traffic"]["firefox_main_document_bytes"] is None


def test_sqlite_http_and_browser_transport_failures_persist_action_evidence_and_finish_task():
    worker = load_worker()

    class DoubleFailureAdapter:
        source_type = "http_html"
        last_transfer_bytes = 0
        last_retry_after_seconds = None

        def fetch(self, url):
            raise worker.AdapterFetchError("HTTP transport unavailable")

        def fetch_browser(self, url, *, fallback_reason, run_id, asin):
            raise worker.AdapterFetchError("Firefox browser session is unavailable")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        manifest = root / "manifest.csv"
        manifest.write_text(
            "\ufeffasin,url,marketplace,source_site_label,source_workbook\n"
            "B00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.xlsx\n",
            encoding="utf-8",
        )
        conn = worker.init_db(root / "state.sqlite3")
        config = {
            **worker.DEFAULTS,
            "max_actions_per_run": 1,
            "output_dir": root / "out",
            "raw_html_dir": None,
            "context": {"expected_country": "US", "expected_currency": "USD", "postal_code": "90001"},
        }
        worker.initialize_manifest(conn, manifest, config)

        assert worker.run_actions(
            conn, DoubleFailureAdapter(), config, limit=1, run_id="run-window-loss"
        ) == 1
        state = conn.execute(
            "SELECT status,last_error FROM item_state WHERE marketplace='US' AND asin='B00RCPDCQU'"
        ).fetchone()
        evidence = conn.execute(
            "SELECT run_id,error_code,http_status,raw_html_path,context_json "
            "FROM collection_evidence WHERE asin='B00RCPDCQU' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()

    assert tuple(state) == ("failed", "HTTP transport unavailable")
    assert evidence["run_id"] == "run-window-loss"
    assert evidence["error_code"] == "fetch_error"
    assert evidence["http_status"] is None
    assert evidence["raw_html_path"] is None
    context = json.loads(evidence["context_json"])
    assert context["fallback_reason"] == "http_transport_error"
    assert context["traffic"]["firefox_main_document_bytes"] is None


class ReviewOnlyStorage(OneProductStorage):
    def claim_task(self, worker_id, lease_seconds=None):
        self.claims += 1
        if self.claims > 1:
            return None
        return {
            "asin": "B00RCPDCQU",
            "marketplace": "US",
            "url": "https://www.amazon.com/dp/B00RCPDCQU",
            "status": "running",
            "task_stage": "reviews",
            "next_review_page": 1,
            "next_review_url": "https://www.amazon.com/product-reviews/B00RCPDCQU",
            "lease_token": "token-1",
            "lease_owner": worker_id,
            "reported_review_count": 1,
            "reported_rating_count": 1,
            "reported_count_source": "header",
            "fetched_review_count": 0,
            "review_pages_fetched": 0,
        }

    def save_review_result(self, **payload):
        self.saved.append(payload)
        return True


class ReviewOnlyFallbackAdapter:
    source_type = "http_html"
    last_transfer_bytes = 25
    last_retry_after_seconds = None

    def __init__(self):
        self.commits = []

    def fetch(self, url):
        return "<html><body>No review cards</body></html>", 200

    def fetch_browser(self, url, *, fallback_reason, run_id, asin):
        self.source_type = "selenium_dom"
        self.last_transfer_bytes = None
        self.last_browser_traffic = {
            "main_document_bytes": None,
            "subresource_bytes": None,
            "main_document_unknown_count": 1,
            "subresource_unknown_count": 1,
        }
        return """
        <div data-hook="review" id="R1">
          <i data-hook="review-star-rating">5.0 out of 5 stars</i>
          <a data-hook="review-title">Good</a><span data-hook="review-body">Works</span>
        </div>
        """, 200

    def commit_browser_context(self, run_id, *, context_confirmed):
        self.commits.append((run_id, context_confirmed))
        return 1


def test_postgres_review_only_browser_fallback_commits_confirmed_cookie_context():
    worker = load_worker()
    storage = ReviewOnlyStorage()
    adapter = ReviewOnlyFallbackAdapter()
    config = {**worker.DEFAULTS, "max_actions_per_run": 1, "raw_html_dir": None, "context": {}}

    assert worker.run_postgres_actions(storage, adapter, config, limit=1, run_id="run-review", worker_id="worker-a") == 1

    assert adapter.commits == [("run-review", True)]
    assert storage.saved[0]["next_status"] == "succeeded"


def test_sqlite_review_only_browser_fallback_commits_confirmed_cookie_context():
    worker = load_worker()
    adapter = ReviewOnlyFallbackAdapter()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        manifest = root / "manifest.csv"
        manifest.write_text(
            "\ufeffasin,url,marketplace,source_site_label,source_workbook\n"
            "B00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.xlsx\n",
            encoding="utf-8",
        )
        conn = worker.init_db(root / "state.sqlite3")
        config = {
            **worker.DEFAULTS,
            "max_actions_per_run": 1,
            "review_page_limit": 0,
            "output_dir": root / "out",
            "raw_html_dir": None,
            "context": {},
        }
        worker.initialize_manifest(conn, manifest, config)
        worker._set_status(conn, "US", "B00RCPDCQU", "running", reason="test")
        worker._set_status(
            conn,
            "US",
            "B00RCPDCQU",
            "reviews_pending",
            reason="test-review",
            task_stage="reviews",
            resume_status="reviews_pending",
            next_review_url="https://www.amazon.com/product-reviews/B00RCPDCQU",
            next_review_page=1,
            reported_review_count=1,
        )

        assert worker.run_actions(conn, adapter, config, limit=1, run_id="run-review") == 1
        state = conn.execute("SELECT status,fetched_review_count FROM item_state WHERE asin='B00RCPDCQU'").fetchone()
        conn.close()

    assert adapter.commits == [("run-review", True)]
    assert tuple(state) == ("succeeded", 1)


def test_postgres_cookie_bridge_failure_is_audited_without_leaving_review_lease_running():
    worker = load_worker()

    class FailingBridgeAdapter(ReviewOnlyFallbackAdapter):
        def commit_browser_context(self, run_id, *, context_confirmed):
            raise worker.AdapterFetchError("isolated cookie export failed")

    storage = ReviewOnlyStorage()
    adapter = FailingBridgeAdapter()
    config = {**worker.DEFAULTS, "max_actions_per_run": 1, "raw_html_dir": None, "context": {}}

    assert worker.run_postgres_actions(storage, adapter, config, limit=1, run_id="run-review", worker_id="worker-a") == 1

    assert storage.saved[0]["next_status"] == "succeeded"
    assert storage.saved[0]["evidence"]["context_json"]["cookie_bridge"] == {
        "status": "failed", "error_code": "cookie_bridge_error"
    }


def test_sqlite_cookie_bridge_failure_is_audited_without_losing_review_result():
    worker = load_worker()

    class FailingBridgeAdapter(ReviewOnlyFallbackAdapter):
        def commit_browser_context(self, run_id, *, context_confirmed):
            raise worker.AdapterFetchError("isolated cookie export failed")

    adapter = FailingBridgeAdapter()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        manifest = root / "manifest.csv"
        manifest.write_text(
            "\ufeffasin,url,marketplace,source_site_label,source_workbook\n"
            "B00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.xlsx\n",
            encoding="utf-8",
        )
        conn = worker.init_db(root / "state.sqlite3")
        config = {
            **worker.DEFAULTS,
            "max_actions_per_run": 1,
            "review_page_limit": 0,
            "output_dir": root / "out",
            "raw_html_dir": None,
            "context": {},
        }
        worker.initialize_manifest(conn, manifest, config)
        worker._set_status(conn, "US", "B00RCPDCQU", "running", reason="test")
        worker._set_status(
            conn,
            "US",
            "B00RCPDCQU",
            "reviews_pending",
            reason="test-review",
            task_stage="reviews",
            resume_status="reviews_pending",
            next_review_url="https://www.amazon.com/product-reviews/B00RCPDCQU",
            next_review_page=1,
            reported_review_count=1,
        )

        assert worker.run_actions(conn, adapter, config, limit=1, run_id="run-review") == 1
        state = conn.execute("SELECT status,fetched_review_count FROM item_state WHERE asin='B00RCPDCQU'").fetchone()
        context = json.loads(conn.execute("SELECT context_json FROM collection_evidence ORDER BY id DESC LIMIT 1").fetchone()[0])
        conn.close()

    assert tuple(state) == ("succeeded", 1)
    assert context["cookie_bridge"] == {"status": "failed", "error_code": "cookie_bridge_error"}


def test_product_fallback_reason_and_nullable_browser_traffic_are_persisted_in_evidence():
    worker = load_worker()

    class Adapter:
        source_type = "http_html"
        last_transfer_bytes = 100
        action_http_transfer_bytes = 100
        last_retry_after_seconds = None

        def __init__(self):
            self.browser_calls = []

        def fetch(self, url):
            return """
            <html><head><link rel="canonical" href="https://www.amazon.com/dp/B00RCPDCQU"></head><body>
              <input id="ASIN" value="B00RCPDCQU"><span id="productTitle">Example</span>
              <span class="a-price"><span class="a-offscreen">$19.99</span></span>
              <div id="desktop_buybox">Delivering to Portland 97230</div>
            </body></html>
            """, 200

        def fetch_browser(self, url, *, fallback_reason, run_id, asin):
            self.browser_calls.append((fallback_reason.value, run_id, asin))
            self.source_type = "selenium_dom"
            self.last_transfer_bytes = None
            self.last_browser_traffic = {
                "main_document_bytes": None,
                "subresource_bytes": 400,
                "main_document_known_count": 0,
                "subresource_known_count": 1,
                "main_document_unknown_count": 1,
                "subresource_unknown_count": 0,
                "blocked_resource_counts": {"image": 3},
            }
            return """
            <html><head><link rel="canonical" href="https://www.amazon.com/dp/B00RCPDCQU"></head><body>
              <input id="ASIN" value="B00RCPDCQU"><span id="productTitle">Example</span>
              <span class="a-price"><span class="a-offscreen">$19.99</span></span>
              <div id="desktop_buybox">Delivering to Los Angeles 90001</div>
            </body></html>
            """, 200

    storage = OneProductStorage()
    adapter = Adapter()
    config = {
        **worker.DEFAULTS,
        "max_actions_per_run": 1,
        "raw_html_dir": None,
        "context": {"expected_country": "US", "expected_currency": "USD", "postal_code": "90001"},
    }

    assert worker.run_postgres_actions(storage, adapter, config, limit=1, run_id="run-1", worker_id="worker-a") == 1

    assert adapter.browser_calls == [("context_mismatch", "run-1", "B00RCPDCQU")]
    context = storage.saved[0]["evidence"]["context_json"]
    assert context["fallback_reason"] == "context_mismatch"
    assert context["fallback_reasons"] == ["context_mismatch"]
    assert context["traffic"]["http_compressed_response_bytes"] == 100
    assert context["traffic"]["firefox_main_document_bytes"] is None
    assert context["traffic"]["firefox_subresource_bytes"] == 400
    assert context["traffic"]["blocked_resource_counts"] == {"image": 3}


@pytest.mark.parametrize(
    ("status", "body", "expected_reason"),
    [
        (403, "Access denied", "http_403"),
        (429, "Too many requests", "http_429"),
        (200, "<html><title>Robot Check</title>captcha</html>", "robot"),
        (202, "<script>window.awsWafCookieDomainList=[]</script>", "waf_challenge"),
        (200, "<html><title>Amazon Sign-In</title><body>Sign in to continue</body></html>", "login_wall"),
    ],
)
def test_blocking_response_never_starts_firefox(status, body, expected_reason):
    worker = load_worker()

    class Adapter:
        source_type = "http_html"
        last_transfer_bytes = 10
        last_retry_after_seconds = None

        def fetch(self, url):
            return body, status

        def fetch_browser(self, *args, **kwargs):
            raise AssertionError("blocking responses must not start Firefox")

    storage = OneProductStorage()
    config = {**worker.DEFAULTS, "max_actions_per_run": 1, "raw_html_dir": None, "context": {}}

    expected_exit = -1 if expected_reason else 1
    assert worker.run_postgres_actions(storage, Adapter(), config, limit=1, run_id="run-1", worker_id="worker-a") == expected_exit
    assert storage.saved[0]["reason"] == expected_reason


def test_explicit_different_asin_redirect_and_noncore_description_gap_do_not_start_firefox():
    worker = load_worker()

    class Adapter:
        source_type = "http_html"
        last_transfer_bytes = 10
        last_retry_after_seconds = None

        def __init__(self, html):
            self.html = html

        def fetch(self, url):
            return self.html, 200

        def fetch_browser(self, *args, **kwargs):
            raise AssertionError("this response is not eligible for Firefox")

    redirected = """
    <html><head><link rel="canonical" href="https://www.amazon.com/dp/B00RCPDI50"></head>
    <body><span id="productTitle">Replacement ASIN</span></body></html>
    """
    valid_without_description = """
    <html><head><link rel="canonical" href="https://www.amazon.com/dp/B00RCPDCQU"></head>
    <body><input id="ASIN" value="B00RCPDCQU"><span id="productTitle">Valid title</span></body></html>
    """
    config = {**worker.DEFAULTS, "max_actions_per_run": 1, "raw_html_dir": None, "context": {}}

    first = OneProductStorage()
    worker.run_postgres_actions(first, Adapter(redirected), config, limit=1, run_id="run-1", worker_id="worker-a")
    assert first.saved[0]["reason"] == "asin_mismatch"

    second = OneProductStorage()
    worker.run_postgres_actions(second, Adapter(valid_without_description), config, limit=1, run_id="run-2", worker_id="worker-a")
    assert second.saved[0]["next_status"] == "succeeded"


def test_trade_in_sign_in_prompt_on_product_page_is_not_a_login_wall_and_keeps_asin_mismatch():
    worker = load_worker()
    product_html = """
    <html><head>
      <title>Amazon.com: Valid product</title>
      <link rel="canonical" href="https://www.amazon.com/example/dp/B07FMMYMQQ">
    </head><body>
      <input id="ASIN" name="ASIN" value="B07FMMYMQQ">
      <span id="productTitle">Valid product title</span>
      <div class="unified-trade-in aok-hidden">
        <a href="#" role="button">Sign in to continue</a>
      </div>
    </body></html>
    """

    assert worker.classify_block(200, product_html) is None

    class Storage(OneProductStorage):
        def claim_task(self, worker_id, lease_seconds=None):
            task = super().claim_task(worker_id, lease_seconds)
            if task is not None:
                task["asin"] = "B07FMLVYDZ"
                task["url"] = "https://www.amazon.com/dp/B07FMLVYDZ"
            return task

    class Adapter:
        source_type = "http_html"
        last_transfer_bytes = 362269
        last_retry_after_seconds = None

        def fetch(self, url):
            return product_html, 200

        def fetch_browser(self, *args, **kwargs):
            raise AssertionError("explicit ASIN mismatch must not start Firefox")

    storage = Storage()
    config = {**worker.DEFAULTS, "max_actions_per_run": 1, "raw_html_dir": None, "context": {}}

    assert worker.run_postgres_actions(
        storage, Adapter(), config, limit=1, run_id="run-trade-in", worker_id="worker-a"
    ) == 1
    assert storage.saved[0]["reason"] == "asin_mismatch"


@pytest.mark.parametrize(
    "login_html",
    [
        "<html><title>Amazon Sign-In</title><body>Sign in to continue</body></html>",
        "<html><title>Account</title><body><form action='/ap/signin'><input id='ap_email'><button id='signInSubmit'>Sign in</button></form></body></html>",
    ],
)
def test_explicit_authentication_page_remains_a_login_wall(login_html):
    worker = load_worker()
    assert worker.classify_block(200, login_html) == "login_wall"
