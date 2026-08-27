"""Storage interfaces used by Collection API.

The first implementation is read-only SQLite. A PostgreSQL implementation can
be added later without changing API routes or response fields.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


class CollectionRepository(Protocol):
    def load_product(self, marketplace: str, asin: str) -> dict[str, Any] | None: ...

    def load_evidence(self, marketplace: str, asin: str, limit: int = 20) -> list[dict[str, Any]]: ...

    def load_job_status(self) -> dict[str, Any]: ...

    def request_refresh(self, marketplace: str, asin: str, requested_by: str, reason: str) -> dict[str, Any]: ...


def _dict_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _freshness(captured_at: Any) -> dict[str, Any]:
    if not captured_at:
        return {"captured_at": None, "age_seconds": None}
    try:
        value = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((datetime.now(timezone.utc) - value).total_seconds()))
    except (TypeError, ValueError):
        age_seconds = None
    return {"captured_at": captured_at, "age_seconds": age_seconds}


class SQLiteCollectionRepository:
    """Read-only-by-convention repository over the local SQLite snapshot."""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def _connection(self, read_only: bool = True) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise FileNotFoundError(f"SQLite database not found: {self.db_path}")
        if read_only:
            uri = f"file:{self.db_path.resolve().as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=2)
        else:
            conn = sqlite3.connect(str(self.db_path), timeout=2)
        conn.row_factory = sqlite3.Row
        return conn

    def load_product(self, marketplace: str, asin: str) -> dict[str, Any] | None:
        conn = self._connection()
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
            retrieved_at = product["collected_at"] if product is not None else evidence["retrieved_at"] if evidence is not None else None
            return {
                "schema_version": "amazon-us-collection-v1",
                "marketplace": marketplace,
                "asin": asin,
                "retrieved_at": retrieved_at,
                "freshness": _freshness(retrieved_at),
                "quality_status": "valid" if status in {"product_done", "succeeded"} else status,
                "source": evidence["source_type"] if evidence is not None else None,
                "product": _dict_row(product),
                "task": _dict_row(state),
                "evidence": _dict_row(evidence),
                "counts": {"media": media_count, "content_modules": content_count},
            }
        finally:
            conn.close()

    def load_job_status(self) -> dict[str, Any]:
        conn = self._connection()
        try:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM item_state GROUP BY status ORDER BY status").fetchall()
            return {
                "schema_version": "amazon-us-collection-v1",
                "retrieved_at": _now(),
                "counts": {row["status"]: row["count"] for row in rows},
            }
        finally:
            conn.close()

    def load_evidence(self, marketplace: str, asin: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        conn = self._connection()
        try:
            rows = conn.execute(
                "SELECT run_id, url, http_status, retrieved_at, source_type, content_hash, raw_html_path, block_reason, parser_version, error_code "
                "FROM collection_evidence WHERE marketplace=? AND asin=? ORDER BY id DESC LIMIT ?",
                (marketplace, asin, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def request_refresh(self, marketplace: str, asin: str, requested_by: str, reason: str) -> dict[str, Any]:
        conn = self._connection(read_only=False)
        try:
            exists = conn.execute("SELECT 1 FROM item_state WHERE marketplace=? AND asin=?", (marketplace, asin)).fetchone()
            if exists is None:
                raise KeyError(f"ASIN not found: {marketplace}/{asin}")
            request = {
                "job_id": f"refresh-{uuid.uuid4().hex}",
                "marketplace": marketplace,
                "asin": asin,
                "requested_by": requested_by or "collection-api",
                "reason": reason or "on_demand",
                "status": "queued",
                "requested_at": _now(),
            }
            conn.execute(
                "INSERT INTO refresh_request(job_id,marketplace,asin,requested_by,reason,status,requested_at) VALUES(?,?,?,?,?,?,?)",
                tuple(request.values()),
            )
            conn.commit()
            return request
        finally:
            conn.close()


class PostgresCollectionRepository:
    """Read-only repository for the PostgreSQL schema in ``schema/``.

    ``psycopg`` is imported only when this repository is used, so the SQLite
    POC remains dependency-free. Tests may inject a DB-API connection factory.
    """

    def __init__(self, dsn: str, connect=None):
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN must not be empty")
        self.dsn = dsn
        self._connect_factory = connect or self._connect

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL backend requires optional dependency psycopg") from exc
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _first(cursor):
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    def load_product(self, marketplace: str, asin: str) -> dict[str, Any] | None:
        with self._connect_factory() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM amazon_us.product_latest WHERE marketplace=%s AND asin=%s "
                    "ORDER BY CASE subject_type WHEN 'own' THEN 0 WHEN 'competitor' THEN 1 ELSE 2 END LIMIT 1",
                    (marketplace, asin),
                )
                product = self._first(cursor)
                cursor.execute(
                    "SELECT * FROM amazon_us.item_state WHERE marketplace=%s AND asin=%s "
                    "ORDER BY CASE subject_type WHEN 'own' THEN 0 WHEN 'competitor' THEN 1 ELSE 2 END LIMIT 1",
                    (marketplace, asin),
                )
                state = self._first(cursor)
                if product is None and state is None:
                    return None
                subject_type = (product or state).get("subject_type", "own")
                cursor.execute(
                    "SELECT run_id, url, http_status, retrieved_at, source_type, content_hash, raw_html_path, block_reason, parser_version, error_code "
                    "FROM amazon_us.collection_evidence WHERE marketplace=%s AND asin=%s AND subject_type=%s "
                    "ORDER BY id DESC LIMIT 1",
                    (marketplace, asin, subject_type),
                )
                evidence = self._first(cursor)
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM amazon_us.media_asset WHERE marketplace=%s AND asin=%s AND subject_type=%s",
                    (marketplace, asin, subject_type),
                )
                media_count = cursor.fetchone()["count"]
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM amazon_us.content_module WHERE marketplace=%s AND asin=%s AND subject_type=%s",
                    (marketplace, asin, subject_type),
                )
                content_count = cursor.fetchone()["count"]
                status = state.get("status") if state is not None else "unknown"
                retrieved_at = (product or {}).get("collected_at") or (evidence or {}).get("retrieved_at")
                return {
                    "schema_version": "amazon-us-collection-v1",
                    "marketplace": marketplace,
                    "asin": asin,
                    "retrieved_at": retrieved_at,
                    "freshness": _freshness(retrieved_at),
                    "quality_status": "valid" if status in {"product_done", "succeeded"} else status,
                    "source": (evidence or {}).get("source_type"),
                    "product": product,
                    "task": state,
                    "evidence": evidence,
                    "counts": {"media": media_count, "content_modules": content_count},
                }

    def load_job_status(self) -> dict[str, Any]:
        with self._connect_factory() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT status, COUNT(*) AS count FROM amazon_us.item_state GROUP BY status ORDER BY status")
                rows = cursor.fetchall()
                return {
                    "schema_version": "amazon-us-collection-v1",
                    "retrieved_at": _now(),
                    "counts": {row["status"]: row["count"] for row in rows},
                }

    def load_evidence(self, marketplace: str, asin: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._connect_factory() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT run_id, url, http_status, retrieved_at, source_type, content_hash, raw_html_path, block_reason, parser_version, error_code "
                    "FROM amazon_us.collection_evidence WHERE marketplace=%s AND asin=%s ORDER BY id DESC LIMIT %s",
                    (marketplace, asin, limit),
                )
                return [dict(row) for row in cursor.fetchall()]

    def request_refresh(self, marketplace: str, asin: str, requested_by: str, reason: str) -> dict[str, Any]:
        request = {
            "job_id": f"refresh-{uuid.uuid4().hex}",
            "marketplace": marketplace,
            "asin": asin,
            "requested_by": requested_by or "collection-api",
            "reason": reason or "on_demand",
            "status": "queued",
        }
        with self._connect_factory() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM amazon_us.item_state WHERE marketplace=%s AND asin=%s LIMIT 1",
                    (marketplace, asin),
                )
                if cursor.fetchone() is None:
                    raise KeyError(f"ASIN not found: {marketplace}/{asin}")
                cursor.execute(
                    "INSERT INTO amazon_us.refresh_request(job_id,marketplace,asin,requested_by,reason,status) VALUES(%s,%s,%s,%s,%s,%s)",
                    (request["job_id"], marketplace, asin, request["requested_by"], request["reason"], request["status"]),
                )
            conn.commit()
        request["requested_at"] = _now()
        return request
