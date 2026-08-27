#!/usr/bin/env python3
"""Local, read-only Collection API for the Amazon US SQLite snapshot."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from collection_storage import SQLiteCollectionRepository
except ModuleNotFoundError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from collection_storage import SQLiteCollectionRepository

API_SCHEMA_VERSION = "amazon-us-collection-v1"
ASIN_PATH = re.compile(r"^/v1/asin/([A-Za-z]{2})/([A-Za-z0-9]{10})$")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def load_product(db_path: Path, marketplace: str, asin: str) -> dict[str, Any] | None:
    """Return a snapshot plus task/evidence metadata without writing to SQLite."""
    return SQLiteCollectionRepository(db_path).load_product(marketplace, asin)


def load_job_status(db_path: Path) -> dict[str, Any]:
    return SQLiteCollectionRepository(db_path).load_job_status()


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
                self._send_json(HTTPStatus.OK, load_job_status(self.server.db_path))
            except (OSError, sqlite3.Error) as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable", "detail": str(exc)})
            return
        match = ASIN_PATH.fullmatch(path)
        if match:
            marketplace, asin = match.group(1).upper(), match.group(2).upper()
            try:
                payload = load_product(self.server.db_path, marketplace, asin)
            except (OSError, sqlite3.Error) as exc:
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
    def __init__(self, address: tuple[str, int], db_path: Path):
        if address[0] not in LOOPBACK_HOSTS:
            raise ValueError("Collection API only allows loopback host by default")
        super().__init__(address, CollectionHandler)
        self.db_path = db_path


def serve(db_path: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("Collection API only allows loopback host by default")
    server = CollectionServer((host, port), db_path)
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
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        serve(args.db, args.host, args.port)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
