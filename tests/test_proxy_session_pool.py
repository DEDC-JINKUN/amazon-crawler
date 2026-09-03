from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_pool():
    spec = importlib.util.spec_from_file_location("proxy_session_pool_test", ROOT / "scripts" / "proxy_session_pool.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeAdapter:
    def __init__(self, config, scripted):
        self.config = config
        self.scripted = scripted
        self.cookie_jar = {}
        self.last_transfer_bytes = 0
        self.action_http_transfer_bytes = 0
        self.source_type = "http_html"
        self.closed = False

    def begin_run(self, *args):
        self.run_scope = args

    def begin_action(self):
        self.action_http_transfer_bytes = 0

    def fetch(self, url):
        result = self.scripted.pop(0)
        if isinstance(result, Exception):
            raise result
        body, status, size = result
        self.last_transfer_bytes = size
        self.action_http_transfer_bytes += size
        return body, status

    def close(self):
        self.cookie_jar.clear()
        self.closed = True


def config(**overrides):
    value = {
        "proxy_url": "http://proxy.example:10000",
        "proxy_session_ports": [10000, 10001, 10002, 10003],
        "proxy_session_mode": "sticky",
        "proxy_session_max_asins": 2,
        "proxy_session_retry_per_asin": 1,
        "proxy_session_consecutive_block_limit": 2,
        "proxy_session_window_size": 20,
        "proxy_session_window_block_limit": 3,
        "proxy_product_session_scope": "bounded",
    }
    value.update(overrides)
    return value


def classifier(status, body):
    return "captcha" if "captcha" in body.lower() else "http_403" if status == 403 else None


def test_rotates_after_two_asins_and_keeps_cookie_jars_isolated_until_close():
    module = load_pool()
    adapters = []

    def factory(slot_config):
        adapter = FakeAdapter(slot_config, [("product", 200, 100)] * 2)
        adapters.append(adapter)
        return adapter

    pool = module.ProxySessionPool(config(), factory, classifier)
    pool.begin_run("run-1", "tenant-a", "worker-a")
    for asin in ("B000000001", "B000000002", "B000000003", "B000000004"):
        pool.begin_action()
        pool.fetch(f"https://www.amazon.com/dp/{asin}")
        pool.record_outcome("completed")

    assert len(adapters) == 2
    assert adapters[0].config["proxy_url"].endswith(":10000")
    assert adapters[1].config["proxy_url"].endswith(":10001")
    adapters[0].cookie_jar["session"] = "first-secret"
    assert adapters[1].cookie_jar == {}
    assert [item["asin_count"] for item in pool.evidence_context()["sessions"]] == [2, 2]

    pool.close()
    assert all(adapter.closed and adapter.cookie_jar == {} for adapter in adapters)


def test_captcha_quarantines_session_and_retries_same_asin_once_on_new_session():
    module = load_pool()
    scripts = [[("captcha page", 200, 25)], [("product", 200, 100)]]
    adapters = []

    def factory(slot_config):
        adapter = FakeAdapter(slot_config, scripts[len(adapters)])
        adapters.append(adapter)
        return adapter

    pool = module.ProxySessionPool(config(), factory, classifier)
    pool.begin_run("run-1", "tenant-a", "worker-a")
    body, status = pool.fetch("https://www.amazon.com/dp/B000000001")
    pool.record_outcome("completed")

    assert (body, status) == ("product", 200)
    assert len(adapters) == 2
    intermediate = pool.drain_intermediate_attempts()
    assert len(intermediate) == 1
    assert intermediate[0]["block_reason"] == "captcha"
    assert intermediate[0]["session_id"] == "session-01"
    assert pool.evidence_context()["sessions"][0]["quarantine_reason"] == "captcha"


def test_consecutive_and_window_breakers_stop_without_unbounded_rotation():
    module = load_pool()

    def factory(slot_config):
        return FakeAdapter(slot_config, [("captcha", 200, 10)])

    pool = module.ProxySessionPool(config(proxy_session_retry_per_asin=0), factory, classifier)
    pool.begin_run("run-1", "tenant-a", "worker-a")
    for asin in ("B000000001", "B000000002"):
        body, _ = pool.fetch(f"https://www.amazon.com/dp/{asin}")
        assert body == "captcha"
        pool.record_outcome("blocked")
    assert pool.circuit_open_reason == "consecutive_blocked_asins"
    with pytest.raises(module.ProxyCircuitOpen, match="consecutive"):
        pool.fetch("https://www.amazon.com/dp/B000000003")

    sequence = iter(["captcha", "product", "captcha", "product", "captcha"])

    def window_factory(slot_config):
        return FakeAdapter(slot_config, [(next(sequence), 200, 10)])

    window = module.ProxySessionPool(
        config(
            proxy_session_ports=[10000, 10001, 10002, 10003, 10004],
            proxy_session_max_asins=1,
            proxy_session_retry_per_asin=0,
            proxy_session_consecutive_block_limit=5,
        ),
        window_factory,
        classifier,
    )
    window.begin_run("run-2", "tenant-a", "worker-a")
    for index in range(5):
        body, _ = window.fetch(f"https://www.amazon.com/dp/B0000001{index:02d}")
        window.record_outcome("blocked" if body == "captcha" else "completed")
    assert window.circuit_open_reason == "rolling_blocked_asin_limit"


def test_network_errors_are_separate_and_sensitive_values_never_enter_context():
    module = load_pool()
    secret = "proxy-password-secret"

    def factory(slot_config):
        adapter = FakeAdapter(slot_config, [TimeoutError(secret)])
        adapter.cookie_jar["auth"] = "cookie-secret"
        return adapter

    pool = module.ProxySessionPool(config(), factory, classifier)
    pool.begin_run("run-1", "tenant-a", "worker-a")
    with pytest.raises(TimeoutError):
        pool.fetch("https://www.amazon.com/dp/B000000001")

    rendered = repr(pool.evidence_context())
    assert "network_error" in rendered
    assert secret not in rendered
    assert "cookie-secret" not in rendered
    assert "proxy.example" not in rendered
    assert pool.circuit_open_reason is None


def test_exhausted_pool_returns_last_real_block_and_opens_circuit_without_throwing():
    module = load_pool()

    def factory(slot_config):
        return FakeAdapter(slot_config, [("captcha", 200, 10)])

    pool = module.ProxySessionPool(config(proxy_session_ports=[10000]), factory, classifier)
    pool.begin_run("run-1", "tenant-a", "worker-a")
    body, status = pool.fetch("https://www.amazon.com/dp/B000000001")

    assert (body, status) == ("captcha", 200)
    assert pool.circuit_open_reason == "session_pool_exhausted"
    assert pool.can_claim_new_asin() is False


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("proxy_session_max_asins", 0),
        ("proxy_session_max_asins", 6),
        ("proxy_session_retry_per_asin", 2),
        ("proxy_session_consecutive_block_limit", 0),
        ("proxy_session_window_size", 101),
        ("proxy_session_window_block_limit", 21),
    ],
)
def test_rejects_unsafe_limits(name, value):
    module = load_pool()
    with pytest.raises(ValueError):
        module.ProxySessionPool(config(**{name: value}), lambda cfg: None, classifier)


