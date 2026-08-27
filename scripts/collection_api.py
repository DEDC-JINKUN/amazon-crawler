#!/usr/bin/env python3
"""Local, read-only Collection API for the Amazon US SQLite snapshot."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from collection_storage import CollectionRepository, PostgresCollectionRepository, SQLiteCollectionRepository
except ModuleNotFoundError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from collection_storage import CollectionRepository, PostgresCollectionRepository, SQLiteCollectionRepository

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


class CollectionHandler(BaseHTTPRequestHandler):
    server: "CollectionServer"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True, "schema_version": API_SCHEMA_VERSION})
            return
        if path == "/v1/jobs/status":
            try:
                self._send_json(HTTPStatus.OK, self.server.repository.load_job_status())
            except (OSError, RuntimeError, sqlite3.Error) as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable", "detail": str(exc)})
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
            self._send_json(HTTPStatus.OK, payload)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})

    def log_message(self, format: str, *args: Any) -> None:
        return


class CollectionServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], db_path: Path | None = None, repository: CollectionRepository | None = None):
        if address[0] not in LOOPBACK_HOSTS:
            raise ValueError("Collection API only allows loopback host by default")
        if repository is None:
            if db_path is None:
                raise ValueError("db_path or repository is required")
            repository = SQLiteCollectionRepository(db_path)
        super().__init__(address, CollectionHandler)
        self.repository = repository


def serve(db_path: Path | None = None, host: str = "127.0.0.1", port: int = 8765, repository: CollectionRepository | None = None) -> None:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("Collection API only allows loopback host by default")
    server = CollectionServer((host, port), db_path, repository)
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
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        repository = SQLiteCollectionRepository(args.db) if args.backend == "sqlite" else PostgresCollectionRepository(args.dsn)
        serve(args.db if args.backend == "sqlite" else None, args.host, args.port, repository)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
