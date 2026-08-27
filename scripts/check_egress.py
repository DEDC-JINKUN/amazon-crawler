#!/usr/bin/env python3
"""Probe one approved HTTP(S) egress without exposing credentials or body data."""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any


def _validate_proxy_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("proxy_url must be an explicit http(s) URL without embedded credentials")
    return value.strip()


def probe(
    proxy_url: str,
    target_url: str,
    *,
    timeout_seconds: int = 15,
    user_agent: str = "amazon-us-egress-probe/1.0",
    username: str | None = None,
    password: str | None = None,
    opener_factory: Any | None = None,
) -> dict[str, Any]:
    proxy_url = _validate_proxy_url(proxy_url)
    target = urlsplit(target_url)
    if target.scheme not in {"http", "https"} or not target.hostname:
        raise ValueError("target_url must be an explicit http(s) URL")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    handlers: list[Any] = [urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})]
    if (username is None) != (password is None):
        raise ValueError("proxy username and password must be supplied together")
    if username is not None:
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, proxy_url, username, password)
        handlers.append(urllib.request.ProxyBasicAuthHandler(manager))
    opener = (opener_factory or urllib.request.build_opener)(*handlers)
    request = urllib.request.Request(
        target_url,
        headers={"Accept": "text/plain,text/html;q=0.9", "Accept-Encoding": "identity", "User-Agent": user_agent},
        method="GET",
    )
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            body = response.read()
            status = int(response.getcode() or 200)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            body = exc.read()
        except Exception:
            body = b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {
            "schema_version": "amazon-us-egress-probe-v1",
            "ok": False,
            "status": None,
            "block_reason": "network_error",
            "error_type": type(exc).__name__,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "response_bytes": 0,
        }
    block_reason = f"http_{status}" if status in {403, 429} else None
    return {
        "schema_version": "amazon-us-egress-probe-v1",
        "ok": 200 <= status < 300 and not block_reason and bool(body),
        "status": status,
        "block_reason": block_reason,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        "response_bytes": len(body),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-url", required=True)
    parser.add_argument("--target-url", default="https://www.amazon.com/robots.txt")
    parser.add_argument("--timeout-seconds", type=int, default=15)
    parser.add_argument("--proxy-username-env")
    parser.add_argument("--proxy-password-env")
    args = parser.parse_args(argv)
    try:
        if bool(args.proxy_username_env) != bool(args.proxy_password_env):
            raise ValueError("proxy username and password environment names must be supplied together")
        result = probe(
            args.proxy_url,
            args.target_url,
            timeout_seconds=args.timeout_seconds,
            username=os.environ.get(args.proxy_username_env) if args.proxy_username_env else None,
            password=os.environ.get(args.proxy_password_env) if args.proxy_password_env else None,
        )
    except (ValueError, OSError) as exc:
        print(json.dumps({"schema_version": "amazon-us-egress-probe-v1", "ok": False, "block_reason": "configuration_error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
