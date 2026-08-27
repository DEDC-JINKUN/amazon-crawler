from __future__ import annotations

import importlib.util
import unittest
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


class HttpAdapterTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
