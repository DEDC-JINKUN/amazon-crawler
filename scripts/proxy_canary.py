#!/usr/bin/env python3
"""Run a bounded, non-Amazon proxy-session canary without disclosing egress identities."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
import socket
import ssl
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
import sys
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

try:
    from proxy_tunnel_auth import ProxyTunnelAuthHTTPSHandler
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from proxy_tunnel_auth import ProxyTunnelAuthHTTPSHandler


SCHEMA_VERSION = "amazon-us-proxy-canary-v1"
DEFAULT_CANARY_URL = "https://api.ipify.org?format=json"
MAX_PROXY_SESSION_PORTS = 40


def _validated_shape(config: dict[str, Any]) -> dict[str, Any]:
    base = urlsplit(str(config.get("proxy_url") or "").strip())
    if base.scheme not in {"http", "https"} or not base.hostname or base.username or base.password:
        raise ValueError("proxy_url must be an approved credential-free HTTP(S) endpoint")
    ports = [int(value) for value in list(config.get("proxy_session_ports") or [])]
    if not ports or len(ports) > MAX_PROXY_SESSION_PORTS:
        raise ValueError(f"proxy_session_ports must contain 1 to {MAX_PROXY_SESSION_PORTS} approved ports")
    if len(set(ports)) != len(ports) or any(value < 1 or value > 65535 for value in ports):
        raise ValueError("proxy_session_ports must be unique valid ports")
    max_asins = int(config.get("proxy_session_max_asins") or 3)
    if max_asins < 1 or max_asins > 5:
        raise ValueError("proxy_session_max_asins must be between 1 and 5")
    target_url = str(config.get("proxy_canary_url") or DEFAULT_CANARY_URL).strip()
    target = urlsplit(target_url)
    target_host = (target.hostname or "").lower().rstrip(".")
    if target.scheme != "https" or not target_host or target.username or target.password:
        raise ValueError("proxy_canary_url must be an explicit HTTPS URL")
    if target_host == "amazon.com" or target_host.endswith(".amazon.com"):
        raise ValueError("proxy_canary_url must not target Amazon")
    timeout_seconds = int(config.get("proxy_canary_timeout_seconds") or 15)
    if timeout_seconds < 1 or timeout_seconds > 120:
        raise ValueError("proxy_canary_timeout_seconds must be between 1 and 120")
    return {
        "base": base,
        "ports": ports,
        "max_asins": max_asins,
        "target_url": target_url,
        "timeout_seconds": timeout_seconds,
    }


def capacity_config_hash(config: dict[str, Any]) -> str:
    shape = _validated_shape(config)
    payload = {
        "proxy_scheme": shape["base"].scheme,
        "proxy_host": shape["base"].hostname,
        "ports": shape["ports"],
        "max_asins": shape["max_asins"],
        "target_url": shape["target_url"],
        "timeout_seconds": shape["timeout_seconds"],
        "username_env": str(config.get("proxy_username_env") or ""),
        "password_env": str(config.get("proxy_password_env") or ""),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _proxy_url(base: Any, port: int) -> str:
    host = base.hostname or ""
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return urlunsplit((base.scheme, netloc, base.path, "", ""))


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    return round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 1)


def probe_proxy_slot(
    *,
    proxy_url: str,
    target_url: str,
    timeout_seconds: int,
    username: str,
    password: str,
    opener_factory: Callable[..., Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Return an internal probe result; callers must remove ``egress_ip`` before serialization."""
    handlers = [
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}),
        ProxyTunnelAuthHTTPSHandler(username, password),
    ]
    opener = (opener_factory or urllib.request.build_opener)(*handlers)
    request = urllib.request.Request(
        target_url,
        headers={"Accept": "application/json,text/plain;q=0.9", "Accept-Encoding": "identity", "User-Agent": "amazon-us-proxy-canary/1.0"},
        method="GET",
    )
    started = clock()
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            body = response.read(4096)
            status = int(response.getcode() or 200)
    except Exception as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 407:
            error_class, status, auth_status, tunnel_status = "proxy_auth_failed", 407, "failed", "unknown"
        elif isinstance(reason, (TimeoutError, socket.timeout)):
            error_class, status, auth_status, tunnel_status = "timeout", None, "unknown", "failed"
        elif isinstance(reason, ssl.SSLError):
            error_class, status, auth_status, tunnel_status = "tls_failed", None, "unknown", "failed"
        elif isinstance(reason, socket.gaierror):
            error_class, status, auth_status, tunnel_status = "dns_failed", None, "unknown", "failed"
        elif isinstance(reason, (ConnectionError, ConnectionRefusedError)):
            error_class, status, auth_status, tunnel_status = "connect_failed", None, "unknown", "failed"
        elif isinstance(exc, urllib.error.HTTPError):
            error_class, status, auth_status, tunnel_status = f"http_{exc.code}", int(exc.code), "succeeded", "succeeded"
        else:
            error_class, status, auth_status, tunnel_status = "network_error", None, "unknown", "failed"
        return {
            "ok": False,
            "http_status": status,
            "latency_ms": round((clock() - started) * 1000, 1),
            "auth_status": auth_status,
            "connect_tls_status": tunnel_status,
            "error_class": error_class,
        }
    decoded = body.decode("utf-8", errors="strict").strip()
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError:
        parsed = decoded
    candidate = parsed.get("ip") if isinstance(parsed, dict) else parsed
    try:
        identity = str(ipaddress.ip_address(str(candidate or "").strip()))
    except ValueError:
        return {
            "ok": False,
            "http_status": status,
            "latency_ms": round((clock() - started) * 1000, 1),
            "auth_status": "succeeded",
            "connect_tls_status": "succeeded",
            "error_class": "invalid_canary_response",
        }
    return {
        "ok": 200 <= status < 300,
        "egress_ip": identity,
        "http_status": status,
        "latency_ms": round((clock() - started) * 1000, 1),
        "auth_status": "succeeded",
        "connect_tls_status": "succeeded",
        "error_class": None,
    }


