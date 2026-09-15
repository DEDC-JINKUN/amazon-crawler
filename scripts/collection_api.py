#!/usr/bin/env python3
"""Loopback API for Amazon US snapshots and bounded refresh requests."""
from __future__ import annotations

import argparse
import hmac
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

try:
    from collection_storage import CampaignConflictError, CollectionRepository, PostgresCollectionRepository, SQLiteCollectionRepository
    from freshness_policy import FreshnessPolicy
except ModuleNotFoundError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from collection_storage import CampaignConflictError, CollectionRepository, PostgresCollectionRepository, SQLiteCollectionRepository
    from freshness_policy import FreshnessPolicy

API_SCHEMA_VERSION = "amazon-us-collection-v1"
ASIN_PATH = re.compile(r"^/v1/asin/([A-Za-z]{2})/([A-Za-z0-9]{10})$")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_AGENT_SCOPES = {
    "read-agent": frozenset({"read"}),
    "refresh-agent": frozenset({"read", "refresh"}),
    "watch-manager": frozenset({"read", "watch_write"}),
}
AGENT_CAPABILITIES = {
    "schema_version": API_SCHEMA_VERSION,
    "transport": "loopback_http",
    "execution": "asynchronous_jobs",
    "limits": {"read_asins": 100, "refresh_asins": 5, "campaign_asins": "configured"},
    "tools": {
        "get_product": {"method": "GET", "path": "/v1/asin/US/{asin}"},
        "batch_get": {"method": "POST", "path": "/v1/asin/batch"},
        "refresh": {"method": "POST", "path": "/v1/asin/refresh", "result": "job_ids"},
        "submit_campaign": {"method": "POST", "path": "/v1/campaigns", "result": "campaign_id"},
        "get_campaign": {"method": "GET", "path": "/v1/campaigns/{campaign_id}"},
        "cancel_campaign": {"method": "POST", "path": "/v1/campaigns/{campaign_id}/cancel"},
        "get_job": {"method": "GET", "path": "/v1/jobs/{job_id}?view=summary"},
        "watch_summary": {"method": "GET", "path": "/v1/watch/summary"},
        "watch_events": {"method": "GET", "path": "/v1/watch/events"},
        "watch_deliveries": {"method": "GET", "path": "/v1/watch/deliveries"},
    },
}


def derive_agent_key(master_key: str, agent_id: str) -> str:
    """Derive a non-master, per-agent credential from the sealed service key."""
    if not master_key or agent_id not in DEFAULT_AGENT_SCOPES:
        raise ValueError("a configured agent identity and service key are required")
    material = f"amazon-us-collection-agent-v1:{agent_id}".encode("utf-8")
    return hmac.new(master_key.encode("utf-8"), material, hashlib.sha256).hexdigest()


def agent_headers(master_key: str, agent_id: str) -> dict[str, str]:
    return {"X-Collection-Agent": agent_id, "X-Collection-Agent-Key": derive_agent_key(master_key, agent_id)}


class AgentPrincipal:
    def __init__(self, agent_id: str, scopes: frozenset[str]):
        self.agent_id = agent_id
        self.scopes = scopes


def load_product(db_path: Path, marketplace: str, asin: str) -> dict[str, Any] | None:
    """Return a snapshot plus task/evidence metadata without writing to SQLite."""
    return SQLiteCollectionRepository(db_path).load_product(marketplace, asin)


def load_history(db_path: Path, marketplace: str, asin: str, limit: int = 20) -> list[dict[str, Any]]:
    return SQLiteCollectionRepository(db_path).load_history(marketplace, asin, limit)


def load_job_status(db_path: Path) -> dict[str, Any]:
    return SQLiteCollectionRepository(db_path).load_job_status()


def load_evidence(db_path: Path, marketplace: str, asin: str, limit: int = 20) -> list[dict[str, Any]]:
    return SQLiteCollectionRepository(db_path).load_evidence(marketplace, asin, limit)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _elapsed_seconds(start: Any, finish: Any) -> float | None:
    if not start or not finish:
        return None
    try:
        start_value = start if isinstance(start, datetime) else datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        finish_value = finish if isinstance(finish, datetime) else datetime.fromisoformat(str(finish).replace("Z", "+00:00"))
        return max(0.0, round((finish_value - start_value).total_seconds(), 3))
    except (TypeError, ValueError):
        return None


