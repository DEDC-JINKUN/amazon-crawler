#!/usr/bin/env python3
"""Loopback-only, read-only operations console for one PostgreSQL tenant."""
from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import re
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "console"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
ITEM_PATH = re.compile(r"^/api/items/([A-Za-z0-9]{10})$")
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
}
SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


class PostgresConsoleRepository:
    """Purpose-built, read-only query surface for the local operations UI."""

    def __init__(self, dsn: str, tenant_id: str):
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN must not be empty")
        if not tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        self.dsn = dsn
        self.tenant_id = tenant_id.strip()

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL console requires optional dependency psycopg") from exc
        return psycopg.connect(
            self.dsn,
            row_factory=dict_row,
            options="-c default_transaction_read_only=on",
        )

    @staticmethod
    def _counts(cursor, sql: str, params: tuple[Any, ...]) -> dict[str, int]:
        cursor.execute(sql, params)
        return {str(row["key"]): int(row["count"]) for row in cursor.fetchall()}

    def load_overview(self, raw_html_dir: Path | None = None) -> dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cursor:
            params = (self.tenant_id,)
            status_counts = self._counts(
                cursor,
                "SELECT status AS key,COUNT(*) AS count FROM amazon_us.item_state "
                "WHERE tenant_id=%s GROUP BY status ORDER BY status",
                params,
            )
            stage_counts = self._counts(
                cursor,
                "SELECT task_stage AS key,COUNT(*) AS count FROM amazon_us.item_state "
                "WHERE tenant_id=%s GROUP BY task_stage ORDER BY task_stage",
                params,
            )
            source_counts = self._counts(
                cursor,
                "SELECT COALESCE(source_type,'unknown') AS key,COUNT(*) AS count "
                "FROM amazon_us.collection_evidence WHERE tenant_id=%s GROUP BY source_type ORDER BY source_type",
                params,
            )
            error_counts = self._counts(
                cursor,
                "SELECT COALESCE(error_code,'none') AS key,COUNT(*) AS count "
                "FROM amazon_us.collection_evidence WHERE tenant_id=%s GROUP BY error_code ORDER BY error_code",
                params,
            )
            block_counts = self._counts(
                cursor,
                "SELECT COALESCE(block_reason,'none') AS key,COUNT(*) AS count "
                "FROM amazon_us.collection_evidence WHERE tenant_id=%s GROUP BY block_reason ORDER BY block_reason",
                params,
            )
            cursor.execute(
                "SELECT COUNT(*) AS total FROM amazon_us.item_state WHERE tenant_id=%s",
                params,
            )
            total = int(cursor.fetchone()["total"])
            cursor.execute(
                "SELECT COUNT(DISTINCT asin) AS touched,COUNT(*) AS actions,COUNT(transfer_bytes) AS known_transfer_records,"
                "COALESCE(SUM(transfer_bytes),0) AS transfer_bytes,MAX(retrieved_at) AS last_evidence_at "
                "FROM amazon_us.collection_evidence WHERE tenant_id=%s",
                params,
            )
            evidence = dict(cursor.fetchone())
            table_counts: dict[str, int] = {}
            for name, table in (
                ("products", "product_latest"),
                ("media", "media_asset"),
                ("content", "content_module"),
                ("review_summaries", "review_summary"),
                ("reviews", "review_record"),
            ):
                cursor.execute(f"SELECT COUNT(*) AS count FROM amazon_us.{table} WHERE tenant_id=%s", params)
                table_counts[name] = int(cursor.fetchone()["count"])
            cursor.execute(
                "SELECT asin,task_stage,lease_owner,lease_expires_at FROM amazon_us.item_state "
                "WHERE tenant_id=%s AND status='running' ORDER BY asin LIMIT 50",
                params,
            )
            running = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT run_id,COUNT(*) AS actions,MIN(retrieved_at) AS started_at,MAX(retrieved_at) AS ended_at,"
                "COUNT(*) FILTER (WHERE block_reason IS NOT NULL) AS blocked "
                "FROM amazon_us.collection_evidence WHERE tenant_id=%s GROUP BY run_id "
                "ORDER BY MAX(retrieved_at) DESC LIMIT 10",
                params,
            )
            recent_runs = [dict(row) for row in cursor.fetchall()]

        files = list(raw_html_dir.rglob("*.html")) if raw_html_dir and raw_html_dir.exists() else []
        raw_bytes = sum(path.stat().st_size for path in files)
        touched = int(evidence.get("touched") or 0)
        actions = int(evidence.get("actions") or 0)
        known_transfers = int(evidence.get("known_transfer_records") or 0)
        database_rows = sum(table_counts.values())
        return {
            "schema_version": "amazon-us-console-v1",
            "tenant_id": self.tenant_id,
            "observed_at": datetime.now(timezone.utc),
            "status_counts": status_counts,
            "stage_counts": stage_counts,
            "source_counts": source_counts,
            "error_counts": error_counts,
            "block_counts": block_counts,
            "progress": {
                "total": total,
                "touched": touched,
                "percent": round(touched / total * 100, 2) if total else 0,
                "successful_products": table_counts["products"],
            },
            "table_counts": table_counts,
            "running": running,
            "recent_runs": recent_runs,
            "four_scale_metrics": {
                "page_actions": actions,
                "successful_asins": table_counts["products"],
                "database_rows": database_rows,
                "field_values": None,
                "field_values_reason": "business definition required",
            },
            "traffic": {
                "raw_html_files": len(files),
                "saved_raw_html_bytes": raw_bytes,
                "known_http_transfer_bytes": int(evidence.get("transfer_bytes") or 0),
                "unknown_transfer_records": actions - known_transfers,
                "proxy_billed_bytes": None,
            },
            "last_evidence_at": evidence.get("last_evidence_at"),
        }

    def list_items(
        self,
        *,
        status: str | None = None,
        stage: str | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        where = ["s.tenant_id=%s"]
        params: list[Any] = [self.tenant_id]
        if status:
            where.append("s.status=%s")
            params.append(status)
        if stage:
            where.append("s.task_stage=%s")
            params.append(stage)
        if query:
            term = f"%{query.strip()}%"
            where.append("(s.asin ILIKE %s OR COALESCE(p.title,'') ILIKE %s OR COALESCE(s.last_error,'') ILIKE %s)")
            params.extend([term, term, term])
        predicate = " AND ".join(where)
        join = (
            "LEFT JOIN amazon_us.product_latest p ON p.tenant_id=s.tenant_id AND p.marketplace=s.marketplace "
            "AND p.asin=s.asin AND p.subject_type=s.subject_type "
        )
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) AS count FROM amazon_us.item_state s {join} WHERE {predicate}", tuple(params))
            total = int(cursor.fetchone()["count"])
            cursor.execute(
                f"""
                SELECT s.asin,s.subject_type,s.status,s.task_stage,s.attempts,s.max_attempts,s.last_error,
                       s.block_reason,s.updated_at,s.lease_owner,s.lease_expires_at,
                       p.title,p.price,p.availability,p.rating,p.reported_review_count,
                       e.source_type,e.http_status,e.error_code AS evidence_error,e.block_reason AS evidence_block,e.retrieved_at
                FROM amazon_us.item_state s
                {join}
                LEFT JOIN LATERAL (
                  SELECT source_type,http_status,error_code,block_reason,retrieved_at
                  FROM amazon_us.collection_evidence ce
                  WHERE ce.tenant_id=s.tenant_id AND ce.marketplace=s.marketplace AND ce.asin=s.asin
                    AND ce.subject_type=s.subject_type
                  ORDER BY ce.id DESC LIMIT 1
                ) e ON TRUE
                WHERE {predicate}
                ORDER BY CASE s.status WHEN 'blocked' THEN 0 WHEN 'failed' THEN 1 WHEN 'running' THEN 2 ELSE 3 END,
                         s.updated_at DESC,s.asin
                LIMIT %s OFFSET %s
                """,
                tuple([*params, limit, offset]),
            )
            items = [dict(row) for row in cursor.fetchall()]
        return {"total": total, "limit": limit, "offset": offset, "items": items}

    def load_detail(self, asin: str) -> dict[str, Any] | None:
        asin = asin.upper()
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM amazon_us.item_state WHERE tenant_id=%s AND marketplace='US' AND asin=%s "
                "ORDER BY CASE subject_type WHEN 'own' THEN 0 WHEN 'competitor' THEN 1 ELSE 2 END LIMIT 1",
                (self.tenant_id, asin),
            )
            state = cursor.fetchone()
            if state is None:
                return None
            task = dict(state)
            subject_type = task["subject_type"]
            identity = (self.tenant_id, asin, subject_type)
            cursor.execute(
                "SELECT * FROM amazon_us.product_latest WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s",
                identity,
            )
            product_row = cursor.fetchone()
            product = dict(product_row) if product_row is not None else None
            cursor.execute(
                "SELECT placement,entry_type,thumbnail_url,display_url,asset_url,poster_url,ordinal,is_primary,"
                "width,height,alt_text,variant_asin,load_status,failure_reason FROM amazon_us.media_asset "
                "WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s ORDER BY ordinal NULLS LAST LIMIT 300",
                identity,
            )
            media = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT module_type,position,order_index,text,image_url,link_url,status FROM amazon_us.content_module "
                "WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s "
                "ORDER BY position,order_index NULLS LAST LIMIT 300",
                identity,
            )
            content = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT * FROM amazon_us.review_summary WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s",
                identity,
            )
            summary_row = cursor.fetchone()
            review_summary = dict(summary_row) if summary_row is not None else None
            cursor.execute(
                "SELECT review_id,rating,title,body,review_url,review_date,locale,verified,body_truncated,review_images,page "
                "FROM amazon_us.review_record WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s "
                "ORDER BY page NULLS LAST,review_id LIMIT 100",
                identity,
            )
            reviews = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT run_id,url,http_status,transfer_bytes,retrieved_at,source_type,content_hash,raw_html_path,"
                "block_reason,parser_version,error_code,context_json FROM amazon_us.collection_evidence "
                "WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s ORDER BY id DESC LIMIT 30",
                identity,
            )
            evidence = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT snapshot_id,collected_at,price,availability,rating,review_count,status FROM amazon_us.product_snapshot "
                "WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s "
                "ORDER BY collected_at DESC,snapshot_id DESC LIMIT 30",
                identity,
            )
            history = [dict(row) for row in cursor.fetchall()]
        return {
            "schema_version": "amazon-us-console-v1",
            "tenant_id": self.tenant_id,
            "asin": asin,
            "task": task,
            "product": product,
            "media": media,
            "content_modules": content,
            "review_summary": review_summary,
            "reviews": reviews,
            "top_reviews": (product or {}).get("top_reviews") or [],
            "evidence": evidence,
            "history": history,
        }


