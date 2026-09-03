from __future__ import annotations

import importlib.util
import gzip
import http.client
import io
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "amazon_us_worker.py"


def load_worker():
    spec = importlib.util.spec_from_file_location("amazon_us_worker_http_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Headers:
    def __init__(self, values=None):
        self.values = values or {}

    def get_content_charset(self):
        return "utf-8"

    def get(self, name, default=None):
        return self.values.get(name, default)


class _Response:
    headers = _Headers()

    def __init__(self, body: bytes, status: int = 200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or _Headers()

    def read(self):
        return self.body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _Opener:
    def __init__(self, response):
        self.response = response
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return self.response


class _SequenceOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def open(self, request, timeout):
        response = self.responses[self.calls]
        self.calls += 1
        return response


class HttpAdapterTests(unittest.TestCase):
    def test_http_gzip_response_is_decoded_and_transfer_bytes_are_compressed(self):
        worker = load_worker()
        plain = b"<html><title>ok</title>" + (b"product text " * 100)
        compressed = gzip.compress(plain)
        opener = _Opener(_Response(compressed, headers=_Headers({"Content-Encoding": "gzip"})))
        config = {**worker.DEFAULTS, "user_agent": "Agent/test-agent", "http_accept_encoding": "gzip"}
        with patch.object(worker.urllib.request, "build_opener", return_value=opener):
            adapter = worker.HttpFirstAdapter(config)
            body, status = adapter.fetch("https://example.test")
        self.assertEqual((body.encode(), status), (plain, 200))
        self.assertEqual(opener.request.get_header("Accept-encoding"), "gzip")
        self.assertEqual(adapter.last_transfer_bytes, len(compressed))
        self.assertLess(adapter.last_transfer_bytes, len(plain))
        adapter.close()

    def test_config_rejects_unsupported_content_encoding(self):
        worker = load_worker()
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text('[worker]\nagent_name="test-agent"\nuser_agent="Agent/test-agent"\nhttp_accept_encoding="br"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "http_accept_encoding"):
                worker.load_config(config_path)

    def test_firefox_adapter_keeps_config_for_delivery_context(self):
        worker = load_worker()

        class FakeOptions:
            def __init__(self):
                self.profile = None

            def add_argument(self, value):
                pass

            def set_preference(self, name, value):
                pass

        class FakeNetwork:
            def add_request_handler(self, *args):
                return "request"

            def add_event_handler(self, event, callback):
                return 1 if event == "response_completed" else 2

            def remove_request_handler(self, *args):
                pass

            def remove_event_handler(self, event, handler_id):
                pass

        class FakeDriver:
            network = FakeNetwork()

            def set_page_load_timeout(self, value):
                pass

            def quit(self):
                pass

        webdriver = types.ModuleType("selenium.webdriver")
        webdriver.Firefox = lambda **kwargs: FakeDriver()
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
        config = {**worker.DEFAULTS, "context": {"postal_code": "90001"}}
        with patch.dict(sys.modules, fake_modules):
            adapter = worker.SeleniumFirefoxAdapter(config)
        self.assertIs(adapter.config, config)
        adapter.close()

    def test_firefox_fetch_clicks_visible_zip_done_button_and_confirms_header(self):
        worker = load_worker()

        class Element:
            def __init__(self, driver, text="", displayed=True, on_click=None):
                self.driver, self.text, self.displayed, self.on_click = driver, text, displayed, on_click

            def click(self):
                if self.on_click:
                    self.on_click()

            def clear(self):
                pass

            def send_keys(self, value):
                self.driver.input_value = value

            def is_displayed(self):
                return self.displayed

            def get_attribute(self, name):
                return "USD" if name == "value" and self.text == "currency" else None

        class Driver:
            page_source = "<html>ok</html>"
            current_window_handle = "top-context"
            input_value = ""
            committed = False
            js_clicked = False
            refreshed = False

            def get(self, url):
                pass

            def refresh(self):
                self.refreshed = True

            def find_element(self, by, value):
                if value == "glow-ingress-line1":
                    return Element(self, "Delivering to Los Angeles 90001" if self.committed else "Delivering to Portland 97230")
                if value == "glow-ingress-line2":
                    return Element(self, "Update location")
                if value == "nav-global-location-popover-link":
                    return Element(self)
                if value == "GLUXZipUpdateInput":
                    return Element(self)
                if value == "#GLUXZipUpdate input[type='submit']":
                    return Element(self)
                if value == "currencyOfPreference":
                    return Element(self, "currency")
                if value == "//button[normalize-space()='Done' or normalize-space()='完成']":
                    return Element(self, "Done", displayed=False)
                raise LookupError(value)

            def find_elements(self, by, value):
                if value == "button[name='glowDoneButton']":
                    return [
                        Element(self, "Done", displayed=False),
                        Element(self, "Done", displayed=True, on_click=lambda: setattr(self, "committed", True)),
                    ]
                return []

            def execute_script(self, script, element):
                self.js_clicked = True
                element.click()

        class Wait:
            def __init__(self, driver, timeout):
                self.driver = driver

            def until(self, condition):
                value = condition(self.driver)
                if not value:
                    raise TimeoutError("condition not met")
                return value

        driver = Driver()
        adapter = object.__new__(worker.SeleniumFirefoxAdapter)
        adapter.config = {**worker.DEFAULTS, "context": {"postal_code": "90001"}}
        adapter.driver = driver
        adapter._context_initialized = False

        import selenium.webdriver.support.expected_conditions as expected_conditions
        import selenium.webdriver.support.ui as support_ui

        with patch.object(support_ui, "WebDriverWait", Wait), patch.object(
            expected_conditions, "presence_of_element_located", lambda locator: lambda current: current.find_element(*locator)
        ), patch.object(
            expected_conditions, "element_to_be_clickable", lambda locator: lambda current: current.find_element(*locator)
        ), patch.object(worker.time, "sleep", lambda seconds: None):
            body, _ = adapter.fetch("https://www.amazon.com/dp/B00RCPDCQU")

        self.assertEqual(body, "<html>ok</html>")
        self.assertTrue(driver.committed)
        self.assertTrue(driver.js_clicked)
        self.assertTrue(driver.refreshed)

    def test_firefox_zip_modal_uses_dom_click_and_text_done_variant(self):
        worker = load_worker()

        class Element:
            def __init__(self, driver, kind, text="", displayed=True):
                self.driver, self.kind, self.text, self.displayed = driver, kind, text, displayed

            def click(self):
                self.driver.native_clicks.append(self.kind)

            def clear(self):
                pass

            def send_keys(self, value):
                self.driver.input_value = value

            def is_displayed(self):
                return self.displayed

            def get_attribute(self, name):
                return "USD" if self.kind == "currency" and name == "value" else None

        class Driver:
            page_source = "<html>ok</html>"
            current_window_handle = "top-context"

            def __init__(self):
                self.input_value = ""
                self.modal_open = False
                self.committed = False
                self.native_clicks = []
                self.dom_clicks = []
                self.refreshed = False

            def get(self, url):
                pass

            def refresh(self):
                self.refreshed = True

            def quit(self):
                pass

            def find_element(self, by, value):
                if value == "glow-ingress-line1":
                    return Element(self, "header", "Delivering to Los Angeles 90001" if self.committed else "Delivering to Portland 97230")
                if value == "glow-ingress-line2":
                    return Element(self, "header", "Update location")
                if value == "nav-global-location-popover-link":
                    return Element(self, "location")
                if value == "GLUXZipUpdateInput":
                    return Element(self, "zip")
                if value == "#GLUXZipUpdate input[type='submit']":
                    return Element(self, "apply")
                if value == "currencyOfPreference":
                    return Element(self, "currency")
                if value == "//button[normalize-space()='Done' or normalize-space()='完成']" and self.modal_open:
                    return Element(self, "done", "Done")
                raise LookupError(value)

            def find_elements(self, by, value):
                return []

            def execute_script(self, script, element=None):
                if element is None:
                    return []
                self.dom_clicks.append(element.kind)
                if element.kind == "apply":
                    self.modal_open = True
                elif element.kind == "done":
                    self.committed = True

        class Wait:
            def __init__(self, driver, timeout):
                self.driver = driver

            def until(self, condition):
                value = condition(self.driver)
                if not value:
                    raise TimeoutError("condition not met")
                return value

        driver = Driver()
        adapter = object.__new__(worker.SeleniumFirefoxAdapter)
        adapter.config = {**worker.DEFAULTS, "context": {"postal_code": "90001"}}
        adapter.driver = driver
        adapter._context_initialized = False

        import selenium.webdriver.support.expected_conditions as expected_conditions
        import selenium.webdriver.support.ui as support_ui

        with patch.object(support_ui, "WebDriverWait", Wait), patch.object(
            expected_conditions, "presence_of_element_located", lambda locator: lambda current: current.find_element(*locator)
        ), patch.object(worker.time, "sleep", lambda seconds: None):
            body, _ = adapter.fetch("https://www.amazon.com/dp/B0BLVBQVKS")

        self.assertEqual(body, "<html>ok</html>")
        self.assertEqual(driver.input_value, "90001")
        self.assertEqual(driver.dom_clicks, ["apply", "done"])
        self.assertTrue(driver.committed)
        self.assertTrue(driver.refreshed)
        self.assertTrue(adapter._context_initialized)

    def test_failed_browser_fallback_does_not_relabel_http_body_as_selenium(self):
        worker = load_worker()

        class Browser:
            def fetch(self, url):
                raise worker.AdapterFetchError("zip context failed")

        adapter = object.__new__(worker.HttpFirstAdapter)
        adapter.browser = Browser()
        adapter.source_type = "http_html"
        adapter.last_transfer_bytes = 123

        with self.assertRaises(worker.AdapterFetchError):
            adapter.fetch_browser(
                "https://www.amazon.com/dp/B00RCPDCQU",
                fallback_reason=worker.FallbackReason.CONTEXT_MISMATCH,
                run_id="run-1",
                asin="B00RCPDCQU",
            )

        self.assertEqual(adapter.source_type, "http_html")
        self.assertEqual(adapter.last_transfer_bytes, 123)

    def test_retry_after_parses_bounded_delta_seconds(self):
        worker = load_worker()
        self.assertEqual(worker._retry_after_seconds("120"), 120)
        self.assertEqual(worker._retry_after_seconds("999999"), 86400)
        self.assertIsNone(worker._retry_after_seconds("0"))
        self.assertIsNone(worker._retry_after_seconds("tomorrow"))
        now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(worker._retry_after_seconds("Thu, 27 Aug 2026 14:00:00 GMT", now), 7200)

    def test_http_429_captures_retry_after_for_task_cooldown(self):
        worker = load_worker()
        error = worker.urllib.error.HTTPError(
            "https://example.test",
            429,
            "too many requests",
            {"Retry-After": "120"},
            io.BytesIO(b"Too many requests"),
        )

        class ErrorOpener:
            def open(self, request, timeout):
                raise error

        with patch.object(worker.urllib.request, "build_opener", return_value=ErrorOpener()):
            adapter = worker.HttpFirstAdapter({**worker.DEFAULTS, "http_max_attempts": 1})
            body, status = adapter.fetch("https://example.test")
        self.assertEqual((body, status), ("Too many requests", 429))
        self.assertEqual(adapter.last_retry_after_seconds, 120)
        adapter.close()

    def test_incomplete_chunked_response_becomes_retryable_adapter_error(self):
        worker = load_worker()

        class BrokenResponse(_Response):
            def read(self):
                raise http.client.IncompleteRead(b"partial")

        opener = _Opener(BrokenResponse(b""))
        config = dict(worker.DEFAULTS)
        config.update({"user_agent": "Agent/test-agent", "http_max_attempts": 1})
        with patch.object(worker.urllib.request, "build_opener", return_value=opener):
            adapter = worker.HttpFirstAdapter(config)
            with self.assertRaises(worker.AdapterFetchError):
                adapter.fetch("https://www.amazon.com/dp/B00RCPDCQU")
            adapter.close()

    def test_incomplete_response_retries_once_then_succeeds(self):
        worker = load_worker()

        class BrokenResponse(_Response):
            def read(self):
                raise http.client.IncompleteRead(b"partial")

        opener = _SequenceOpener([BrokenResponse(b""), _Response(b"<html>complete</html>")])
        config = {**worker.DEFAULTS, "user_agent": "Agent/test-agent", "http_max_attempts": 2, "http_retry_backoff_seconds": 0}
        with patch.object(worker.urllib.request, "build_opener", return_value=opener):
            adapter = worker.HttpFirstAdapter(config)
            body, status = adapter.fetch("https://www.amazon.com/dp/B00RCPDCQU")
        self.assertEqual((body, status), ("<html>complete</html>", 200))
        self.assertEqual(opener.calls, 2)
        self.assertEqual(adapter.last_transfer_bytes, len(b"partial") + len(b"<html>complete</html>"))
        adapter.close()

    def test_http_error_with_truncated_body_returns_status_without_crashing(self):
        worker = load_worker()

        class BrokenError(worker.urllib.error.HTTPError):
            def read(self):
                raise http.client.IncompleteRead(b"partial error")

        error = BrokenError("https://example.test", 404, "not found", {}, None)

        class ErrorOpener:
            def open(self, request, timeout):
                raise error

        opener = ErrorOpener()
        config = {**worker.DEFAULTS, "user_agent": "Agent/test-agent", "http_max_attempts": 1}
        with patch.object(worker.urllib.request, "build_opener", return_value=opener):
            adapter = worker.HttpFirstAdapter(config)
            body, status = adapter.fetch("https://example.test")
        self.assertEqual((body, status), ("partial error", 404))
        adapter.close()

    def test_firefox_proxy_settings_use_same_explicit_endpoint(self):
        worker = load_worker()
        self.assertEqual(worker._firefox_proxy_settings("http://127.0.0.1:8080"), {"proxyType": "manual", "httpProxy": "127.0.0.1:8080", "sslProxy": "127.0.0.1:8080"})
        self.assertIsNone(worker._firefox_proxy_settings(""))

    def test_firefox_proxy_settings_reject_embedded_credentials(self):
        worker = load_worker()
        with self.assertRaises(ValueError):
            worker._firefox_proxy_settings("http://user:pass@127.0.0.1:8080")

    def test_http_first_fetch_uses_transparent_headers_and_returns_status(self):
        worker = load_worker()
        opener = _Opener(_Response(b"<html><title>ok</title></html>"))
        config = dict(worker.DEFAULTS)
        config.update({"request_timeout_seconds": 7, "user_agent": "Agent/test-agent"})
        with patch.object(worker.urllib.request, "build_opener", return_value=opener):
            adapter = worker.HttpFirstAdapter(config)
            body, status = adapter.fetch("https://www.amazon.com/dp/B00RCPDCQU")
        self.assertEqual(status, 200)
        self.assertIn("<title>ok</title>", body)
        self.assertEqual(opener.timeout, 7)
        self.assertEqual(opener.request.get_header("User-agent"), "Agent/test-agent")
        self.assertEqual(opener.request.get_header("Accept"), "text/html,application/xhtml+xml")
        self.assertEqual(opener.request.get_header("Accept-encoding"), "gzip")
        self.assertEqual(opener.request.get_header("Connection"), "close")
        self.assertEqual(adapter.source_type, "http_html")
        adapter.close()

    def test_loaded_paid_proxy_config_defaults_to_per_asin_relay_and_bounded_jitter(self):
        worker = load_worker()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.toml"
            path.write_text(
                "[worker]\n"
                "agent_name='amazon-us-worker'\n"
                "user_agent='Mozilla/5.0 Agent/amazon-us-worker'\n"
                "proxy_url='http://proxy.example:10000'\n"
                "proxy_username_env='PROXY_USER'\n"
                "proxy_password_env='PROXY_PASS'\n"
                "proxy_session_ports=[10000,10001,10002]\n"
                "egress_requests_per_second=0.2\n"
                "rate_burst=1\n",
                encoding="utf-8",
            )

            config = worker.load_config(path)

        assert config["egress_profile"] == "proxy_sessions"
        assert config["proxy_product_session_scope"] == "per_asin"
        assert config["firefox_proxy_auth_mode"] == "loopback_connect_relay"
        assert config["proxy_request_jitter_seconds"] == 1.0
        assert config["global_requests_per_second"] == 0.2
        assert config["egress_requests_per_second"] == 0.2
        assert config["rate_burst"] == 1

    def test_loaded_paid_proxy_config_rejects_rates_above_serial_safety_limit(self):
        worker = load_worker()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.toml"
            path.write_text(
                "[worker]\n"
                "agent_name='amazon-us-worker'\n"
                "user_agent='Mozilla/5.0 Agent/amazon-us-worker'\n"
                "proxy_url='http://proxy.example:10000'\n"
                "proxy_username_env='PROXY_USER'\n"
                "proxy_password_env='PROXY_PASS'\n"
                "proxy_session_ports=[10000]\n"
                "global_requests_per_second=0.21\n"
                "egress_requests_per_second=0.2\n"
                "rate_burst=1\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "must be between 0 and 0.2"):
                worker.load_config(path)

    def test_http_first_can_build_an_explicit_proxy_opener(self):
        worker = load_worker()
        with patch.object(worker.urllib.request, "ProxyHandler") as proxy_handler, patch.object(worker.urllib.request, "build_opener") as build_opener:
            worker.HttpFirstAdapter({**worker.DEFAULTS, "proxy_url": "http://127.0.0.1:8080"})
        proxy_handler.assert_called_once_with({"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"})
        handlers = build_opener.call_args.args
        self.assertIs(handlers[0], proxy_handler.return_value)
        self.assertIsInstance(handlers[1], worker.urllib.request.HTTPCookieProcessor)

    def test_proxy_credentials_are_read_from_named_environment_variables(self):
        worker = load_worker()
        opener = _Opener(_Response(b"<html><title>ok</title></html>"))
        with patch.dict(worker.os.environ, {"PROXY_USER": "alice", "PROXY_PASS": "pw"}, clear=False), \
             patch.object(worker.urllib.request, "ProxyHandler") as proxy_handler, \
             patch.object(worker.urllib.request, "build_opener", return_value=opener) as build_opener:
            adapter = worker.HttpFirstAdapter({**worker.DEFAULTS, "proxy_url": "http://127.0.0.1:8080", "proxy_username_env": "PROXY_USER", "proxy_password_env": "PROXY_PASS"})
            adapter.fetch("https://www.amazon.com/dp/B00RCPDCQU")
        proxy_handler.assert_called_once_with({"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"})
        self.assertTrue(any(isinstance(handler, worker.ProxyTunnelAuthHTTPSHandler) for handler in build_opener.call_args.args))
        self.assertIsNone(opener.request.get_header("Proxy-authorization"))
        adapter.close()

    def test_proxy_credentials_require_both_environment_names(self):
        worker = load_worker()
        with self.assertRaises(ValueError):
            worker.HttpFirstAdapter({**worker.DEFAULTS, "proxy_url": "http://127.0.0.1:8080", "proxy_username_env": "PROXY_USER"})

    def test_http_first_rejects_proxy_url_with_embedded_credentials(self):
        worker = load_worker()
        with self.assertRaisesRegex(ValueError, "credentials must not be embedded"):
            worker.HttpFirstAdapter({**worker.DEFAULTS, "proxy_url": "http://user:secret@127.0.0.1:8080"})

    def test_authenticated_proxy_browser_fallback_fails_before_firefox_starts(self):
        worker = load_worker()
        with patch.dict(worker.os.environ, {"PROXY_USER": "alice", "PROXY_PASS": "pw"}, clear=False), \
             patch.object(worker, "SeleniumFirefoxAdapter") as firefox:
            adapter = worker.HttpFirstAdapter({
                **worker.DEFAULTS,
                "proxy_url": "http://127.0.0.1:8080",
                "proxy_username_env": "PROXY_USER",
                "proxy_password_env": "PROXY_PASS",
            })
            with self.assertRaisesRegex(worker.AdapterFetchError, "Firefox proxy authentication is unavailable"):
                adapter.fetch_browser(
                    "https://www.amazon.com/dp/B00RCPDCQU",
                    fallback_reason=worker.FallbackReason.CONTEXT_MISMATCH,
                    run_id="run-a",
                    asin="B00RCPDCQU",
                )
        firefox.assert_not_called()
        adapter.close()

    def test_proxy_request_jitter_is_bounded_and_applied_before_each_http_request(self):
        worker = load_worker()
        opener = _Opener(_Response(b"<html><title>ok</title></html>"))
        sleeps = []
        config = {
            **worker.DEFAULTS, "proxy_url": "http://127.0.0.1:8080",
            "user_agent": "Agent/test-agent", "proxy_request_jitter_seconds": 2.0,
        }
        with patch.object(worker.urllib.request, "build_opener", return_value=opener), \
             patch.object(worker.random, "uniform", return_value=1.25), \
             patch.object(worker.time, "sleep", side_effect=sleeps.append):
            adapter = worker.HttpFirstAdapter(config)
            adapter.fetch("https://www.amazon.com/dp/B00RCPDCQU")
            adapter.close()

        assert sleeps == [1.25]

    def test_authenticated_proxy_browser_uses_ephemeral_loopback_connect_relay_without_persisting_credentials(self):
        worker = load_worker()
        created = {}

        class Relay:
            address = ("127.0.0.1", 43123)

            def __init__(self, upstream, username, password, **_kwargs):
                created["upstream"] = upstream
                created["credential_pair"] = (username, password)
                created["relay"] = self
                self.started = False
                self.closed = False

            def start(self): self.started = True
            def close(self): self.closed = True
            def audit_summary(self):
                return {"transport": "loopback_connect_relay", "status": "running", "accepted_connections": 1,
                        "rejected_connections": 0, "active_connections": 0}

        class Browser:
            def __init__(self, config):
                created["browser_config"] = config
                self.last_traffic = {}
                self._context_initialized = False

            def fetch(self, url): return "<html>verified</html>", 200
            def close(self): return None

        with patch.dict(worker.os.environ, {"PROXY_USER": "fixture-user", "PROXY_PASS": "fixture-pass"}, clear=False), \
             patch.object(worker, "ProxyConnectRelay", Relay, create=True), \
             patch.object(worker, "SeleniumFirefoxAdapter", Browser):
            adapter = worker.HttpFirstAdapter({
                **worker.DEFAULTS,
                "proxy_url": "http://127.0.0.1:8080",
                "proxy_username_env": "PROXY_USER",
                "proxy_password_env": "PROXY_PASS",
                "firefox_proxy_auth_mode": "loopback_connect_relay",
            })
            body, status = adapter.fetch_browser(
                "https://www.amazon.com/dp/B00RCPDCQU",
                fallback_reason=worker.FallbackReason.CONTEXT_MISMATCH,
                run_id="run-a", asin="B00RCPDCQU",
            )
            summary = adapter.proxy_relay_summary()
            adapter.close()

        assert (body, status) == ("<html>verified</html>", 200)
        assert created["relay"].started is True and created["relay"].closed is True
        assert created["browser_config"]["proxy_url"] == "http://127.0.0.1:43123"
        assert created["browser_config"]["proxy_username_env"] == ""
        assert created["browser_config"]["proxy_password_env"] == ""
        assert "fixture-user" not in repr(summary) and "fixture-pass" not in repr(summary)


if __name__ == "__main__":
    unittest.main()