def test_rejects_more_than_forty_approved_ports():
    module = load_pool()
    with pytest.raises(ValueError, match="1 to 40 approved ports"):
        module.ProxySessionPool(
            config(proxy_session_ports=list(range(10000, 10041))),
            lambda cfg: None,
            classifier,
        )


def load_worker():
    spec = importlib.util.spec_from_file_location("amazon_worker_pool_test", ROOT / "scripts" / "amazon_us_worker.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ProductStorage:
    tenant_id = "tenant-a"

    def __init__(self, count=1):
        self.remaining = count
        self.saved = []

    def claim_task(self, worker_id, lease_seconds=None):
        if self.remaining <= 0:
            return None
        index = self.remaining
        self.remaining -= 1
        asin = f"B000000{index:03d}"
        return {
            "asin": asin,
            "url": f"https://www.amazon.com/dp/{asin}",
            "task_stage": "product",
            "lease_token": f"lease-{index}",
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


def product_html(asin):
    return f"""
    <html><head><link rel='canonical' href='https://www.amazon.com/dp/{asin}'></head><body>
      <input id='ASIN' value='{asin}'><span id='productTitle'>Product</span>
    </body></html>
    """


def test_postgres_runner_records_retry_attribution_and_sanitized_session_metrics():
    worker = load_worker()
    pool_module = load_pool()
    scripts = [[("captcha", 200, 25)], [(product_html("B000000001"), 200, 100)]]
    adapters = []

    def factory(slot_config):
        adapter = FakeAdapter(slot_config, scripts[len(adapters)])
        adapters.append(adapter)
        return adapter

    pool = pool_module.ProxySessionPool(config(), factory, worker.classify_block)
    storage = ProductStorage()
    worker_config = {**worker.DEFAULTS, "max_actions_per_run": 1, "raw_html_dir": None, "context": {}}

    assert worker._run_postgres_actions_impl(
        storage, pool, worker_config, limit=1, run_id="run-pool", worker_id="worker-a"
    ) == 1
    context = storage.saved[0]["evidence"]["context_json"]["proxy_session_pool"]
    traffic = storage.saved[0]["evidence"]["context_json"]["traffic"]

    assert "product" in storage.saved[0]
    assert context["sessions"][0]["blocked"] == 1
    assert context["sessions"][1]["completed"] == 1
    assert context["attempts"][0]["block_reason"] == "captcha"
    assert context["attempts"][0]["content_hash"]
    assert "body" not in context["attempts"][0]
    assert "proxy.example" not in repr(context)
    assert traffic["http_compressed_response_bytes"] == 125


def test_postgres_runner_reports_circuit_after_two_blocked_asins_and_eighteen_unrequested():
    worker = load_worker()
    pool_module = load_pool()

    def factory(slot_config):
        return FakeAdapter(slot_config, [("captcha", 200, 10)])

    pool = pool_module.ProxySessionPool(config(), factory, worker.classify_block)
    storage = ProductStorage(count=20)
    worker_config = {**worker.DEFAULTS, "max_actions_per_run": 20, "raw_html_dir": None, "context": {}}

    assert worker._run_postgres_actions_impl(
        storage, pool, worker_config, limit=20, run_id="run-circuit", worker_id="worker-a"
    ) == -1
    context = storage.saved[-1]["evidence"]["context_json"]["proxy_session_pool"]

    assert len(storage.saved) == 2
    assert context["circuit_open_reason"] == "consecutive_blocked_asins"
    assert context["unrequested_count"] == 18
    assert len(context["attempts"]) == 2


def test_blocked_attempt_is_preserved_when_retry_session_has_network_error():
    worker = load_worker()
    pool_module = load_pool()
    scripts = [[("captcha", 200, 25)], [worker.AdapterFetchError("network unavailable")]]
    adapters = []

    def factory(slot_config):
        adapter = FakeAdapter(slot_config, scripts[len(adapters)])
        adapters.append(adapter)
        return adapter

    pool = pool_module.ProxySessionPool(config(), factory, worker.classify_block)
    storage = ProductStorage()
    worker_config = {**worker.DEFAULTS, "max_actions_per_run": 1, "raw_html_dir": None, "context": {}}

    assert worker._run_postgres_actions_impl(
        storage, pool, worker_config, limit=1, run_id="run-block-network", worker_id="worker-a"
    ) == 1
    context = storage.saved[0]["evidence"]["context_json"]["proxy_session_pool"]

    assert storage.saved[0]["reason"] == "fetch_error"
    assert len(context["attempts"]) == 1
    assert context["attempts"][0]["block_reason"] == "captcha"
    assert context["attempts"][0]["content_hash"]


def test_captcha_then_inner_retry_transport_failure_retires_slot_before_next_asin():
    worker = load_worker()
    pool_module = load_pool()
    scripts = [
        [("captcha", 200, 25)],
        [worker.AdapterFetchError("TLS retries exhausted")],
        [(product_html("B000000001"), 200, 100)],
    ]
    adapters = []

    def factory(slot_config):
        adapter = FakeAdapter(slot_config, scripts[len(adapters)])
        adapters.append(adapter)
        return adapter

    pool = pool_module.ProxySessionPool(
        config(proxy_session_ports=[10000, 10001, 10002], proxy_session_max_asins=3),
        factory,
        worker.classify_block,
    )
    pool.begin_run("run-transport-sequence", "tenant-a", "worker-a")

    with pytest.raises(worker.AdapterFetchError, match="TLS retries exhausted"):
        pool.fetch("https://www.amazon.com/dp/B000000002")
    body, status = pool.fetch("https://www.amazon.com/dp/B000000001")

    assert (body, status) == (product_html("B000000001"), 200)
    assert len(adapters) == 3
    sessions = pool.evidence_context()["sessions"]
    assert [item["session_id"] for item in sessions] == ["session-01", "session-02", "session-03"]
    assert sessions[0]["quarantine_reason"] == "captcha"
    assert sessions[1]["quarantine_reason"] == "transport_error"
    assert sessions[1]["network_error"] == 1
    assert sessions[2]["health"] == "healthy"


def test_breadth_products_rotate_per_asin_while_same_asin_review_pages_stay_sticky():
    module = load_pool()
    adapters = []

    def factory(slot_config):
        adapter = FakeAdapter(slot_config, [("ok", 200, 10)] * 4)
        adapters.append(adapter)
        return adapter

    pool = module.ProxySessionPool(
        config(proxy_product_session_scope="per_asin", proxy_session_max_asins=3),
        factory, classifier,
    )
    pool.begin_run("run-strategy", "tenant-a", "worker-a")
    pool.fetch("https://www.amazon.com/dp/B000000001")
    pool.fetch("https://www.amazon.com/dp/B000000002")
    pool.fetch("https://www.amazon.com/product-reviews/B000000002?pageNumber=1")
    pool.fetch("https://www.amazon.com/product-reviews/B000000002?pageNumber=2")

    assert len(adapters) == 2
    sessions = pool.evidence_context()["sessions"]
    strategy = pool.evidence_context()
    assert strategy["product_session_scope"] == "per_asin"
    assert strategy["product_asins_per_session"] == 1
    assert strategy["review_session_scope"] == "same_asin_sticky"
    assert [item["asin_count"] for item in sessions] == [1, 1]
    assert sessions[0]["health"] == "healthy"
    assert sessions[1]["request_count"] == 3


def test_interleaved_review_returns_to_the_original_asin_sticky_session():
    module = load_pool()
    adapters = []

    def factory(slot_config):
        adapter = FakeAdapter(slot_config, [("ok", 200, 10)] * 3)
        adapters.append(adapter)
        return adapter

    pool = module.ProxySessionPool(
        config(proxy_product_session_scope="per_asin"), factory, classifier,
    )
    pool.begin_run("run-interleaved", "tenant-a", "worker-a")
    pool.fetch("https://www.amazon.com/dp/B000000001")
    pool.fetch("https://www.amazon.com/dp/B000000002")
    pool.fetch("https://www.amazon.com/product-reviews/B000000001?pageNumber=2")

    assert len(adapters) == 2
    sessions = pool.evidence_context()["sessions"]
    assert sessions[0]["request_count"] == 2
    assert sessions[1]["request_count"] == 1
    assert pool.evidence_context()["current_session_id"] == "session-01"


def test_begin_run_clears_asin_slot_bindings_and_never_reuses_closed_adapter():
    module = load_pool()
    adapters = []

    def factory(slot_config):
        adapter = FakeAdapter(slot_config, [("ok", 200, 10)])
        adapters.append(adapter)
        return adapter

    pool = module.ProxySessionPool(
        config(proxy_product_session_scope="per_asin", proxy_session_ports=[10000, 10001]),
        factory, classifier,
    )
    pool.begin_run("run-1", "tenant-a", "worker-a")
    pool.fetch("https://www.amazon.com/dp/B000000001")
    first = adapters[0]

    pool.begin_run("run-2", "tenant-a", "worker-a")
    pool.fetch("https://www.amazon.com/dp/B000000001")

    assert len(adapters) == 2
    assert first.closed is True
    assert adapters[1].run_scope == ("run-2", "tenant-a", "worker-a")
    assert pool.evidence_context()["current_session_id"] == "session-01"


def test_per_asin_pool_reports_no_preclaim_capacity_after_last_port_is_used():
    module = load_pool()
    pool = module.ProxySessionPool(
        config(proxy_product_session_scope="per_asin", proxy_session_ports=[10000], proxy_session_max_asins=3),
        lambda slot_config: FakeAdapter(slot_config, [("ok", 200, 10)]),
        classifier,
    )
    pool.begin_run("run-1", "tenant-a", "worker-a")
    pool.fetch("https://www.amazon.com/dp/B000000001")

    assert pool.can_claim_new_asin() is False


def test_firefox_exception_quarantines_slot_without_immediate_global_circuit():
    worker = load_worker()
    pool_module = load_pool()

    class BrokenFirefoxAdapter(FakeAdapter):
        def fetch_browser(self, _url, **_kwargs):
            raise worker.AdapterFetchError("browser challenge verification failed")

    pool = pool_module.ProxySessionPool(
        config(proxy_session_retry_per_asin=0, proxy_session_consecutive_block_limit=5),
        lambda slot_config: BrokenFirefoxAdapter(slot_config, [("captcha", 200, 25)]),
        worker.classify_block,
    )
    storage = ProductStorage(count=1)
    worker_config = {
        **worker.DEFAULTS, "max_actions_per_run": 1, "raw_html_dir": None, "context": {},
        "proxy_firefox_verify_on_access_block": True,
    }

    assert worker._run_postgres_actions_impl(
        storage, pool, worker_config, limit=1, run_id="run-firefox-error", worker_id="worker-a"
    ) == 1
    assert len(storage.saved) == 1
    context = storage.saved[0]["evidence"]["context_json"]["proxy_session_pool"]
    assert context["sessions"][0]["firefox_verification"] == "failed"
    assert context["circuit_open_reason"] is None
    assert context["unrequested_count"] == 0
    assert pool._current.adapter.closed is True


def test_one_blocked_asin_after_firefox_failure_isolated_then_next_asin_succeeds():
    worker = load_worker()
    pool_module = load_pool()
    adapters = []

    class FirefoxFailureAdapter(FakeAdapter):
        def fetch_browser(self, _url, **_kwargs):
            raise worker.AdapterFetchError("browser verification failed")

    def factory(slot_config):
        index = len(adapters)
        scripted = [("captcha", 200, 25)] if index < 2 else [(product_html("B000000001"), 200, 100)]
        adapter = FirefoxFailureAdapter(slot_config, scripted)
        adapters.append(adapter)
        return adapter

    pool = pool_module.ProxySessionPool(
        config(proxy_session_retry_per_asin=1, proxy_session_consecutive_block_limit=2),
        factory, worker.classify_block,
    )
    storage = ProductStorage(count=2)
    worker_config = {
        **worker.DEFAULTS, "max_actions_per_run": 2, "raw_html_dir": None, "context": {},
        "proxy_firefox_verify_on_access_block": True,
    }

    assert worker._run_postgres_actions_impl(
        storage, pool, worker_config, limit=2, run_id="run-one-bad-asin", worker_id="worker-a"
    ) == 2
    assert len(storage.saved) == 2
    assert storage.saved[0]["reason"] == "captcha"
    assert "product" in storage.saved[1]
    first_traffic = storage.saved[0]["evidence"]["context_json"]["traffic"]
    second_traffic = storage.saved[1]["evidence"]["context_json"]["traffic"]
    assert storage.saved[0]["evidence"]["transfer_bytes"] == 25
    assert first_traffic["http_compressed_response_bytes"] == 50
    assert first_traffic["firefox_main_document_bytes"] is None
    assert second_traffic["http_compressed_response_bytes"] == 100
    final = storage.saved[1]["evidence"]["context_json"]["proxy_session_pool"]
    assert final["circuit_open_reason"] is None
    assert final["unrequested_count"] == 0
    assert len(adapters) == 3
    assert adapters[1].closed is True


def test_two_consecutive_blocked_asins_open_global_circuit_and_leave_remainder_unrequested():
    worker = load_worker()
    pool_module = load_pool()
    pool = pool_module.ProxySessionPool(
        config(proxy_session_retry_per_asin=0, proxy_session_consecutive_block_limit=2),
        lambda slot_config: FakeAdapter(slot_config, [("captcha", 200, 25)]),
        worker.classify_block,
    )
    storage = ProductStorage(count=3)
    worker_config = {
        **worker.DEFAULTS, "max_actions_per_run": 3, "raw_html_dir": None, "context": {},
        "proxy_firefox_verify_on_access_block": False,
    }

    assert worker._run_postgres_actions_impl(
        storage, pool, worker_config, limit=3, run_id="run-two-blocked-asins", worker_id="worker-a"
    ) == -1
    assert len(storage.saved) == 2
    final = storage.saved[-1]["evidence"]["context_json"]["proxy_session_pool"]
    assert final["circuit_open_reason"] == "consecutive_blocked_asins"
    assert final["unrequested_count"] == 1


def test_two_http_captchas_allow_one_stock_firefox_verification_on_last_proxy_session():
    worker = load_worker()
    pool_module = load_pool()
    adapters = []

    class BrowserCapableAdapter(FakeAdapter):
        def fetch_browser(self, _url, **_kwargs):
            self.source_type = "selenium_dom"
            self.last_transfer_bytes = None
            self.last_browser_traffic = {"main_document_bytes": None, "subresource_bytes": None}
            self.browser_attempted = True
            return product_html("B000000001"), 200

        def commit_browser_context(self, *_args, **_kwargs): return 0

    def factory(slot_config):
        adapter = BrowserCapableAdapter(slot_config, [("captcha", 200, 25)])
        adapters.append(adapter)
        return adapter

    pool = pool_module.ProxySessionPool(config(), factory, worker.classify_block)
    storage = ProductStorage()
    worker_config = {
        **worker.DEFAULTS, "max_actions_per_run": 1, "raw_html_dir": None, "context": {},
        "proxy_firefox_verify_on_access_block": True,
    }

    assert worker._run_postgres_actions_impl(
        storage, pool, worker_config, limit=1, run_id="run-firefox-verify", worker_id="worker-a"
    ) == 1

    assert "product" in storage.saved[0]
    pool_context = storage.saved[0]["evidence"]["context_json"]["proxy_session_pool"]
    sessions = pool_context["sessions"]
    assert len(pool_context["attempts"]) == 2
    assert all("body" not in attempt and attempt["content_hash"] for attempt in pool_context["attempts"])
    assert [item["firefox_verification"] for item in sessions] == [None, "succeeded"]
    assert sessions[0]["health"] == "quarantined"
    assert sessions[1]["health"] == "healthy"


def test_firefox_challenge_after_two_http_captchas_keeps_circuit_open_and_stops():
    worker = load_worker()
    pool_module = load_pool()

    class ChallengedAdapter(FakeAdapter):
        def fetch_browser(self, _url, **_kwargs):
            self.source_type = "selenium_dom"
            self.last_transfer_bytes = None
            self.last_browser_traffic = {"main_document_bytes": None, "subresource_bytes": None}
            self.browser_attempted = True
            return "captcha", 200

    pool = pool_module.ProxySessionPool(config(), lambda slot_config: ChallengedAdapter(slot_config, [("captcha", 200, 25)]), worker.classify_block)
    storage = ProductStorage(count=3)
    worker_config = {
        **worker.DEFAULTS, "max_actions_per_run": 3, "raw_html_dir": None, "context": {},
        "proxy_firefox_verify_on_access_block": True,
    }

    assert worker._run_postgres_actions_impl(
        storage, pool, worker_config, limit=3, run_id="run-firefox-blocked", worker_id="worker-a"
    ) == -1
    assert len(storage.saved) == 2
    context = storage.saved[-1]["evidence"]["context_json"]["proxy_session_pool"]
    assert context["sessions"][-1]["firefox_verification"] == "failed"
    assert context["circuit_open_reason"] == "consecutive_blocked_asins"
    assert context["unrequested_count"] == 1


def test_transport_failure_with_unknown_bytes_does_not_become_zero():
    worker = load_worker()
    pool_module = load_pool()

    class UnknownByteAdapter(FakeAdapter):
        def __init__(self, slot_config):
            super().__init__(slot_config, [worker.AdapterFetchError("transport failed")])
            self.last_transfer_bytes = None

    pool = pool_module.ProxySessionPool(config(proxy_session_ports=[10000]), UnknownByteAdapter, worker.classify_block)
    pool.begin_run("run-unknown-bytes", "tenant-a", "worker-a")

    with pytest.raises(worker.AdapterFetchError):
        pool.fetch("https://www.amazon.com/dp/B000000001")

    assert pool.last_transfer_bytes is None


def test_console_projects_latest_sanitized_pool_context_for_run_and_receipt():
    spec = importlib.util.spec_from_file_location("collection_console_pool_test", ROOT / "scripts" / "collection_console.py")
    console = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(console)
    rows = [
        {"context_json": {}},
        {"context_json": {"proxy_session_pool": {"mode": "sticky", "sessions": [{"session_id": "session-01"}], "unrequested_count": 7}}},
    ]

    assert console.latest_proxy_session_pool(rows) == {
        "mode": "sticky",
        "sessions": [{"session_id": "session-01"}],
        "unrequested_count": 7,
    }


def test_simulated_twenty_products_all_record_evidence_and_rotate_bounded_sessions():
    worker = load_worker()
    pool_module = load_pool()

    class DynamicAdapter(FakeAdapter):
        def __init__(self, slot_config):
            super().__init__(slot_config, [])

        def fetch(self, url):
            asin = url.split("/dp/")[1][:10]
            self.last_transfer_bytes = 100
            self.action_http_transfer_bytes += 100
            return product_html(asin), 200

    pool = pool_module.ProxySessionPool(
        config(proxy_session_ports=list(range(10000, 10010)), proxy_session_max_asins=2),
        DynamicAdapter,
        worker.classify_block,
    )
    storage = ProductStorage(count=20)
    worker_config = {**worker.DEFAULTS, "max_actions_per_run": 20, "raw_html_dir": None, "context": {}}

    assert worker._run_postgres_actions_impl(
        storage, pool, worker_config, limit=20, run_id="run-20", worker_id="worker-a"
    ) == 20
    assert len(storage.saved) == 20
    assert all(payload["evidence"]["run_id"] == "run-20" for payload in storage.saved)
    final_pool = storage.saved[-1]["evidence"]["context_json"]["proxy_session_pool"]
    assert len(final_pool["sessions"]) == 10
    assert sum(item["request_count"] for item in final_pool["sessions"]) == 20
    assert sum(item["completed"] for item in final_pool["sessions"]) == 20
    assert final_pool["circuit_open_reason"] is None
    assert final_pool["unrequested_count"] == 0


def test_simulated_hundred_products_fit_in_a_finite_thirty_four_slot_pool():
    worker = load_worker()
    pool_module = load_pool()

    class DynamicAdapter(FakeAdapter):
        def __init__(self, slot_config):
            super().__init__(slot_config, [])

        def fetch(self, url):
            asin = url.split("/dp/")[1][:10]
            self.last_transfer_bytes = 100
            self.action_http_transfer_bytes += 100
            return product_html(asin), 200

    pool = pool_module.ProxySessionPool(
        config(proxy_session_ports=list(range(10000, 10034)), proxy_session_max_asins=3),
        DynamicAdapter,
        worker.classify_block,
    )
    storage = ProductStorage(count=100)
    worker_config = {**worker.DEFAULTS, "max_actions_per_run": 100, "raw_html_dir": None, "context": {}}

    assert worker._run_postgres_actions_impl(
        storage, pool, worker_config, limit=100, run_id="run-100", worker_id="worker-a"
    ) == 100
    assert len(storage.saved) == 100
    final_pool = storage.saved[-1]["evidence"]["context_json"]["proxy_session_pool"]
    assert len(final_pool["sessions"]) == 34
    assert sum(item["completed"] for item in final_pool["sessions"]) == 100
    assert final_pool["circuit_open_reason"] is None
    assert final_pool["unrequested_count"] == 0


def test_worker_builds_pool_only_when_approved_session_ports_are_configured():
    worker = load_worker()
    plain = worker._build_http_adapter({**worker.DEFAULTS, "proxy_url": ""})
    pooled = worker._build_http_adapter({**worker.DEFAULTS, **config()})
    try:
        assert plain.__class__.__name__ == "HttpFirstAdapter"
        assert pooled.__class__.__name__ == "ProxySessionPool"
        assert pooled.evidence_context()["mode"] == "sticky"
    finally:
        plain.close()
        pooled.close()


def test_reserved_slot_ids_select_only_assigned_ports_and_validate_before_slot_creation():
    module = load_pool()
    events = []
    adapters = []

    def factory(slot_config):
        events.append(("factory", slot_config["proxy_url"]))
        adapter = FakeAdapter(slot_config, [("product", 200, 10)])
        adapters.append(adapter)
        return adapter

    pool = module.ProxySessionPool(
        config(proxy_session_ports=[10000, 10001, 10002], proxy_session_max_asins=1),
        factory,
        classifier,
    )
    pool.configure_capacity_reservation(["session-02"], lambda: events.append("validate"))
    pool.begin_run("run-1", "tenant-a", "worker-a")
    pool.fetch("https://www.amazon.com/dp/B000000001")

    assert events[0] == "validate"
    assert events[1][0] == "factory" and events[1][1].endswith(":10001")
    assert pool.evidence_context()["sessions"][0]["session_id"] == "session-02"
