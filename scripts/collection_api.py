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

API_SCHEMA_VERSION = "amazon-us-collection-v1"
ASIN_PATH = re.compile(r"^/v1/asin/([A-Za-z]{2})/([A-Za-z0-9]{10})$")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _dict_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _read_connection(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    conn = sqlite3.connect(str(db_path), timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_product(db_path: Path, marketplace: str, asin: str) -> dict[str, Any] | None:
    """Return a snapshot plus task/evidence metadata without writing to SQLite."""
    conn = _read_connection(db_path)
    try:
        product = conn.execute("SELECT * FROM product_snapshot WHERE marketplace=? AND asin=?", (marketplace, asin)).fetchone()
        state = conn.execute("SELECT * FROM item_state WHERE marketplace=? AND asin=?", (marketplace, asin)).fetchone()
        if product is None and state is None:
            return None
        evidence = conn.execute(
            "SELECT run_id, url, http_status, retrieved_at, source_type, content_hash, raw_html_path, block_reason, parser_version, error_code "
            "FROM collection_evidence WHERE marketplace=? AND asin=? ORDER BY id DESC LIMIT 1",
            (marketplace, asin),
        ).fetchone()
        media_count = conn.execute("SELECT COUNT(*) FROM media_asset WHERE marketplace=? AND asin=?", (marketplace, asin)).fetchone()[0]
        content_count = conn.execute("SELECT COUNT(*) FROM content_module WHERE marketplace=? AND asin=?", (marketplace, asin)).fetchone()[0]
        status = state["status"] if state is not None else "unknown"
        return {
            "schema_version": API_SCHEMA_VERSION,
            "marketplace": marketplace,
            "asin": asin,
            "retrieved_at": (product["collected_at"] if product is not None else evidence["retrieved_at"] if evidence is not None else None),
            "quality_status": "valid" if status in {"product_done", "succeeded"} else status,
            "source": evidence["source_type"] if evidence is not None else None,
            "product": _dict_row(product),
            "task": _dict_row(state),
            "evidence": _dict_row(evidence),
            "counts": {"media": media_count, "content_modules": content_count},
        }
    finally:
        conn.close()


def load_job_status(db_path: Path) -> dict[str, Any]:
    conn = _read_connection(db_path)
    try:
        rows = conn.execute("SELECT status, COUNT(*) AS count FROM item_state GROUP BY status ORDER BY status").fetchall()
        return {
            "schema_version": API_SCHEMA_VERSION,
            "retrieved_at": _now(),
            "counts": {row["status"]: row["count"] for row in rows},
        }
    finally:
        conn.close()


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