class ConsoleHandler(BaseHTTPRequestHandler):
    server: "ConsoleServer"

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_default).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _authorized(self) -> bool:
        expected = self.server.api_key
        if not expected:
            return True
        supplied = self.headers.get("X-Collection-API-Key", "")
        if hmac.compare_digest(supplied, expected):
            return True
        self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def _send_static(self, path: str) -> None:
        name = STATIC_FILES.get(path)
        if name is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})
            return
        file_path = STATIC_ROOT / name
        try:
            body = file_path.read_bytes()
        except OSError:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "console_asset_unavailable"})
            return
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        if file_path.suffix == ".js":
            content_type = "text/javascript"
        self._send_bytes(HTTPStatus.OK, body, f"{content_type}; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        if path in STATIC_FILES:
            self._send_static(path)
            return
        if path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True, "schema_version": "amazon-us-console-v1"})
            return
        if not path.startswith("/api/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})
            return
        if not self._authorized():
            return
        try:
            if path == "/api/overview":
                self._send_json(HTTPStatus.OK, self.server.repository.load_overview(self.server.raw_html_dir))
                return
            if path == "/api/items":
                query = parse_qs(parsed.query)
                status = query.get("status", [""])[0].strip() or None
                stage = query.get("stage", [""])[0].strip() or None
                term = query.get("q", [""])[0].strip()[:100] or None
                limit = int(query.get("limit", ["100"])[0])
                offset = int(query.get("offset", ["0"])[0])
                if status and not re.fullmatch(r"[a-z_]+", status):
                    raise ValueError("invalid status")
                if stage and stage not in {"product", "reviews", "complete"}:
                    raise ValueError("invalid stage")
                payload = self.server.repository.list_items(
                    status=status, stage=stage, query=term, limit=limit, offset=offset
                )
                self._send_json(HTTPStatus.OK, payload)
                return
            match = ITEM_PATH.fullmatch(path)
            if match:
                asin = match.group(1).upper()
                if not ASIN_RE.fullmatch(asin):
                    raise ValueError("invalid ASIN")
                payload = self.server.repository.load_detail(asin)
                if payload is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "asin_not_found", "asin": asin})
                    return
                self._send_json(HTTPStatus.OK, payload)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "detail": str(exc)})
        except Exception:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable"})

    def do_POST(self) -> None:  # noqa: N802
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only_console"})

    def do_PUT(self) -> None:  # noqa: N802
        self.do_POST()

    def do_DELETE(self) -> None:  # noqa: N802
        self.do_POST()

    def log_message(self, format: str, *args: Any) -> None:
        if self.server.access_log:
            super().log_message(format, *args)


class ConsoleServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        repository: Any,
        *,
        raw_html_dir: Path | None = None,
        api_key: str = "",
        access_log: bool = False,
    ):
        if address[0] not in LOOPBACK_HOSTS:
            raise ValueError("console only allows loopback host")
        super().__init__(address, ConsoleHandler)
        self.repository = repository
        self.raw_html_dir = raw_html_dir
        self.api_key = api_key
        self.access_log = access_log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--raw-html-dir", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--api-key-env", default="AMAZON_COLLECTION_API_KEY")
    parser.add_argument("--require-api-key", action="store_true")
    parser.add_argument("--access-log", action="store_true")
    args = parser.parse_args(argv)
    try:
        dsn = os.environ.get(args.dsn_env, "").strip()
        if not dsn:
            raise ValueError(f"PostgreSQL DSN environment variable is required: {args.dsn_env}")
        api_key = os.environ.get(args.api_key_env, "")
        if args.require_api_key and not api_key:
            raise ValueError(f"required API key environment variable is missing: {args.api_key_env}")
        repository = PostgresConsoleRepository(dsn, args.tenant_id)
        server = ConsoleServer(
            (args.host, args.port),
            repository,
            raw_html_dir=args.raw_html_dir,
            api_key=api_key,
            access_log=args.access_log,
        )
        print(f"Amazon Collection Console: http://{args.host}:{server.server_port}")
        print(f"Tenant: {args.tenant_id} | read-only | refresh: 5s")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
