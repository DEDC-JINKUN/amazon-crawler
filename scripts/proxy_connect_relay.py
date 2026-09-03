"""Run-scoped loopback CONNECT relay for stock Firefox proxy authentication.

The relay never terminates TLS. It accepts CONNECT only from loopback, adds the
upstream Basic proxy credential in memory, and forwards opaque bytes until the
browser or upstream closes the tunnel.
"""
from __future__ import annotations

import base64
import re
import select
import socket
import socketserver
import threading
from typing import Iterable
from urllib.parse import urlsplit


MAX_HEADER_BYTES = 16 * 1024


class _RelayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True


class ProxyConnectRelay:
    """Expose an ephemeral loopback proxy that authenticates upstream CONNECT."""

    def __init__(
        self,
        upstream_proxy_url: str,
        username: str,
        password: str,
        *,
        allowed_hosts: Iterable[str] = ("amazon.com", "media-amazon.com", "ssl-images-amazon.com"),
        connect_timeout_seconds: float = 15.0,
    ) -> None:
        upstream = urlsplit(str(upstream_proxy_url or "").strip())
        if (
            upstream.scheme != "http" or not upstream.hostname or upstream.username or upstream.password
            or upstream.path not in {"", "/"} or upstream.query or upstream.fragment
        ):
            raise ValueError("upstream proxy must be a credential-free HTTP endpoint")
        username = str(username or "")
        password = str(password or "")
        if not username or not password or ":" in username or any(value in username + password for value in "\r\n"):
            raise ValueError("proxy relay credentials are invalid")
        normalized_hosts = tuple(sorted({str(value).lower().strip(".") for value in allowed_hosts if value}))
        if not normalized_hosts:
            raise ValueError("at least one CONNECT host suffix is required")
        self._upstream = (upstream.hostname, upstream.port or 80)
        self._allowed_hosts = normalized_hosts
        self._timeout = max(1.0, min(float(connect_timeout_seconds), 120.0))
        encoded = base64.b64encode(f"{username}:{password}".encode("utf-8"))
        self._authorization = bytearray(b"Basic " + encoded)
        self._server: _RelayServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._accepted = 0
        self._rejected = 0
        self._active = 0
        self._status = "created"

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            raise RuntimeError("proxy relay is not started")
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def _allowed(self, host: str) -> bool:
        value = host.lower().strip(".")
        return any(value == suffix or value.endswith("." + suffix) for suffix in self._allowed_hosts)

    @staticmethod
    def _read_headers(sock: socket.socket) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(min(4096, MAX_HEADER_BYTES - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) >= MAX_HEADER_BYTES:
                raise ValueError("CONNECT headers exceed the relay limit")
        return bytes(data)

    @staticmethod
    def _reply(client: socket.socket, status: int, reason: str) -> None:
        client.sendall(f"HTTP/1.1 {status} {reason}\r\nConnection: close\r\n\r\n".encode("ascii"))

    def _parse_target(self, request: bytes) -> tuple[str, int]:
        try:
            line = request.split(b"\r\n", 1)[0].decode("ascii", errors="strict")
            method, target, version = line.split(" ")
            host, port_text = target.rsplit(":", 1)
            port = int(port_text)
        except (UnicodeDecodeError, ValueError):
            raise ValueError("invalid CONNECT request") from None
        if method != "CONNECT" or version not in {"HTTP/1.0", "HTTP/1.1"}:
            raise TypeError("CONNECT is required")
        if port != 443 or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host) or not self._allowed(host):
            raise PermissionError("CONNECT target is not allowed")
        return host.lower(), port

    def _tunnel(self, client: socket.socket, upstream: socket.socket) -> None:
        sockets = (client, upstream)
        while True:
            readable, _, _ = select.select(sockets, (), (), self._timeout)
            if not readable:
                return
            for source in readable:
                payload = source.recv(65536)
                if not payload:
                    return
                destination = upstream if source is client else client
                destination.sendall(payload)

    def _handle(self, client: socket.socket) -> None:
        client.settimeout(self._timeout)
        upstream: socket.socket | None = None
        counted_active = False
        try:
            request = self._read_headers(client)
            try:
                host, port = self._parse_target(request)
            except TypeError:
                with self._lock:
                    self._rejected += 1
                self._reply(client, 405, "Method Not Allowed")
                return
            except (ValueError, PermissionError):
                with self._lock:
                    self._rejected += 1
                self._reply(client, 403, "Forbidden")
                return
            upstream = socket.create_connection(self._upstream, timeout=self._timeout)
            authorization = bytes(self._authorization)
            upstream.sendall(
                f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n".encode("ascii")
                + b"Proxy-Connection: Keep-Alive\r\nProxy-Authorization: " + authorization + b"\r\n\r\n"
            )
            response = self._read_headers(upstream)
            status_line = response.split(b"\r\n", 1)[0]
            if not re.match(br"HTTP/1\.[01] 2\d\d(?: |$)", status_line):
                self._reply(client, 502, "Bad Gateway")
                return
            with self._lock:
                self._accepted += 1
                self._active += 1
                counted_active = True
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._tunnel(client, upstream)
        except (OSError, ValueError):
            try:
                self._reply(client, 502, "Bad Gateway")
            except OSError:
                pass
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass
            with self._lock:
                if counted_active:
                    self._active -= 1

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("proxy relay is already started")
        relay = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                relay._handle(self.request)

        self._server = _RelayServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="proxy-connect-relay", daemon=True)
        self._status = "running"
        self._thread.start()

    def close(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        for index in range(len(self._authorization)):
            self._authorization[index] = 0
        self._status = "closed"

    def audit_summary(self) -> dict[str, int | str]:
        with self._lock:
            return {
                "transport": "loopback_connect_relay",
                "status": self._status,
                "accepted_connections": self._accepted,
                "rejected_connections": self._rejected,
                "active_connections": self._active,
            }

    def __enter__(self) -> "ProxyConnectRelay":
        self.start()
        return self

    def __exit__(self, *_args) -> None:
        self.close()


__all__ = ["ProxyConnectRelay"]
