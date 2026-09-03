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
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

try:
    from collection_storage import CollectionRepository, PostgresCollectionRepository, SQLiteCollectionRepository
    from freshness_policy import FreshnessPolicy
except ModuleNotFoundError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from collection_storage import CollectionRepository, PostgresCollectionRepository, SQLiteCollectionRepository
    from freshness_policy import FreshnessPolicy

API_SCHEMA_VERSION = "amazon-us-collection-v1"
ASIN_PATH = re.compile(r"^/v1/asin/([A-Za-z]{2})/([A-Za-z0-9]{10})$")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_AGENT_SCOPES = {
    "read-agent": frozenset({"read"}),
    "refresh-agent": frozenset({"read", "refresh"}),
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
        traffic.setdefault("transfer_bytes", latest_evidence.get("transfer_bytes"))
    return {
        "product": repository.load_product(marketplace, asin),
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


class CollectionHandler(BaseHTTPRequestHandler):
    server: "CollectionServer"

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
            return AgentPrincipal("insecure-local", frozenset({"read", "refresh"}))
        agent_id = self.headers.get("X-Collection-Agent", "")
        supplied_agent_key = self.headers.get("X-Collection-Agent-Key", "")
        if agent_id in self.server.agent_scopes and supplied_agent_key:
            expected_agent_key = derive_agent_key(expected, agent_id)
            if hmac.compare_digest(supplied_agent_key, expected_agent_key):
                principal = AgentPrincipal(agent_id, self.server.agent_scopes[agent_id])
                if required_scope in principal.scopes:
                    if not self.server.allow_request(principal.agent_id):
                        self._send_json(
                            HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate_limited"},
                            {"Retry-After": str(self.server.retry_after_seconds(principal.agent_id))},
                        )
                        self._audit(principal, action, resource, "rate_limited")
                        return None
                    return principal
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "insufficient_scope", "required_scope": required_scope})
                self._audit(principal, action, resource, "forbidden")
                return None
        # The service key remains a local operator credential for compatibility
        # with existing loopback tooling. It is never exposed by the agent client.
        supplied = self.headers.get("X-Collection-API-Key", "")
        if hmac.compare_digest(supplied, expected):
            principal = AgentPrincipal("local-operator", frozenset({"read", "refresh"}))
            if self.server.allow_request(principal.agent_id):
                return principal
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate_limited"},
                {"Retry-After": str(self.server.retry_after_seconds(principal.agent_id))},
            )
            self._audit(principal, action, resource, "rate_limited")
            return None
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
            status_loader = getattr(self.server, "refresh_worker_status", None)
            if callable(status_loader):
                worker_status = status_loader()
                payload["refresh_worker"] = worker_status
                if worker_status.get("state") in {"failed", "blocked"}:
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
        if path == "/v1/jobs/status":
            try:
                payload = self.server.repository.load_job_status()
                self._audit(principal, "read", path, "ok")
                self._send_json(HTTPStatus.OK, payload)
            except (OSError, RuntimeError, sqlite3.Error, Exception):
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable"})
            return
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
            payload = {"schema_version": API_SCHEMA_VERSION, "job": job}
            if job.get("status") in {"completed", "failed", "cancelled"}:
                try:
                    payload["result"] = _terminal_job_result(self.server.repository, job)
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
        if path == "/v1/asin/batch":
            principal = self._authorized("read", "batch_read", path)
        else:
            principal = self._authorized("refresh", "request_refresh", path)
        if principal is None:
            return
        if path != "/v1/asin/batch" and not self.server.can_accept_refresh():
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "refresh_worker_unavailable"})
            self._audit(principal, "request_refresh", path, "worker_unavailable")
            return
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
                    items.append(item if item is not None else {"asin": asin, "marketplace": marketplace, "found": False})
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
    def __init__(self, address: tuple[str, int], db_path: Path | None = None, repository: CollectionRepository | None = None, api_key: str | None = None, agent_scopes: dict[str, frozenset[str]] | None = None, agent_rate_limit: int = 60, refresh_notifier=None, refresh_worker_status=None):
        if address[0] not in LOOPBACK_HOSTS:
            raise ValueError("Collection API only allows loopback host by default")
        if repository is None:
            if db_path is None:
                raise ValueError("db_path or repository is required")
            repository = SQLiteCollectionRepository(db_path)
        super().__init__(address, CollectionHandler)
        self.repository = repository
        self.api_key = api_key or ""
        self.agent_scopes = dict(agent_scopes or DEFAULT_AGENT_SCOPES)
        self.agent_rate_limit = max(1, int(agent_rate_limit))
        self._agent_windows: dict[str, tuple[float, int]] = {}
        self._agent_window_lock = threading.Lock()
        self.refresh_notifier = refresh_notifier
        self.refresh_worker_status = refresh_worker_status

    def notify_refresh_worker(self) -> None:
        if callable(self.refresh_notifier):
            self.refresh_notifier()

    def can_accept_refresh(self) -> bool:
        if not callable(self.refresh_worker_status):
            return True
        return self.refresh_worker_status().get("state") not in {"failed", "blocked", "stopped"}

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
    server = CollectionServer((host, port), db_path, repository, api_key, agent_rate_limit=agent_rate_limit)
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
