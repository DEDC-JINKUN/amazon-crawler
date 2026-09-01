"""HTTPS proxy authentication that never adds credentials to origin headers."""
from __future__ import annotations

import base64
import http.client
import urllib.request
from typing import Any


class ProxyTunnelAuthHTTPSHandler(urllib.request.HTTPSHandler):
    """Attach Basic credentials only when urllib opens an HTTPS CONNECT tunnel."""

    def __init__(self, username: str, password: str) -> None:
        super().__init__()
        credential = f"{username}:{password}".encode("utf-8")
        self._proxy_authorization = "Basic " + base64.b64encode(credential).decode("ascii")

    def https_open(self, request: Any) -> Any:
        authorization = self._proxy_authorization

        def connection_factory(host: str, **kwargs: Any) -> http.client.HTTPSConnection:
            connection = http.client.HTTPSConnection(host, **kwargs)
            set_tunnel = connection.set_tunnel

            def authenticated_tunnel(
                tunnel_host: str,
                port: int | None = None,
                headers: dict[str, str] | None = None,
            ) -> None:
                tunnel_headers = dict(headers or {})
                tunnel_headers["Proxy-Authorization"] = authorization
                set_tunnel(tunnel_host, port=port, headers=tunnel_headers)

            connection.set_tunnel = authenticated_tunnel  # type: ignore[method-assign]
            return connection

        return self.do_open(connection_factory, request, context=self._context)
