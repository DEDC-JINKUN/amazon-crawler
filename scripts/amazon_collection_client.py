#!/usr/bin/env python3
"""Small Agent-safe client for the loopback Amazon Collection API.

This client owns only a scoped API credential.  It deliberately has no import
or command path to the crawler, PostgreSQL DSN, browser state, or proxy.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import os
import re
import sys
import time
from typing import Any
import urllib.error
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


MIN_POLL_SECONDS = 1.1


class AgentRateLimitError(OSError):
    """Stable loopback API backpressure carrying only a bounded retry delay."""

    def __init__(self, retry_after_seconds: float):
        self.retry_after_seconds = max(MIN_POLL_SECONDS, min(float(retry_after_seconds), 60.0))
        super().__init__("collection_api_rate_limited")


def _retry_after_seconds(value: Any) -> float:
    try:
        return max(MIN_POLL_SECONDS, min(float(str(value or "").strip()), 60.0))
    except (TypeError, ValueError):
        return MIN_POLL_SECONDS


def write_json(payload: dict[str, Any], *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str), file=stream)


class AmazonCollectionClient:
    def __init__(self, base_url: str, agent_id: str, agent_key: str, timeout_seconds: float = 10.0):
        parsed = urlsplit(base_url)
        if parsed.username or parsed.password:
            raise ValueError("credentials are not allowed in the Collection API URL")
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Collection API URL must use HTTP on a loopback host")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Collection API URL must not include a path, query, or fragment")
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.agent_key = agent_key
        self.timeout_seconds = timeout_seconds
        self._opener = build_opener(_NoRedirectHandler())

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
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if int(exc.code) == 429:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                raise AgentRateLimitError(_retry_after_seconds(retry_after)) from None
            raise

    def get_product(self, asin: str, fields: list[str] | None = None) -> dict[str, Any]:
        suffix = "" if not fields else "?fields=" + ",".join(fields)
        return self._request("GET", f"/v1/asin/US/{asin.upper()}{suffix}")

    def get_products(self, asins: list[str]) -> dict[str, Any]:
        return self._request("POST", "/v1/asin/batch", {"marketplace": "US", "asins": asins})

    def request_refresh(self, asin: str, reason: str = "on_demand") -> dict[str, Any]:
        return self._request("POST", f"/v1/asin/US/{asin.upper()}/refresh", {"reason": reason})

    def request_refreshes(self, asins: list[str], reason: str = "on_demand") -> dict[str, Any]:
        normalized: list[str] = []
        for value in asins:
            asin = str(value).strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{10}", asin):
                raise ValueError(f"invalid ASIN: {asin}")
            if asin not in normalized:
                normalized.append(asin)
        if not 1 <= len(normalized) <= 5:
            raise ValueError("refresh requires 1 to 5 unique ASINs")
        return self._request("POST", "/v1/asin/refresh", {"marketplace": "US", "asins": normalized, "reason": reason})

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/jobs/{job_id}")

    def wait_for_jobs(self, job_ids: list[str], *, timeout_seconds: float = 300.0, poll_seconds: float = 1.0) -> dict[str, Any]:
        if not job_ids:
            raise ValueError("at least one job_id is required")
        deadline = time.monotonic() + max(0.01, float(timeout_seconds))
        pending = deque(dict.fromkeys(job_ids))
        results: dict[str, dict[str, Any]] = {}
        terminal = {"completed", "failed", "cancelled"}
        while pending:
            job_id = pending.popleft()
            try:
                payload = self.get_job(job_id)
            except AgentRateLimitError as exc:
                pending.appendleft(job_id)
                sleep_seconds = exc.retry_after_seconds
            else:
                if payload.get("job", {}).get("status") in terminal:
                    results[job_id] = payload
                else:
                    pending.append(job_id)
                sleep_seconds = max(MIN_POLL_SECONDS, float(poll_seconds))
            if not pending:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"refresh jobs did not finish before timeout: {','.join(pending)}")
            time.sleep(min(sleep_seconds, remaining))
        return {"results": [results[job_id] for job_id in job_ids]}

    def refresh_and_wait(self, asins: list[str], *, reason: str = "on_demand", timeout_seconds: float = 300.0, poll_seconds: float = 1.0) -> dict[str, Any]:
        submitted = self.request_refreshes(asins, reason)
        return self.wait_for_jobs(
            [str(job["job_id"]) for job in submitted["jobs"]],
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    subparsers = parser.add_subparsers(dest="command", required=True)
    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("asin")
    get_parser.add_argument("--fields", nargs="*")
    batch_parser = subparsers.add_parser("batch")
    batch_parser.add_argument("asins", nargs="+")
    refresh_parser = subparsers.add_parser("refresh")
    refresh_parser.add_argument("asins", nargs="+")
    refresh_parser.add_argument("--reason", default="on_demand")
    refresh_parser.add_argument("--wait", action="store_true")
    refresh_parser.add_argument("--timeout-seconds", type=float, default=300.0)
    job_parser = subparsers.add_parser("job")
    job_parser.add_argument("job_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = AmazonCollectionClient.from_environment(args.base_url)
        if args.command == "get":
            payload = client.get_product(args.asin, args.fields)
        elif args.command == "batch":
            payload = client.get_products(args.asins)
        elif args.command == "refresh":
            payload = (
                client.refresh_and_wait(args.asins, reason=args.reason, timeout_seconds=args.timeout_seconds)
                if args.wait else client.request_refreshes(args.asins, args.reason)
            )
        else:
            payload = client.get_job(args.job_id)
        write_json(payload)
        return 0
    except (OSError, ValueError, TimeoutError) as exc:
        write_json({"error": type(exc).__name__, "detail": str(exc)}, error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
