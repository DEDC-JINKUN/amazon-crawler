#!/usr/bin/env python3
"""Local, read-only Collection API for the Amazon US SQLite snapshot."""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sqlite3
from datetime import date, datetime
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


def load_product(db_path: Path, marketplace: str, asin: str) -> dict[str, Any] | None:
    """Return a snapshot plus task/evidence metadata without writing to SQLite."""
    return SQLiteCollectionRepository(db_path).load_product(marketplace, asin)


def load_job_status(db_path: Path) -> dict[str, Any]:
    return SQLiteCollectionRepository(db_path).load_job_status()


def load_evidence(db_path: Path, marketplace: str, asin: str, limit: int = 20) -> list[dict[str, Any]]:
    return SQLiteCollectionRepository(db_path).load_evidence(marketplace, asin, limit)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


class CollectionHandler(BaseHTTPRequestHandler):
    server: "CollectionServer"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = self.server.api_key
        if not expected:
            return True
        supplied = self.headers.get("X-Collection-API-Key", "")
        if hmac.compare_digest(supplied, expected):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("WWW-Authenticate", "ApiKey")
        self.end_headers()
        self.wfile.write(b'{"error":"unauthorized"}')
        return False

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True, "schema_version": API_SCHEMA_VERSION})
            return
        if not self._authorized():
            return
        if path == "/v1/jobs/status":
            try:
                self._send_json(HTTPStatus.OK, self.server.repository.load_job_status())
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable", "detail": str(exc)})
            return
        job_match = re.fullmatch(r"/v1/jobs/([A-Za-z0-9_-]+)", path)
        if job_match:
            try:
                job = self.server.repository.load_refresh_request(job_match.group(1))
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable", "detail": str(exc)})
                return
            if job is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "job_not_found", "job_id": job_match.group(1)})
                return
            self._send_json(HTTPStatus.OK, {"schema_version": API_SCHEMA_VERSION, "job": job})
            return
        evidence_match = re.fullmatch(r"/v1/asin/([A-Za-z]{2})/([A-Za-z0-9]{10})/evidence", path)
        if evidence_match:
            marketplace, asin = evidence_match.group(1).upper(), evidence_match.group(2).upper()
            try:
                evidence = self.server.repository.load_evidence(marketplace, asin)
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable", "detail": str(exc)})
                return
            if not evidence:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "evidence_not_found", "asin": asin, "marketplace": marketplace})
                return
            self._send_json(HTTPStatus.OK, {"schema_version": API_SCHEMA_VERSION, "marketplace": marketplace, "asin": asin, "items": evidence})
            return
        match = ASIN_PATH.fullmatch(path)
        if match:
            marketplace, asin = match.group(1).upper(), match.group(2).upper()
            try:
                payload = self.server.repository.load_product(marketplace, asin)
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable", "detail": str(exc)})
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
            self._send_json(HTTPStatus.OK, payload)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if not self._authorized():
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
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable", "detail": str(exc)})
                return
            self._send_json(HTTPStatus.OK, {"schema_version": API_SCHEMA_VERSION, "marketplace": marketplace, "items": items})
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
            requested_by = str(body.get("requested_by") or "collection-api")[:120]
            reason = str(body.get("reason") or "on_demand")[:240]
            marketplace, asin = match.group(1).upper(), match.group(2).upper()
            request = self.server.repository.request_refresh(marketplace, asin, requested_by, reason)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "detail": str(exc)})
            return
        except KeyError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "asin_not_found", "asin": asin, "marketplace": marketplace})
            return
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable", "detail": str(exc)})
            return
        self._send_json(HTTPStatus.ACCEPTED, {"schema_version": API_SCHEMA_VERSION, "job": request})

    def log_message(self, format: str, *args: Any) -> None:
        return


class CollectionServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], db_path: Path | None = None, repository: CollectionRepository | None = None, api_key: str | None = None):
        if address[0] not in LOOPBACK_HOSTS:
            raise ValueError("Collection API only allows loopback host by default")
        if repository is None:
            if db_path is None:
                raise ValueError("db_path or repository is required")
            repository = SQLiteCollectionRepository(db_path)
        super().__init__(address, CollectionHandler)
        self.repository = repository
        self.api_key = api_key or ""


def serve(db_path: Path | None = None, host: str = "127.0.0.1", port: int = 8765, repository: CollectionRepository | None = None, api_key: str | None = None) -> None:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("Collection API only allows loopback host by default")
    server = CollectionServer((host, port), db_path, repository, api_key)
    try:
        print(f"Collection API listening on http://{host}:{server.server_port}")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("state/amazon_us.sqlite3"))
    parser.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite")
    parser.add_argument("--dsn", default="", help="PostgreSQL DSN (required with --backend postgres)")
    parser.add_argument("--api-key-env", default="AMAZON_COLLECTION_API_KEY", help="Environment variable containing optional API key")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        repository = SQLiteCollectionRepository(args.db) if args.backend == "sqlite" else PostgresCollectionRepository(args.dsn)
        serve(args.db if args.backend == "sqlite" else None, args.host, args.port, repository, os.environ.get(args.api_key_env, ""))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
