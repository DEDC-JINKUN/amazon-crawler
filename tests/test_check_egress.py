from __future__ import annotations

import base64
import http.server
import importlib.util
import subprocess
import sys
import threading
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
    result = module.probe("http://127.0.0.1:8080", "https://api.ipify.org?format=json", opener_factory=lambda *handlers: opener)
    assert result["ok"] is True
    assert result["status"] == 200
    assert result["response_bytes"] == 17
    assert "proxy_url" not in result
    assert "body" not in result
    assert opener.request.get_header("Accept-encoding") == "identity"


def test_probe_marks_429_as_blocked():
    module = load_module()
    opener = Opener(Response(status=429, body=b"slow down"))
    result = module.probe("http://127.0.0.1:8080", "https://api.ipify.org?format=json", opener_factory=lambda *handlers: opener)
    assert result["ok"] is False
    assert result["status"] == 429
    assert result["block_reason"] == "http_429"


def test_probe_rejects_200_challenge_and_empty_response():
    module = load_module()
    challenge = Opener(Response(status=200, body=b"<title>Robot Check</title>"))
    result = module.probe("http://127.0.0.1:8080", "https://api.ipify.org?format=json", opener_factory=lambda *handlers: challenge)
    assert result["ok"] is False
    assert result["block_reason"] == "robot"

    captcha = Opener(Response(status=200, body=b"Enter the characters you see below"))
    result = module.probe("http://127.0.0.1:8080", "https://api.ipify.org?format=json", opener_factory=lambda *handlers: captcha)
    assert result["ok"] is False
    assert result["block_reason"] == "captcha"

    empty = Opener(Response(status=200, body=b""))
    result = module.probe("http://127.0.0.1:8080", "https://api.ipify.org?format=json", opener_factory=lambda *handlers: empty)
    assert result["ok"] is False
    assert result["block_reason"] == "empty_response"


def test_body_classifier_rejects_202_aws_waf_challenge_without_an_amazon_probe():
    module = load_module()
    body = b"""
    <script>window.awsWafCookieDomainList = []; AwsWafIntegration.getToken();</script>
    <script src="https://example.token.awswaf.com/challenge.js"></script>
    <div id="challenge-container"></div>
    """
    assert module._classify_body(202, body) == "waf_challenge"


def test_probe_rejects_embedded_credentials_and_partial_auth():
    module = load_module()
    with pytest.raises(ValueError, match="without embedded credentials"):
        embedded = "http://" + "user" + ":" + "pass" + "@127.0.0.1:8080"
        module.probe(embedded, "https://api.ipify.org?format=json")
    with pytest.raises(ValueError, match="supplied together"):
        module.probe("http://127.0.0.1:8080", "https://api.ipify.org?format=json", username="u")
    with pytest.raises(ValueError, match="approved non-Amazon allowlist"):
        module.probe("http://127.0.0.1:8080", "http://api.ipify.org", username="u", password="p")


def test_health_probe_rejects_any_non_allowlisted_target_before_opening():
    module = load_module()
    opener = Opener(Response())

    with pytest.raises(ValueError, match="approved non-Amazon allowlist"):
        module.probe(
            "http://127.0.0.1:8080",
            "https://www.amazon.co.uk/robots.txt",
            opener_factory=lambda *handlers: opener,
        )
    assert opener.request is None


def test_health_probe_cli_rejects_amazon_target_without_network():
    result = subprocess.run(
        [
            sys.executable,
            SCRIPT,
            "--proxy-url",
            "http://127.0.0.1:9",
            "--target-url",
            "https://www.amazon.co.uk/robots.txt",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert "approved non-Amazon allowlist" in result.stdout


def test_probe_sends_basic_proxy_auth_only_to_https_connect_proxy():
    module = load_module()
    captured = {"authorization": None}

    class ProxyHandler(http.server.BaseHTTPRequestHandler):
        def do_CONNECT(self):
            captured["authorization"] = self.headers.get("Proxy-Authorization")
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = module.probe(
            f"http://127.0.0.1:{server.server_port}",
            "https://api.ipify.org?format=json",
            username="alice",
            password="fake-secret",
            timeout_seconds=2,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    expected = "Basic " + base64.b64encode(b"alice:fake-secret").decode("ascii")
    assert captured["authorization"] == expected
    assert result["ok"] is False
    assert result["block_reason"] == "network_error"