def _at_or_after(value: Any, baseline: Any) -> bool | None:
    if not value or not baseline:
        return None
    try:
        observed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        expected = baseline if isinstance(baseline, datetime) else datetime.fromisoformat(str(baseline).replace("Z", "+00:00"))
        return observed >= expected - timedelta(seconds=1)
    except (TypeError, ValueError):
        return None


def _terminal_job_result(repository: CollectionRepository, job: dict[str, Any]) -> dict[str, Any]:
    marketplace = str(job.get("marketplace") or "US").upper()
    asin = str(job.get("asin") or "").upper()
    evidence_items = repository.load_evidence(marketplace, asin, limit=1)
    latest_evidence = evidence_items[0] if evidence_items else None
    traffic: dict[str, Any] = {}
    if latest_evidence:
        context = latest_evidence.get("context_json") or {}
        if isinstance(context, str):
            try:
                context = json.loads(context)
            except json.JSONDecodeError:
                context = {}
        if isinstance(context, dict) and isinstance(context.get("traffic"), dict):
            traffic.update(context["traffic"])
        relay = context.get("proxy_connect_relay") if isinstance(context, dict) else None
        if isinstance(relay, dict) and relay.get("tunnel_bytes") is not None:
            traffic["proxy_tunnel_bytes"] = relay.get("tunnel_bytes")
            traffic["proxy_tunnel_client_to_upstream_bytes"] = relay.get("client_to_upstream_bytes")
            traffic["proxy_tunnel_upstream_to_client_bytes"] = relay.get("upstream_to_client_bytes")
        traffic.setdefault("transfer_bytes", latest_evidence.get("transfer_bytes"))
    product = repository.load_product(marketplace, asin)
    return {
        "product": product,
        "quality_status": (product or {}).get("quality_status"),
        "latest_evidence": latest_evidence,
        "evidence_after_request": _at_or_after(
            (latest_evidence or {}).get("retrieved_at"), job.get("requested_at")
        ),
        "timing": {
            "requested_at": job.get("requested_at"),
            "claimed_at": job.get("claimed_at"),
            "completed_at": job.get("completed_at"),
            "elapsed_seconds": _elapsed_seconds(job.get("requested_at"), job.get("completed_at")),
        },
        "traffic": traffic,
    }


def _terminal_job_summary(repository: CollectionRepository, job: dict[str, Any]) -> dict[str, Any]:
    """Small Agent-facing result; detailed evidence remains explicitly available."""
    result = _terminal_job_result(repository, job)
    payload = result.get("product") if isinstance(result.get("product"), dict) else {}
    product = payload.get("product") if isinstance(payload.get("product"), dict) else payload
    evidence = result.get("latest_evidence") if isinstance(result.get("latest_evidence"), dict) else {}
    return {
        "quality_status": result.get("quality_status"),
        "product": {
            "asin": str(job.get("asin") or product.get("asin") or ""),
            "title": product.get("title"),
            "price": product.get("price"),
            "availability": product.get("availability"),
            "rating": product.get("rating"),
        },
        "evidence": {
            "run_id": evidence.get("run_id"),
            "retrieved_at": evidence.get("retrieved_at"),
            "source": evidence.get("source_type"),
            "http_status": evidence.get("http_status"),
            "transfer_bytes": evidence.get("transfer_bytes"),
            "error": evidence.get("error_code") or evidence.get("block_reason"),
        },
        "evidence_after_request": result.get("evidence_after_request"),
        "timing": result.get("timing"),
        "traffic": result.get("traffic"),
        "details_available": True,
    }