def run_proxy_canary(
    config: dict[str, Any],
    *,
    requested_actions: int,
    probe_slot: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    shape = _validated_shape(config)
    if requested_actions < 1:
        raise ValueError("requested_actions must be positive")
    username_env = str(config.get("proxy_username_env") or "").strip()
    password_env = str(config.get("proxy_password_env") or "").strip()
    username = os.environ.get(username_env) if username_env else None
    password = os.environ.get(password_env) if password_env else None
    if not username or not password:
        required_slots = math.ceil(requested_actions / shape["max_asins"])
        return {
            "schema_version": SCHEMA_VERSION,
            "canary_status": "unknown",
            "planned_slots": len(shape["ports"]),
            "tested_slots": 0,
            "available_slots": None,
            "unique_egress_count": None,
            "duplicate_egress_count": None,
            "requested_capacity": requested_actions,
            "required_slots": required_slots,
            "slot_capacity": None,
            "capacity_gate_status": "denied",
            "capacity_gate_reason": "credentials_missing",
            "p95_latency_ms": None,
            "config_hash": capacity_config_hash(config),
            "sessions": [],
        }

    sessions: list[dict[str, Any]] = []
    identities: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    available_slots = 0
    duplicate_count = 0
    latencies: list[float] = []
    probe_slot = probe_slot or probe_proxy_slot
    for index, port in enumerate(shape["ports"], 1):
        probe = probe_slot(
            proxy_url=_proxy_url(shape["base"], port),
            target_url=shape["target_url"],
            timeout_seconds=shape["timeout_seconds"],
            username=username,
            password=password,
        )
        ok = bool(probe.get("ok"))
        identity = ipaddress.ip_address(str(probe.get("egress_ip") or "")) if ok else None
        latency = float(probe["latency_ms"]) if probe.get("latency_ms") is not None else None
        if ok:
            available_slots += 1
            if identity in identities:
                duplicate_count += 1
            identities.add(identity)
            if latency is not None:
                latencies.append(latency)
        sessions.append({
            "session_id": f"session-{index:02d}",
            "status": "available" if ok else "unavailable",
            "auth_status": "succeeded" if ok else str(probe.get("auth_status") or "unknown"),
            "connect_tls_status": "succeeded" if ok else str(probe.get("connect_tls_status") or "unknown"),
            "error_class": None if ok else str(probe.get("error_class") or "canary_failed"),
            "http_status": probe.get("http_status"),
            "latency_ms": latency,
        })

    unique_count = len(identities)
    required_slots = math.ceil(requested_actions / shape["max_asins"])
    slot_capacity = unique_count * shape["max_asins"]
    allowed = unique_count >= required_slots
    canary_status = "succeeded" if unique_count == len(shape["ports"]) else "partial" if unique_count else "failed"
    gate_reason = (
        "capacity_sufficient"
        if allowed
        else "duplicate_egress_capacity_insufficient"
        if duplicate_count
        else "unique_capacity_insufficient"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "canary_status": canary_status,
        "planned_slots": len(shape["ports"]),
        "tested_slots": len(shape["ports"]),
        "available_slots": available_slots,
        "unique_egress_count": unique_count,
        "duplicate_egress_count": duplicate_count,
        "requested_capacity": requested_actions,
        "required_slots": required_slots,
        "slot_capacity": slot_capacity,
        "capacity_gate_status": "allowed" if allowed else "denied",
        "capacity_gate_reason": gate_reason,
        "p95_latency_ms": _p95(latencies),
        "config_hash": capacity_config_hash(config),
        "sessions": sessions,
    }


def execute_canary(
    config_path: Path,
    *,
    tenant_id: str,
    requested_actions: int,
    operation_id: str,
    connect: Callable[[], Any],
    probe_slot: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Record one canary as an operation fact and return the identical safe projection."""
    from operation_ledger import ensure_schema, finish_operation, start_operation

    ensure_schema(connect)
    start_operation(operation_id, tenant_id, "canary", "dataimpulse-us", None, connect=connect)
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
        config = dict(document.get("worker") or {})
        result = run_proxy_canary(config, requested_actions=requested_actions, probe_slot=probe_slot)
        allowed = result["capacity_gate_status"] == "allowed"
        finish_operation(
            operation_id,
            tenant_id,
            "succeeded" if allowed else "failed",
            None if allowed else "capacity_gate",
            None if allowed else str(result["capacity_gate_reason"]),
            capacity_fact=result,
            connect=connect,
        )
        return result
    except Exception:
        finish_operation(
            operation_id,
            tenant_id,
            "failed",
            "configuration",
            "configuration_error",
            connect=connect,
        )
        raise


def _default_connect(dsn: str):
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("PostgreSQL canary requires psycopg") from exc
    return psycopg.connect(dsn, row_factory=dict_row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--requested-actions", type=int, required=True)
    parser.add_argument("--operation-id")
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    args = parser.parse_args(argv)
    operation_id = args.operation_id or f"op-canary-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "canary_status": "unknown", "capacity_gate_status": "denied", "capacity_gate_reason": "database_credentials_missing"}))
        return 2
    try:
        result = execute_canary(
            args.config,
            tenant_id=args.tenant_id,
            requested_actions=args.requested_actions,
            operation_id=operation_id,
            connect=lambda: _default_connect(dsn),
        )
    except (OSError, RuntimeError, ValueError, KeyError, tomllib.TOMLDecodeError):
        print(json.dumps({"schema_version": SCHEMA_VERSION, "canary_status": "unknown", "capacity_gate_status": "denied", "capacity_gate_reason": "configuration_error"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["capacity_gate_status"] == "allowed" else 3


__all__ = ["capacity_config_hash", "execute_canary", "probe_proxy_slot", "run_proxy_canary"]


if __name__ == "__main__":
    raise SystemExit(main())
