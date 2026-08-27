from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_egress.py"


def load_module():
    spec = importlib.util.spec_from_file_location("check_egress_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, status=200, body=b"ok"):
        self.status = status
        self.body = body

    def read(self):
        return self.body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class Opener:
    def __init__(self, response):
        self.response = response
        self.request = None

    def open(self, request, timeout):
        self.request = request
        return self.response


def test_probe_reports_safe_success_without_proxy_url_or_body():
    module = load_module()
    opener = Opener(Response(body=b"public probe body"))
    result = module.probe("http://127.0.0.1:8080", "https://example.test/robots.txt", opener_factory=lambda *handlers: opener)
    assert result["ok"] is True
    assert result["status"] == 200
    assert result["response_bytes"] == 17
    assert "proxy_url" not in result
    assert "body" not in result
    assert opener.request.get_header("Accept-encoding") == "identity"


def test_probe_marks_429_as_blocked():
    module = load_module()
    opener = Opener(Response(status=429, body=b"slow down"))
    result = module.probe("http://127.0.0.1:8080", "https://example.test", opener_factory=lambda *handlers: opener)
    assert result["ok"] is False
    assert result["status"] == 429
    assert result["block_reason"] == "http_429"


def test_probe_rejects_200_challenge_and_empty_response():
    module = load_module()
    challenge = Opener(Response(status=200, body=b"<title>Robot Check</title>"))
    result = module.probe("http://127.0.0.1:8080", "https://example.test", opener_factory=lambda *handlers: challenge)
    assert result["ok"] is False
    assert result["block_reason"] == "robot"

    empty = Opener(Response(status=200, body=b""))
    result = module.probe("http://127.0.0.1:8080", "https://example.test", opener_factory=lambda *handlers: empty)
    assert result["ok"] is False
    assert result["block_reason"] == "empty_response"


def test_probe_rejects_embedded_credentials_and_partial_auth():
    module = load_module()
    with pytest.raises(ValueError, match="without embedded credentials"):
        module.probe("http://user:pass@127.0.0.1:8080", "https://example.test")
    with pytest.raises(ValueError, match="supplied together"):
        module.probe("http://127.0.0.1:8080", "https://example.test", username="u")
