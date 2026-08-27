from __future__ import annotations

import importlib.util
import http.client
import io
import sys
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
    def get_content_charset(self):
        return "utf-8"


class _Response:
    headers = _Headers()

    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status

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
    def test_firefox_adapter_keeps_config_for_delivery_context(self):
        worker = load_worker()

        class FakeOptions:
            def __init__(self):
                self.profile = None

            def add_argument(self, value):
                pass

            def set_preference(self, name, value):
                pass

        class FakeDriver:
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
        self.assertEqual(adapter.source_type, "http_html")
        adapter.close()

    def test_http_first_can_build_an_explicit_proxy_opener(self):
        worker = load_worker()
        with patch.object(worker.urllib.request, "ProxyHandler") as proxy_handler, patch.object(worker.urllib.request, "build_opener") as build_opener:
            worker.HttpFirstAdapter({**worker.DEFAULTS, "proxy_url": "http://127.0.0.1:8080"})
        proxy_handler.assert_called_once_with({"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"})
        build_opener.assert_called_once_with(proxy_handler.return_value)

    def test_proxy_credentials_are_read_from_named_environment_variables(self):
        worker = load_worker()
        with patch.dict(worker.os.environ, {"PROXY_USER": "alice", "PROXY_PASS": "pw"}, clear=False), \
             patch.object(worker.urllib.request, "ProxyHandler") as proxy_handler, \
             patch.object(worker.urllib.request, "HTTPPasswordMgrWithDefaultRealm") as manager, \
             patch.object(worker.urllib.request, "ProxyBasicAuthHandler") as auth_handler, \
             patch.object(worker.urllib.request, "build_opener"):
            worker.HttpFirstAdapter({**worker.DEFAULTS, "proxy_url": "http://127.0.0.1:8080", "proxy_username_env": "PROXY_USER", "proxy_password_env": "PROXY_PASS"})
        manager.return_value.add_password.assert_called_once_with(None, "http://127.0.0.1:8080", "alice", "pw")
        auth_handler.assert_called_once_with(manager.return_value)

    def test_proxy_credentials_require_both_environment_names(self):
        worker = load_worker()
        with self.assertRaises(ValueError):
            worker.HttpFirstAdapter({**worker.DEFAULTS, "proxy_url": "http://127.0.0.1:8080", "proxy_username_env": "PROXY_USER"})


if __name__ == "__main__":
    unittest.main()
