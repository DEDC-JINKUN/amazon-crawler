from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
import socket
import socketserver
import threading
import time


ROOT = Path(__file__).resolve().parents[1]


def load_relay():
    spec = importlib.util.spec_from_file_location(
        "proxy_connect_relay_test", ROOT / "scripts" / "proxy_connect_relay.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def read_headers(sock: socket.socket) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def test_loopback_relay_adds_upstream_connect_auth_then_tunnels_opaque_bytes():
    relay_module = load_relay()
    observed = []

    class Upstream(socketserver.BaseRequestHandler):
        def handle(self):
            observed.append(read_headers(self.request))
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            payload = self.request.recv(4096)
            self.request.sendall(payload)

    upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Upstream)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True); upstream_thread.start()
    relay = relay_module.ProxyConnectRelay(
        f"http://127.0.0.1:{upstream.server_address[1]}", "fixture-user", "fixture-pass",
        allowed_hosts=("amazon.com", "media-amazon.com", "ssl-images-amazon.com"),
    )
    relay.start()
    try:
        with socket.create_connection(relay.address, timeout=2) as client:
            client.sendall(b"CONNECT www.amazon.com:443 HTTP/1.1\r\nHost: www.amazon.com:443\r\n\r\n")
            assert read_headers(client).startswith(b"HTTP/1.1 200")
            opaque = b"\x16\x03\x03opaque-tls-record"
            client.sendall(opaque)
            assert client.recv(len(opaque)) == opaque
    finally:
        relay.close(); upstream.shutdown(); upstream.server_close(); upstream_thread.join(timeout=2)

    expected = base64.b64encode(b"fixture-user:fixture-pass")
    assert b"Proxy-Authorization: Basic " + expected in observed[0]
    summary = relay.audit_summary()
    assert summary == {
        "transport": "loopback_connect_relay", "status": "closed",
        "accepted_connections": 1, "rejected_connections": 0, "active_connections": 0,
    }
    assert "fixture-user" not in repr(summary) and "fixture-pass" not in repr(summary)
    assert all(value == 0 for value in relay._authorization)


def test_relay_rejects_non_connect_and_non_amazon_targets_before_upstream():
    relay_module = load_relay()
    relay = relay_module.ProxyConnectRelay(
        "http://127.0.0.1:9", "fixture-user", "fixture-pass",
        allowed_hosts=("amazon.com", "media-amazon.com", "ssl-images-amazon.com"),
    )
    relay.start()
    try:
        for request in (
            b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n",
            b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n",
            b"CONNECT www.amazon.com:80 HTTP/1.1\r\nHost: www.amazon.com:80\r\n\r\n",
        ):
            with socket.create_connection(relay.address, timeout=2) as client:
                client.sendall(request)
                assert read_headers(client).startswith((b"HTTP/1.1 405", b"HTTP/1.1 403"))
    finally:
        relay.close()

    assert relay.audit_summary()["rejected_connections"] == 3


def test_relay_close_terminates_active_tunnel_waits_handlers_and_zeros_credentials():
    relay_module = load_relay()
    upstream_connected = threading.Event()

    class Upstream(socketserver.BaseRequestHandler):
        def handle(self):
            read_headers(self.request)
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            upstream_connected.set()
            while self.request.recv(4096):
                pass

    upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Upstream)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True); upstream_thread.start()
    relay = relay_module.ProxyConnectRelay(
        f"http://127.0.0.1:{upstream.server_address[1]}", "fixture-user", "fixture-pass",
        max_connections=1,
    )
    relay.start()
    first = socket.create_connection(relay.address, timeout=2)
    try:
        first.sendall(b"CONNECT www.amazon.com:443 HTTP/1.1\r\nHost: www.amazon.com:443\r\n\r\n")
        assert read_headers(first).startswith(b"HTTP/1.1 200")
        assert upstream_connected.wait(2)
        with socket.create_connection(relay.address, timeout=2) as second:
            second.sendall(b"CONNECT www.amazon.com:443 HTTP/1.1\r\nHost: www.amazon.com:443\r\n\r\n")
            assert read_headers(second).startswith(b"HTTP/1.1 503")

        started = time.monotonic()
        relay.close()
        assert time.monotonic() - started < 3
        try:
            assert first.recv(1) == b""
        except (ConnectionResetError, OSError):
            pass
    finally:
        first.close(); relay.close(); upstream.shutdown(); upstream.server_close(); upstream_thread.join(timeout=2)

    summary = relay.audit_summary()
    assert summary["active_connections"] == 0
    assert summary["rejected_connections"] == 1
    assert all(value == 0 for value in relay._authorization)


def test_relay_accepts_only_standard_200_connect_response():
    relay_module = load_relay()

    class Upstream(socketserver.BaseRequestHandler):
        def handle(self):
            read_headers(self.request)
            self.request.sendall(b"HTTP/1.1 204 No Content\r\n\r\n")

    upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Upstream)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True); upstream_thread.start()
    relay = relay_module.ProxyConnectRelay(
        f"http://127.0.0.1:{upstream.server_address[1]}", "fixture-user", "fixture-pass",
    )
    relay.start()
    try:
        with socket.create_connection(relay.address, timeout=2) as client:
            client.sendall(b"CONNECT www.amazon.com:443 HTTP/1.1\r\nHost: www.amazon.com:443\r\n\r\n")
            assert read_headers(client).startswith(b"HTTP/1.1 502")
    finally:
        relay.close(); upstream.shutdown(); upstream.server_close(); upstream_thread.join(timeout=2)
