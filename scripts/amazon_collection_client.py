#!/usr/bin/env python3
"""Small Agent-safe client for the loopback Amazon Collection API.

This client owns only a scoped API credential.  It deliberately has no import
or command path to the crawler, PostgreSQL DSN, browser state, or proxy.
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.request import Request, urlopen


class AmazonCollectionClient:
    def __init__(self, base_url: str, agent_id: str, agent_key: str, timeout_seconds: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.agent_key = agent_key
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls, base_url: str = "http://127.0.0.1:8765") -> "AmazonCollectionClient":
        agent_id = os.environ.get("AMAZON_COLLECTION_AGENT_ID", "").strip()
        agent_key = os.environ.get("AMAZON_COLLECTION_AGENT_KEY", "").strip()
        if not agent_id or not agent_key:
            raise ValueError("scoped collection-agent credentials are required")
        return cls(base_url.rstrip("/"), agent_id, agent_key)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.base_url}{path}", data=data, method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json", "X-Collection-Agent": self.agent_id, "X-Collection-Agent-Key": self.agent_key},
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_product(self, asin: str, fields: list[str] | None = None) -> dict[str, Any]:
        suffix = "" if not fields else "?fields=" + ",".join(fields)
        return self._request("GET", f"/v1/asin/US/{asin.upper()}{suffix}")

    def get_products(self, asins: list[str]) -> dict[str, Any]:
        return self._request("POST", "/v1/asin/batch", {"marketplace": "US", "asins": asins})

    def request_refresh(self, asin: str, reason: str = "on_demand") -> dict[str, Any]:
        return self._request("POST", f"/v1/asin/US/{asin.upper()}/refresh", {"reason": reason})

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/jobs/{job_id}")
