"""Truthful action-scoped proxy traffic measurements."""
from __future__ import annotations

import threading


class ProxyTransportMeter:
    """Count only byte layers observable exactly by this process.

    HTTP response bodies and opaque TLS forwarded by the loopback CONNECT relay
    are different layers and must never be added together as supplier billing.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self.reset()

    @staticmethod
    def _count(value: int) -> int:
        result = int(value)
        if result < 0:
            raise ValueError("byte count must be non-negative")
        return result

    def reset(self) -> None:
        with self._lock:
            self._generation += 1
            self._http_response_body = 0
            self._client_to_upstream = 0
            self._upstream_to_client = 0
            self._tunnel_observed = False

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def _current(self, generation: int | None) -> bool:
        return generation is None or int(generation) == self._generation

    def record_http_response_body(self, count: int, *, generation: int | None = None) -> None:
        with self._lock:
            if self._current(generation):
                self._http_response_body += self._count(count)

    def record_tunnel_observed(self, *, generation: int | None = None) -> None:
        with self._lock:
            if self._current(generation):
                self._tunnel_observed = True

    def record_tunnel_client_to_upstream(self, count: int, *, generation: int | None = None) -> None:
        with self._lock:
            if self._current(generation):
                self._client_to_upstream += self._count(count)
                self._tunnel_observed = True

    def record_tunnel_upstream_to_client(self, count: int, *, generation: int | None = None) -> None:
        with self._lock:
            if self._current(generation):
                self._upstream_to_client += self._count(count)
                self._tunnel_observed = True

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            response = self._http_response_body
            upload = self._client_to_upstream
            download = self._upstream_to_client
            tunnel_observed = self._tunnel_observed
        return {
            "schema_version": "amazon-proxy-transport-v1",
            "http_compressed_response_body": {
                "bytes": response,
                "measurement": "exact_body_bytes",
            },
            "proxy_tunnel_forwarded_tls": {
                "client_to_upstream_bytes": upload,
                "upstream_to_client_bytes": download,
                "total_bytes": upload + download,
                "measurement": "exact_forwarded_payload_bytes",
                "observed": tunnel_observed,
            },
            "direct_http_tunnel_total_bytes": None,
            "supplier_billed_bytes": None,
        }


__all__ = ["ProxyTransportMeter"]