def _agent_product_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the default Agent read projection without local evidence internals."""
    product = payload.get("product") if isinstance(payload.get("product"), dict) else {}
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    return {
        "schema_version": payload.get("schema_version") or API_SCHEMA_VERSION,
        "marketplace": payload.get("marketplace"),
        "asin": payload.get("asin") or product.get("asin"),
        "retrieved_at": payload.get("retrieved_at"),
        "freshness": payload.get("freshness"),
        "quality_status": payload.get("quality_status"),
        "source": payload.get("source"),
        "product": {
            "asin": product.get("asin"), "title": product.get("title"),
            "price": product.get("price"), "availability": product.get("availability"),
            "rating": product.get("rating"),
        } if product else None,
        "task": {
            "status": task.get("status"), "task_stage": task.get("task_stage"),
            "last_error": task.get("last_error"),
        } if task else None,
        "evidence": {
            "run_id": evidence.get("run_id"), "retrieved_at": evidence.get("retrieved_at"),
            "source": evidence.get("source_type"), "http_status": evidence.get("http_status"),
            "transfer_bytes": evidence.get("transfer_bytes"),
            "error": evidence.get("error_code") or evidence.get("block_reason"),
        } if evidence else None,
        "counts": payload.get("counts"),
    }


class CollectionHandler(BaseHTTPRequestHandler):
    server: "CollectionServer"

    def _discard_auth_body(self, limit: int = 1048576, timeout_seconds: float = 0.25) -> bool:
        """Drain a bounded denied body within one fixed deadline."""
        if self.command not in {"POST", "PUT", "PATCH"}:
            return True
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self.close_connection = True
            return False
        if length < 0 or length > limit:
            self.close_connection = True
            return False
        if not length:
            return True
        deadline = time.monotonic() + max(0.01, float(timeout_seconds))
        previous_timeout = self.connection.gettimeout()
        remaining = length
        try:
            while remaining:
                seconds_left = deadline - time.monotonic()
                if seconds_left <= 0:
                    self.close_connection = True
                    return False
                self.connection.settimeout(seconds_left)
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    self.close_connection = True
                    return False
                remaining -= len(chunk)
            return True
        except (OSError, TimeoutError):
            self.close_connection = True
            return False
        finally:
            try:
                self.connection.settimeout(previous_timeout)
            except OSError:
                self.close_connection = True

    def _send_json(self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in dict(headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _audit(self, principal: AgentPrincipal | None, action: str, resource: str, outcome: str) -> None:
        recorder = getattr(self.server.repository, "record_api_audit", None)
        if recorder is None:
            return
        try:
            recorder(agent_id=principal.agent_id if principal else "unauthenticated", action=action, resource=resource, outcome=outcome)
        except Exception:
            # Audit availability must not turn a read request into a data leak;
            # PostgreSQL production uses a durable table and is checked by readyz.
            return

    def _authorized(self, required_scope: str, action: str, resource: str) -> AgentPrincipal | None:
        expected = self.server.api_key
        if not expected:
            return AgentPrincipal("insecure-local", frozenset({"read", "refresh", "watch_write"}))
        agent_id = self.headers.get("X-Collection-Agent", "")
        supplied_agent_key = self.headers.get("X-Collection-Agent-Key", "")
        if agent_id in self.server.agent_scopes and supplied_agent_key:
            expected_agent_key = derive_agent_key(expected, agent_id)
            if hmac.compare_digest(supplied_agent_key, expected_agent_key):
                principal = AgentPrincipal(agent_id, self.server.agent_scopes[agent_id])
                if required_scope in principal.scopes:
                    if not self.server.allow_request(principal.agent_id):
                        self._discard_auth_body()
                        self._send_json(
                            HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate_limited"},
                            {"Retry-After": str(self.server.retry_after_seconds(principal.agent_id))},
                        )
                        self._audit(principal, action, resource, "rate_limited")
                        return None
                    return principal
                self._discard_auth_body()
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "insufficient_scope", "required_scope": required_scope})
                self._audit(principal, action, resource, "forbidden")
                return None
        # The service key remains a local operator credential for compatibility
        # with existing loopback tooling. It is never exposed by the agent client.
        supplied = self.headers.get("X-Collection-API-Key", "")
        if hmac.compare_digest(supplied, expected):
            principal = AgentPrincipal("local-operator", frozenset({"read", "refresh", "watch_write"}))
            if self.server.allow_request(principal.agent_id):
                return principal
            self._discard_auth_body()
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate_limited"},
                {"Retry-After": str(self.server.retry_after_seconds(principal.agent_id))},
            )
            self._audit(principal, action, resource, "rate_limited")
            return None
        self._discard_auth_body()
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("WWW-Authenticate", "ApiKey")
        self.end_headers()
        self.wfile.write(b'{"error":"unauthorized"}')
        self._audit(None, action, resource, "unauthorized")
        return None

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True, "schema_version": API_SCHEMA_VERSION})
            return
        if path == "/readyz":
            try:
                checker = getattr(self.server.repository, "load_schema_contract", None)
                if checker is not None:
                    contract = checker()
                    if contract.get("item_state") != ["lease_expires_at", "lease_owner", "lease_token", "next_retry_at"] or contract.get("collection_evidence") != ["context_json", "transfer_bytes"]:
                        self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "schema_not_ready"})
                        return
                campaign_checker=getattr(self.server.repository,"load_campaign_contract",None)
                if callable(campaign_checker) and not campaign_checker():
                    self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,{"ok":False,"error":"campaign_schema_not_ready"}); return
                self.server.repository.load_job_status()
            except (OSError, RuntimeError, sqlite3.Error, KeyError, ValueError):
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "database_unavailable"})
                return
            except Exception:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "database_unavailable"})
                return
            payload = {"ok": True, "schema_version": API_SCHEMA_VERSION}
            tenant_id = getattr(self.server.repository, "tenant_id", None)
            if tenant_id:
                payload["tenant_id"] = tenant_id
            subject_type = getattr(self.server.repository, "subject_type", None)
            if subject_type:
                payload["subject_type"] = subject_type
            status_loader = getattr(self.server, "refresh_worker_status", None)
            if callable(status_loader):
                worker_status = status_loader()
                payload["refresh_worker"] = worker_status
                if not self.server.refresh_worker_ready(worker_status):
                    payload.update({"ok": False, "error": "refresh_worker_unavailable"})
                    self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, payload)
                    return
            self._send_json(HTTPStatus.OK, payload)
            return
        principal: AgentPrincipal | None = None
        if path not in {"/healthz", "/readyz"}:
            principal = self._authorized("read", "read", path)
            if principal is None:
                return
        if path == "/v1/capabilities":
            self._audit(principal, "read", path, "ok")
            capabilities=json.loads(json.dumps(AGENT_CAPABILITIES))
            capabilities["limits"]["campaign_asins"]=self.server.campaign_max_asins
            self._send_json(HTTPStatus.OK, capabilities)
            return
        if path.startswith("/v1/watch/"):
            watch = getattr(self.server, "reporting_repository", None)
            tenant_id = getattr(self.server.repository, "tenant_id", None)
            if watch is None or not tenant_id:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "watch_reporting_unavailable"})
                return
            try:
                if path == "/v1/watch/summary":
                    payload = watch.summary(tenant_id=tenant_id, marketplace="US")
                elif path == "/v1/watch/events":
                    payload = {"items": watch.list_events(tenant_id=tenant_id, marketplace="US")}
                elif path == "/v1/watch/deliveries":
                    payload = {"items": watch.list_deliveries(tenant_id=tenant_id)}
                else:
                    report_match = re.fullmatch(r"/v1/watch/reports/([A-Za-z0-9_-]+)", path)
                    if not report_match:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"}); return
                    payload = watch.load_report(report_match.group(1), tenant_id=tenant_id)
                    if payload is None:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": "report_not_found"}); return
                self._audit(principal, "read_watch", path, "ok")
                self._send_json(HTTPStatus.OK, {"schema_version": API_SCHEMA_VERSION, **payload})
            except Exception:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "watch_reporting_unavailable"})
            return
        if path == "/v1/recovery/status":
            try:
                payload = self.server.repository.load_recovery_status()
                self._audit(principal, "read", path, "ok")
                self._send_json(HTTPStatus.OK, payload)
            except Exception:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error":"recovery_unavailable"})
            return
        if path == "/v1/jobs/status":
            try:
                payload = self.server.repository.load_job_status()
                self._audit(principal, "read", path, "ok")
                self._send_json(HTTPStatus.OK, payload)
            except (OSError, RuntimeError, sqlite3.Error, Exception):
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable"})
            return
        campaign_match = re.fullmatch(r"/v1/campaigns/(campaign-[a-f0-9]{32})", path)
        if campaign_match:
            loader=getattr(self.server.repository,"load_campaign",None)
            try:
                query=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
                member_mode=str(query.get("members",["summary"])[0])
                if member_mode not in {"summary","all","page"}: raise ValueError("invalid campaign member view")
                offset=0;limit=None
                if member_mode=="page":
                    raw_offset=str(query.get("offset",["0"])[0]);raw_limit=str(query.get("limit",["100"])[0])
                    if not raw_offset.isdigit() or not raw_limit.isdigit(): raise ValueError("invalid campaign member page")
                    offset=int(raw_offset);limit=int(raw_limit)
                    if offset<0 or not 1<=limit<=100: raise ValueError("invalid campaign member page")
                payload=loader(
                    campaign_match.group(1),include_members=member_mode!="summary",
                    member_offset=offset,member_limit=limit,
                ) if callable(loader) else None
            except ValueError as exc: self._send_json(HTTPStatus.BAD_REQUEST,{"error":"invalid_request","detail":str(exc)}); return
            except Exception: self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,{"error":"database_unavailable"}); return
            if payload is None: self._send_json(HTTPStatus.NOT_FOUND,{"error":"campaign_not_found"}); return
            self._audit(principal,"read_campaign",campaign_match.group(1),"ok")
            self._send_json(HTTPStatus.OK,{"schema_version":API_SCHEMA_VERSION,"campaign":payload}); return
        job_match = re.fullmatch(r"/v1/jobs/([A-Za-z0-9_-]+)", path)
        if job_match:
            try:
                job = self.server.repository.load_refresh_request(job_match.group(1))
            except (OSError, RuntimeError, sqlite3.Error, Exception):
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable"})
                return
            if job is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "job_not_found", "job_id": job_match.group(1)})
                return
            self._audit(principal, "read", path, "ok")
            view = parse_qs(urlsplit(self.path).query).get("view", ["summary"])[0]
            if view not in {"summary", "detailed"}:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_job_view"})
                return
            payload = {"schema_version": API_SCHEMA_VERSION, "job": job}
            if job.get("status") in {"completed", "failed", "cancelled"}:
                try:
                    payload["result"] = (
                        _terminal_job_summary(self.server.repository, job)
                        if view == "summary" else _terminal_job_result(self.server.repository, job)
                    )
                except Exception:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable"})
                    return
            self._send_json(HTTPStatus.OK, payload)
            return
        evidence_match = re.fullmatch(r"/v1/asin/([A-Za-z]{2})/([A-Za-z0-9]{10})/evidence", path)
        if evidence_match:
            marketplace, asin = evidence_match.group(1).upper(), evidence_match.group(2).upper()
            try:
                evidence = self.server.repository.load_evidence(marketplace, asin)
            except (OSError, RuntimeError, sqlite3.Error, Exception):
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable"})
                return
            if not evidence:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "evidence_not_found", "asin": asin, "marketplace": marketplace})
                return
            self._audit(principal, "read", path, "ok")
            self._send_json(HTTPStatus.OK, {"schema_version": API_SCHEMA_VERSION, "marketplace": marketplace, "asin": asin, "items": evidence})
            return
        history_match = re.fullmatch(r"/v1/asin/([A-Za-z]{2})/([A-Za-z0-9]{10})/history", path)
        if history_match:
            marketplace, asin = history_match.group(1).upper(), history_match.group(2).upper()
            try:
                history = self.server.repository.load_history(marketplace, asin)
            except (OSError, RuntimeError, sqlite3.Error, Exception):
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable"})
                return
            if not history:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "history_not_found", "asin": asin, "marketplace": marketplace})
                return
            self._audit(principal, "read", path, "ok")
            self._send_json(HTTPStatus.OK, {"schema_version": API_SCHEMA_VERSION, "marketplace": marketplace, "asin": asin, "items": history})
            return
        match = ASIN_PATH.fullmatch(path)
        if match:
            marketplace, asin = match.group(1).upper(), match.group(2).upper()
            try:
                payload = self.server.repository.load_product(marketplace, asin)
            except (OSError, RuntimeError, sqlite3.Error, Exception):
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable"})
                return
            if payload is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "asin_not_found", "asin": asin, "marketplace": marketplace})
                return
            requested = [item.strip() for item in parse_qs(urlsplit(self.path).query).get("fields", [""])[0].split(",") if item.strip()]
            if requested:
                if len(requested) > 10 or any(len(item) > 40 for item in requested):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_fields"})
                    return
                payload["freshness"] = FreshnessPolicy().evaluate(payload.get("retrieved_at"), requested)
            self._audit(principal, "read", path, "ok")
            self._send_json(HTTPStatus.OK, payload)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        watch_ack = re.fullmatch(r"/v1/watch/events/([A-Za-z0-9_-]+)/ack", path)
        if watch_ack:
            principal = self._authorized("watch_write", "ack_watch_event", path)
        elif path == "/v1/asin/batch":
            principal = self._authorized("read", "batch_read", path)
        else:
            principal = self._authorized("refresh", "request_refresh", path)
        if principal is None:
            return
        if watch_ack:
            watch = getattr(self.server, "reporting_repository", None)
            if watch is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "watch_reporting_unavailable"}); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 1024: raise ValueError("request body too large")
                body = json.loads((self.rfile.read(length) if length else b"{}").decode("utf-8"))
                reason = str((body or {}).get("reason") or "")[:240] if isinstance(body, dict) else ""
                tenant_id = getattr(self.server.repository, "tenant_id", None)
                if not tenant_id or not watch.acknowledge_event(tenant_id=tenant_id, event_id=watch_ack.group(1), actor=principal.agent_id, reason=reason):
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "watch_event_not_found"}); return
                self._audit(principal, "ack_watch_event", watch_ack.group(1), "ok")
                self._send_json(HTTPStatus.OK, {"schema_version": API_SCHEMA_VERSION, "event_id": watch_ack.group(1), "status": "acknowledged"})
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "detail": str(exc)})
            except Exception:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "watch_reporting_unavailable"})
            return
        if path != "/v1/asin/batch" and not re.fullmatch(r"/v1/campaigns/campaign-[a-f0-9]{32}/cancel",path) and not self.server.can_accept_refresh():
            self._discard_auth_body()
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "refresh_worker_unavailable"})
            self._audit(principal, "request_refresh", path, "worker_unavailable")
            return
        cancel_match = re.fullmatch(r"/v1/campaigns/(campaign-[a-f0-9]{32})/cancel", path)
        if cancel_match:
            cancel=getattr(self.server.repository,"cancel_campaign",None)
            try: campaign=cancel(cancel_match.group(1)) if callable(cancel) else None
            except Exception: self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,{"error":"database_unavailable"}); return
            if campaign is None: self._send_json(HTTPStatus.NOT_FOUND,{"error":"campaign_not_found"}); return
            self._audit(principal,"cancel_campaign",cancel_match.group(1),"ok")
            self._send_json(HTTPStatus.OK,{"schema_version":API_SCHEMA_VERSION,"campaign":campaign}); return
        if path == "/v1/campaigns":
            try:
                length=int(self.headers.get("Content-Length","0"))
                if length>1048576: raise ValueError("campaign request body too large")
                body=json.loads((self.rfile.read(length) if length else b"{}").decode("utf-8"))
                if not isinstance(body,dict) or not isinstance(body.get("asins"),list): raise ValueError("asins must be a JSON array")
                key=str(self.headers.get("Idempotency-Key") or body.get("idempotency_key") or "")
                submit=getattr(self.server.repository,"submit_campaign",None)
                if not callable(submit): self._send_json(HTTPStatus.SERVICE_UNAVAILABLE,{"error":"campaigns_unavailable"}); return
                campaign=submit(asins=body["asins"],idempotency_key=key,requested_by=principal.agent_id,
                    subject_type=body.get("subject_type","own"),
                    priority=body.get("priority",50),freshness_seconds=body.get("freshness_seconds"),
                    fields=body.get("fields") or [],deadline_seconds=body.get("deadline_seconds",86400),
                    max_members=self.server.campaign_max_asins)
            except KeyError as exc: self._send_json(HTTPStatus.NOT_FOUND,{"error":"asin_not_found","detail":str(exc)}); return
            except CampaignConflictError as exc: self._send_json(HTTPStatus.CONFLICT,{"error":"idempotency_conflict","detail":str(exc)}); return
            except (ValueError,json.JSONDecodeError) as exc: self._send_json(HTTPStatus.BAD_REQUEST,{"error":"invalid_request","detail":str(exc)}); return
            except Exception: self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,{"error":"database_unavailable"}); return
            self._audit(principal,"submit_campaign",campaign["campaign_id"],"accepted")
            self.server.notify_refresh_worker()
            self._send_json(HTTPStatus.ACCEPTED,{"schema_version":API_SCHEMA_VERSION,"campaign":campaign}); return
        if path == "/v1/asin/batch":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 65536:
                    raise ValueError("request body too large")
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                if not isinstance(body, dict) or not isinstance(body.get("asins"), list):
                    raise ValueError("asins must be a JSON array")
                marketplace = str(body.get("marketplace") or "US").upper()
                if marketplace != "US":
                    raise ValueError("only US marketplace is supported")
                asins = []
                for value in body["asins"]:
                    asin = str(value).upper()
                    if not re.fullmatch(r"[A-Z0-9]{10}", asin):
                        raise ValueError(f"invalid ASIN: {asin}")
                    if asin not in asins:
                        asins.append(asin)
                if not asins or len(asins) > 100:
                    raise ValueError("asins must contain 1 to 100 unique values")
                items = []
                for asin in asins:
                    item = self.server.repository.load_product(marketplace, asin)
                    items.append(_agent_product_summary(item) if item is not None else {"asin": asin, "marketplace": marketplace, "found": False})
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "detail": str(exc)})
                return
            except Exception:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable"})
                return
            self._audit(principal, "batch_read", path, "ok")
            self._send_json(HTTPStatus.OK, {"schema_version": API_SCHEMA_VERSION, "marketplace": marketplace, "items": items})
            return
        if path == "/v1/asin/refresh":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > 8192:
                    raise ValueError("request body too large")
                body = json.loads((self.rfile.read(length) if length else b"{}").decode("utf-8"))
                if not isinstance(body, dict) or not isinstance(body.get("asins"), list):
                    raise ValueError("asins must be a JSON array")
                marketplace = str(body.get("marketplace") or "US").upper()
                if marketplace != "US":
                    raise ValueError("only US marketplace is supported")
                asins: list[str] = []
                for value in body["asins"]:
                    asin = str(value).upper()
                    if not re.fullmatch(r"[A-Z0-9]{10}", asin):
                        raise ValueError(f"invalid ASIN: {asin}")
                    if asin not in asins:
                        asins.append(asin)
                if not 1 <= len(asins) <= 5:
                    raise ValueError("asins must contain 1 to 5 unique values")
                reason = str(body.get("reason") or "on_demand")[:240]
                missing = [asin for asin in asins if self.server.repository.load_product(marketplace, asin) is None]
                if missing:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "asin_not_found", "asins": missing})
                    return
                batcher = getattr(self.server.repository, "request_refresh_batch", None)
                jobs = (
                    batcher(marketplace, asins, principal.agent_id, reason)
                    if callable(batcher)
                    else [self.server.repository.request_refresh(marketplace, asin, principal.agent_id, reason) for asin in asins]
                )
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "detail": str(exc)})
                return
            except Exception:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable"})
                return
            self._audit(principal, "request_refresh_batch", f"{marketplace}/{len(asins)}", "accepted")
            self.server.notify_refresh_worker()
            self._send_json(HTTPStatus.ACCEPTED, {"schema_version": API_SCHEMA_VERSION, "jobs": jobs})
            return
        match = re.fullmatch(r"/v1/asin/([A-Za-z]{2})/([A-Za-z0-9]{10})/refresh", path)
        if not match:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 4096:
                raise ValueError("request body too large")
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            reason = str(body.get("reason") or "on_demand")[:240]
            marketplace, asin = match.group(1).upper(), match.group(2).upper()
            request = self.server.repository.request_refresh(marketplace, asin, principal.agent_id, reason)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "detail": str(exc)})
            return
        except KeyError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "asin_not_found", "asin": asin, "marketplace": marketplace})
            return
        except Exception:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable"})
            return
        self._audit(principal, "request_refresh", f"{marketplace}/{asin}", "accepted")
        self.server.notify_refresh_worker()
        self._send_json(HTTPStatus.ACCEPTED, {"schema_version": API_SCHEMA_VERSION, "job": request})

    def log_message(self, format: str, *args: Any) -> None:
        return


class CollectionServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], db_path: Path | None = None, repository: CollectionRepository | None = None, api_key: str | None = None, agent_scopes: dict[str, frozenset[str]] | None = None, agent_rate_limit: int = 60, refresh_notifier=None, refresh_worker_status=None, reporting_repository=None, campaign_max_asins: int = 5000):
        if address[0] not in LOOPBACK_HOSTS:
            raise ValueError("Collection API only allows loopback host by default")
        if repository is None:
            if db_path is None:
                raise ValueError("db_path or repository is required")
            repository = SQLiteCollectionRepository(db_path)
        super().__init__(address, CollectionHandler)
        self.repository = repository
        self.api_key = api_key or ""
        self.campaign_max_asins=max(1,min(int(campaign_max_asins),100000))
        self.agent_scopes = dict(agent_scopes or DEFAULT_AGENT_SCOPES)
        self.agent_rate_limit = max(1, int(agent_rate_limit))
        self._agent_windows: dict[str, tuple[float, int]] = {}
        self._agent_window_lock = threading.Lock()
        self.refresh_notifier = refresh_notifier
        self.refresh_worker_status = refresh_worker_status
        self.reporting_repository = reporting_repository

    def notify_refresh_worker(self) -> None:
        if callable(self.refresh_notifier):
            self.refresh_notifier()

    def can_accept_refresh(self) -> bool:
        if not callable(self.refresh_worker_status):
            return True
        return self.refresh_worker_ready(self.refresh_worker_status())

    def refresh_worker_ready(self, worker_status: dict[str, Any]) -> bool:
        if worker_status.get("state") != "running":
            return False
        if bool(worker_status.get("capacity_backpressure")):
            return False
        count_fields = ("requested_workers", "effective_workers", "alive_workers")
        present = tuple(name in worker_status for name in count_fields)
        if not any(present):
            return True
        if not all(present):
            return False
        try:
            requested = int(worker_status.get("requested_workers") or 0)
            effective = int(worker_status.get("effective_workers") or 0)
            alive = int(worker_status.get("alive_workers") or 0)
        except (TypeError, ValueError):
            return False
        return requested > 0 and requested == effective == alive

    def allow_request(self, agent_id: str) -> bool:
        import time
        now = time.monotonic()
        with self._agent_window_lock:
            start, count = self._agent_windows.get(agent_id, (now, 0))
            if now - start >= 60:
                start, count = now, 0
            if count >= self.agent_rate_limit:
                self._agent_windows[agent_id] = (start, count)
                return False
            self._agent_windows[agent_id] = (start, count + 1)
            return True

    def retry_after_seconds(self, agent_id: str) -> int:
        import math
        import time
        with self._agent_window_lock:
            start, _count = self._agent_windows.get(agent_id, (time.monotonic(), 0))
        return max(1, min(60, math.ceil(60.0 - max(0.0, time.monotonic() - start))))


def serve(db_path: Path | None = None, host: str = "127.0.0.1", port: int = 8765, repository: CollectionRepository | None = None, api_key: str | None = None, agent_rate_limit: int = 60) -> None:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("Collection API only allows loopback host by default")
    reporting_repository = None
    if isinstance(repository, PostgresCollectionRepository):
        from product_watch.delivery_repository import WatchDeliveryRepository
        reporting_repository = WatchDeliveryRepository(repository.dsn)
    server = CollectionServer((host, port), db_path, repository, api_key, agent_rate_limit=agent_rate_limit, reporting_repository=reporting_repository)
    try:
        print(f"Collection API listening on http://{host}:{server.server_port}")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def create_repository(backend: str, db_path: Path, dsn: str, tenant_id: str) -> CollectionRepository:
    if backend == "sqlite":
        return SQLiteCollectionRepository(db_path)
    return PostgresCollectionRepository(dsn, tenant_id=tenant_id)


def resolve_api_key(environment_name: str, required: bool) -> str:
    key = os.environ.get(environment_name, "")
    if required and not key:
        raise ValueError(f"required API key environment variable is missing: {environment_name}")
    return key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("state/amazon_us.sqlite3"))
    parser.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite")
    parser.add_argument("--dsn", default="", help="PostgreSQL DSN (required with --backend postgres)")
    parser.add_argument("--dsn-env", default="", help="Environment variable containing the PostgreSQL DSN")
    parser.add_argument("--tenant-id", default="default", help="PostgreSQL tenant to expose")
    parser.add_argument("--api-key-env", default="AMAZON_COLLECTION_API_KEY", help="Environment variable containing optional API key")
    parser.add_argument("--require-api-key", action="store_true", help="Fail startup when the API key environment variable is empty")
    parser.add_argument("--allow-insecure-local-testing", action="store_true", help="Only for offline tests; production startup requires an API key")
    parser.add_argument("--agent-rate-limit", type=int, default=60, help="Maximum authenticated requests per agent per minute")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        dsn = args.dsn or (os.environ.get(args.dsn_env, "") if args.dsn_env else "")
        repository = create_repository(args.backend, args.db, dsn, args.tenant_id)
        require_key = not args.allow_insecure_local_testing or args.require_api_key
        serve(args.db if args.backend == "sqlite" else None, args.host, args.port, repository, resolve_api_key(args.api_key_env, require_key), args.agent_rate_limit)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
