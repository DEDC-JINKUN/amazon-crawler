"""Tenant-scoped read and bounded-refresh storage used by Collection API."""
from __future__ import annotations

import sqlite3
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

class CampaignConflictError(ValueError):
    pass


def _context_dict(evidence: dict[str, Any] | None) -> dict[str, Any]:
    value = (evidence or {}).get("context_json") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return value if isinstance(value, dict) else {}


def _is_variant_resolution(evidence: dict[str, Any] | None) -> bool:
    if (evidence or {}).get("error_code") != "asin_mismatch":
        return False
    identity = _context_dict(evidence).get("identity") or {}
    if not isinstance(identity, dict) or identity.get("canonical_valid_amazon") is not True:
        return False
    requested = str(identity.get("requested_asin") or "").upper()
    observed = str(identity.get("observed_asin") or "").upper()
    canonical = str(identity.get("canonical_asin") or "").upper()
    parent = str(identity.get("parent_asin") or "").upper()
    children = {str(item).upper() for item in identity.get("child_asins") or []}
    return bool(
        ASIN_RE.fullmatch(requested)
        and ASIN_RE.fullmatch(observed)
        and ASIN_RE.fullmatch(parent)
        and requested != observed
        and canonical == observed
        and requested in children
        and observed in children
    )


def _quality_status(status: str, product: dict[str, Any] | None, evidence: dict[str, Any] | None) -> str:
    if _is_variant_resolution(evidence):
        return "variant_redirect"
    return "valid" if product is not None and status in {"product_done", "succeeded"} else status


class CollectionRepository(Protocol):
    def load_product(self, marketplace: str, asin: str) -> dict[str, Any] | None: ...

    def load_history(self, marketplace: str, asin: str, limit: int = 20) -> list[dict[str, Any]]: ...

    def load_evidence(self, marketplace: str, asin: str, limit: int = 20) -> list[dict[str, Any]]: ...

    def load_job_status(self) -> dict[str, Any]: ...

    def load_refresh_request(self, job_id: str) -> dict[str, Any] | None: ...

    def request_refresh(self, marketplace: str, asin: str, requested_by: str, reason: str) -> dict[str, Any]: ...

    def request_refresh_batch(self, marketplace: str, asins: list[str], requested_by: str, reason: str) -> list[dict[str, Any]]: ...

    def record_api_audit(self, *, agent_id: str, action: str, resource: str, outcome: str) -> None: ...


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

    @staticmethod
    def _evidence_select(conn: sqlite3.Connection) -> str:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(collection_evidence)")}
        base = "run_id, url, http_status"
        if "transfer_bytes" in columns:
            base += ", transfer_bytes"
        base += ", retrieved_at, source_type, content_hash, raw_html_path, block_reason, parser_version, error_code"
        return base + (", context_json" if "context_json" in columns else "")

    @staticmethod
    def _with_legacy_context(row: sqlite3.Row | None) -> dict[str, Any] | None:
        value = _dict_row(row)
        if value is not None:
            value.setdefault("context_json", None)
        return value

    def load_product(self, marketplace: str, asin: str) -> dict[str, Any] | None:
        conn = self._connection()
        try:
            product = conn.execute("SELECT * FROM product_snapshot WHERE marketplace=? AND asin=?", (marketplace, asin)).fetchone()
            state = conn.execute("SELECT * FROM item_state WHERE marketplace=? AND asin=?", (marketplace, asin)).fetchone()
            if product is None and state is None:
                return None
            evidence = conn.execute(f"SELECT {self._evidence_select(conn)} FROM collection_evidence WHERE marketplace=? AND asin=? ORDER BY id DESC LIMIT 1", (marketplace, asin)).fetchone()
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
                "quality_status": _quality_status(status, _dict_row(product), self._with_legacy_context(evidence)),
                "source": evidence["source_type"] if evidence is not None else None,
                "product": _dict_row(product),
                "task": _dict_row(state),
                "evidence": self._with_legacy_context(evidence),
                "counts": {"media": media_count, "content_modules": content_count},
            }
        finally:
            conn.close()

    def load_job_status(self) -> dict[str, Any]:
        conn = self._connection()
        try:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM item_state GROUP BY status ORDER BY status").fetchall()
            refresh_rows = conn.execute("SELECT status, COUNT(*) AS count FROM refresh_request GROUP BY status ORDER BY status").fetchall()
            return {
                "schema_version": "amazon-us-collection-v1",
                "retrieved_at": _now(),
                "counts": {row["status"]: row["count"] for row in rows},
                "refresh_requests": {row["status"]: row["count"] for row in refresh_rows},
            }
        finally:
            conn.close()

    def load_evidence(self, marketplace: str, asin: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        conn = self._connection()
        try:
            rows = conn.execute(f"SELECT {self._evidence_select(conn)} FROM collection_evidence WHERE marketplace=? AND asin=? ORDER BY id DESC LIMIT ?", (marketplace, asin, limit)).fetchall()
            return [self._with_legacy_context(row) for row in rows]
        finally:
            conn.close()

    def load_history(self, marketplace: str, asin: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        conn = self._connection()
        try:
            rows = conn.execute(
                "SELECT id AS snapshot_id, captured_at, source_type, raw_html_path, value_json "
                "FROM product_snapshot_history WHERE marketplace=? AND asin=? ORDER BY id DESC LIMIT ?",
                (marketplace, asin, limit),
            ).fetchall()
            items = []
            for row in rows:
                item = {"snapshot_id": row["snapshot_id"], "captured_at": row["captured_at"], "source": row["source_type"], "raw_html_path": row["raw_html_path"]}
                try:
                    item["data"] = json.loads(row["value_json"])
                except (TypeError, ValueError):
                    item["data"] = None
                items.append(item)
            return items
        finally:
            conn.close()

    def request_refresh(self, marketplace: str, asin: str, requested_by: str, reason: str) -> dict[str, Any]:
        return self.request_refresh_batch(marketplace, [asin], requested_by, reason)[0]

    def request_refresh_batch(self, marketplace: str, asins: list[str], requested_by: str, reason: str) -> list[dict[str, Any]]:
        normalized = list(dict.fromkeys(asins))
        if not 1 <= len(normalized) <= 5:
            raise ValueError("refresh batch requires 1 to 5 ASINs")
        conn = self._connection(read_only=False)
        try:
            missing = [asin for asin in normalized if conn.execute(
                "SELECT 1 FROM item_state WHERE marketplace=? AND asin=?", (marketplace, asin)
            ).fetchone() is None]
            if missing:
                raise KeyError(f"ASIN not found: {marketplace}/{','.join(missing)}")
            requests = []
            for asin in normalized:
                active = conn.execute(
                    "SELECT * FROM refresh_request WHERE marketplace=? AND asin=? AND status IN ('queued','claimed') ORDER BY requested_at DESC LIMIT 1",
                    (marketplace, asin),
                ).fetchone()
                if active is not None:
                    requests.append(_dict_row(active) or {})
                    continue
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
                requests.append(request)
            conn.commit()
            return requests
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def load_refresh_request(self, job_id: str) -> dict[str, Any] | None:
        conn = self._connection()
        try:
            row = conn.execute("SELECT * FROM refresh_request WHERE job_id=?", (job_id,)).fetchone()
            return _dict_row(row)
        finally:
            conn.close()

    def record_api_audit(self, *, agent_id: str, action: str, resource: str, outcome: str) -> None:
        """Keep local test audits durable without storing credentials or bodies."""
        conn = self._connection(read_only=False)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS collection_api_audit ("
                "id INTEGER PRIMARY KEY,recorded_at TEXT NOT NULL,agent_id TEXT NOT NULL,"
                "action TEXT NOT NULL,resource TEXT NOT NULL,outcome TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO collection_api_audit(recorded_at,agent_id,action,resource,outcome) VALUES(?,?,?,?,?)",
                (_now(), agent_id[:120], action[:80], resource[:240], outcome[:80]),
            )
            conn.commit()
        finally:
            conn.close()


class PostgresCollectionRepository:
    """Tenant-scoped query and refresh repository for PostgreSQL.

    ``psycopg`` is imported only when this repository is used, so the SQLite
    POC remains dependency-free. Tests may inject a DB-API connection factory.
    """

    def __init__(self, dsn: str, connect=None, tenant_id: str = "default", subject_type: str = "own"):
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN must not be empty")
        if not tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        self.dsn = dsn
        self.tenant_id = tenant_id.strip()
        if subject_type not in {"own","competitor","candidate"}: raise ValueError("invalid subject_type")
        self.subject_type=subject_type
        self._connect_factory = connect or self._connect

    def load_recovery_status(self):
        from recovery_scheduler import read_projection
        return read_projection(self._connect_factory, self.tenant_id)

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

    def load_schema_contract(self) -> dict[str, list[str]]:
        """Return required schema columns present in the PostgreSQL backend."""
        required = {
            "item_state": {"next_retry_at", "lease_token", "lease_owner", "lease_expires_at"},
            "collection_evidence": {"context_json", "transfer_bytes"},
        }
        with self._connect_factory() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name IN (%s,%s)",
                    ("amazon_us", "item_state", "collection_evidence"),
                )
                present: dict[str, set[str]] = {table: set() for table in required}
                for row in cursor.fetchall():
                    table = row["table_name"] if isinstance(row, dict) else row[0]
                    column = row["column_name"] if isinstance(row, dict) else row[1]
                    present.setdefault(table, set()).add(column)
        return {table: sorted(required_columns & present.get(table, set())) for table, required_columns in required.items()}

    def load_campaign_contract(self) -> bool:
        with self._connect_factory() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT to_regclass('amazon_us.collection_campaign') IS NOT NULL AND to_regclass('amazon_us.collection_campaign_member') IS NOT NULL AS ready")
            return bool(cursor.fetchone()["ready"])

    def load_product(self, marketplace: str, asin: str) -> dict[str, Any] | None:
        with self._connect_factory() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM amazon_us.product_latest WHERE tenant_id=%s AND marketplace=%s AND asin=%s "
                    "ORDER BY CASE subject_type WHEN 'own' THEN 0 WHEN 'competitor' THEN 1 ELSE 2 END LIMIT 1",
                    (self.tenant_id, marketplace, asin),
                )
                product = self._first(cursor)
                cursor.execute(
                    "SELECT * FROM amazon_us.item_state WHERE tenant_id=%s AND marketplace=%s AND asin=%s "
                    "ORDER BY CASE subject_type WHEN 'own' THEN 0 WHEN 'competitor' THEN 1 ELSE 2 END LIMIT 1",
                    (self.tenant_id, marketplace, asin),
                )
                state = self._first(cursor)
                if product is None and state is None:
                    return None
                subject_type = (product or state).get("subject_type", "own")
                cursor.execute(
                    "SELECT run_id, url, http_status, transfer_bytes, retrieved_at, source_type, content_hash, raw_html_path, block_reason, parser_version, error_code, context_json "
                    "FROM amazon_us.collection_evidence WHERE tenant_id=%s AND marketplace=%s AND asin=%s AND subject_type=%s "
                    "ORDER BY id DESC LIMIT 1",
                    (self.tenant_id, marketplace, asin, subject_type),
                )
                evidence = self._first(cursor)
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM amazon_us.media_asset WHERE tenant_id=%s AND marketplace=%s AND asin=%s AND subject_type=%s",
                    (self.tenant_id, marketplace, asin, subject_type),
                )
                media_count = cursor.fetchone()["count"]
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM amazon_us.content_module WHERE tenant_id=%s AND marketplace=%s AND asin=%s AND subject_type=%s",
                    (self.tenant_id, marketplace, asin, subject_type),
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
                    "quality_status": _quality_status(status, product, evidence),
                    "source": (evidence or {}).get("source_type"),
                    "product": product,
                    "task": state,
                    "evidence": evidence,
                    "counts": {"media": media_count, "content_modules": content_count},
                }

    def load_job_status(self) -> dict[str, Any]:
        with self._connect_factory() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT status, COUNT(*) AS count FROM amazon_us.item_state WHERE tenant_id=%s GROUP BY status ORDER BY status", (self.tenant_id,))
                rows = cursor.fetchall()
                cursor.execute("SELECT status, COUNT(*) AS count FROM amazon_us.refresh_request WHERE tenant_id=%s GROUP BY status ORDER BY status", (self.tenant_id,))
                refresh_rows = cursor.fetchall()
                return {
                    "schema_version": "amazon-us-collection-v1",
                    "retrieved_at": _now(),
                    "counts": {row["status"]: row["count"] for row in rows},
                    "refresh_requests": {row["status"]: row["count"] for row in refresh_rows},
                }

    def load_evidence(self, marketplace: str, asin: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._connect_factory() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT run_id, url, http_status, transfer_bytes, retrieved_at, source_type, content_hash, raw_html_path, block_reason, parser_version, error_code, context_json "
                    "FROM amazon_us.collection_evidence WHERE tenant_id=%s AND marketplace=%s AND asin=%s ORDER BY id DESC LIMIT %s",
                    (self.tenant_id, marketplace, asin, limit),
                )
                return [dict(row) for row in cursor.fetchall()]

    def load_history(self, marketplace: str, asin: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._connect_factory() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT snapshot_id, collected_at AS captured_at, subject_type, price, availability, rating, review_count, status "
                    "FROM amazon_us.product_snapshot WHERE tenant_id=%s AND marketplace=%s AND asin=%s ORDER BY collected_at DESC, snapshot_id DESC LIMIT %s",
                    (self.tenant_id, marketplace, asin, limit),
                )
                return [dict(row) for row in cursor.fetchall()]

    def request_refresh(self, marketplace: str, asin: str, requested_by: str, reason: str) -> dict[str, Any]:
        return self.request_refresh_batch(marketplace, [asin], requested_by, reason)[0]

    def _reset_successful_terminal_recovery(self, cursor, marketplace: str, asin: str, subject_type: str) -> bool:
        """Open one fresh bounded job only after a prior successful terminal outcome."""
        cursor.execute(
            "UPDATE amazon_us.recovery_job SET attempts=0,request_count=0,browser_request_count=0,"
            "relay_payload_bytes=0,known_bytes=0,unknown_byte_attempts=0,deadline=CURRENT_TIMESTAMP+INTERVAL '24 hours',"
            "next_retry_at=NULL,outcome='new',terminal=false,lease_token=NULL,updated_at=CURRENT_TIMESTAMP "
            "WHERE tenant_id=%s AND asin=%s AND subject_type=%s AND stage='product' "
            "AND terminal AND outcome IN ('completed','partial','variant') RETURNING asin",
            (self.tenant_id, asin, subject_type),
        )
        if cursor.fetchone() is None:
            return False
        cursor.execute(
            "UPDATE amazon_us.item_state SET next_retry_at=NULL WHERE tenant_id=%s AND marketplace=%s AND asin=%s AND subject_type=%s",
            (self.tenant_id, marketplace, asin, subject_type),
        )
        cursor.execute(
            "INSERT INTO amazon_us.state_history(tenant_id,marketplace,asin,subject_type,from_status,to_status,reason) "
            "SELECT tenant_id,marketplace,asin,subject_type,status,status,'explicit_refresh_new_logical_job' "
            "FROM amazon_us.item_state WHERE tenant_id=%s AND marketplace=%s AND asin=%s AND subject_type=%s",
            (self.tenant_id, marketplace, asin, subject_type),
        )
        return True

    def submit_campaign(
        self, *, asins: list[str], idempotency_key: str, requested_by: str, subject_type: str = "own",
        priority: int = 50, freshness_seconds: int | None = None,
        fields: list[str] | None = None, deadline_seconds: int = 86400,
        max_members: int = 5000,
    ) -> dict[str, Any]:
        normalized = list(dict.fromkeys(str(value).strip().upper() for value in asins))
        if not normalized or len(normalized) > int(max_members) or any(not ASIN_RE.fullmatch(value) for value in normalized):
            raise ValueError("campaign membership is invalid or exceeds configured quota")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,160}", str(idempotency_key or "")):
            raise ValueError("invalid campaign idempotency key")
        priority = int(priority)
        deadline_seconds = int(deadline_seconds)
        if not 0 <= priority <= 100 or not 60 <= deadline_seconds <= 604800:
            raise ValueError("invalid campaign priority or deadline")
        requested_fields = sorted(set(str(value)[:40] for value in (fields or [])))
        if subject_type!=self.subject_type: raise ValueError("campaign subject_type does not match worker scope")
        request_document = {"asins": normalized, "subject_type":subject_type, "priority": priority, "freshness_seconds": freshness_seconds, "fields": requested_fields, "deadline_seconds": deadline_seconds}
        request_hash = hashlib.sha256(json.dumps(request_document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        campaign_id = "campaign-" + uuid.uuid5(uuid.NAMESPACE_URL, f"{self.tenant_id}:{idempotency_key}").hex
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT campaign_id,request_hash FROM amazon_us.collection_campaign WHERE tenant_id=%s AND idempotency_key=%s FOR UPDATE", (self.tenant_id,idempotency_key))
                    existing = cursor.fetchone()
                    if existing is not None:
                        row = dict(existing)
                        if row["request_hash"] != request_hash:
                            raise CampaignConflictError("campaign idempotency key conflicts with another request")
                        conn.commit()
                        return self.load_campaign(row["campaign_id"])
                    cursor.execute("INSERT INTO amazon_us.collection_campaign(tenant_id,campaign_id,idempotency_key,request_hash,subject_type,priority,requested_fields,freshness_seconds,deadline,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,CURRENT_TIMESTAMP+(%s*INTERVAL '1 second'),%s) ON CONFLICT (tenant_id,idempotency_key) DO NOTHING RETURNING campaign_id", (self.tenant_id,campaign_id,idempotency_key,request_hash,subject_type,priority,json.dumps(requested_fields),freshness_seconds,deadline_seconds,requested_by))
                    if cursor.fetchone() is None:
                        cursor.execute("SELECT campaign_id,request_hash FROM amazon_us.collection_campaign WHERE tenant_id=%s AND idempotency_key=%s",(self.tenant_id,idempotency_key)); row=dict(cursor.fetchone())
                        conn.commit()
                        if row["request_hash"]!=request_hash: raise CampaignConflictError("campaign idempotency key conflicts with another request")
                        return self.load_campaign(row["campaign_id"])
                    subjects = {}
                    cursor.execute("SELECT asin,subject_type FROM amazon_us.item_state WHERE tenant_id=%s AND marketplace='US' AND subject_type=%s AND asin=ANY(%s)", (self.tenant_id,subject_type,normalized))
                    for row in cursor.fetchall():
                        value=dict(row); subjects[value["asin"]]=value["subject_type"]
                    missing=[asin for asin in normalized if asin not in subjects]
                    if missing: raise KeyError("campaign contains unregistered ASINs: "+",".join(missing))
                    for ordinal,asin in enumerate(normalized):
                        subject=subjects[asin]
                        proposed="refresh-"+uuid.uuid5(uuid.NAMESPACE_URL,f"{campaign_id}:{asin}:{subject}").hex
                        cursor.execute("INSERT INTO amazon_us.refresh_request(job_id,tenant_id,marketplace,asin,subject_type,requested_by,reason,status) VALUES(%s,%s,'US',%s,%s,%s,%s,'queued') ON CONFLICT (tenant_id,marketplace,asin,subject_type) WHERE status IN ('queued','claimed') DO UPDATE SET job_id=amazon_us.refresh_request.job_id RETURNING job_id", (proposed,self.tenant_id,asin,subject,requested_by,f"campaign:{campaign_id}"))
                        job_id=str(cursor.fetchone()["job_id"])
                        if job_id == proposed:
                            self._reset_successful_terminal_recovery(cursor, "US", asin, subject)
                        cursor.execute("INSERT INTO amazon_us.collection_campaign_member(tenant_id,campaign_id,asin,subject_type,refresh_job_id,ordinal) VALUES(%s,%s,%s,%s,%s,%s)", (self.tenant_id,campaign_id,asin,subject,job_id,ordinal))
                conn.commit()
            except Exception:
                conn.rollback(); raise
        return self.load_campaign(campaign_id)

    def load_campaign(self, campaign_id: str) -> dict[str, Any]:
        with self._connect_factory() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT *,CURRENT_TIMESTAMP AS observed_at FROM amazon_us.collection_campaign WHERE tenant_id=%s AND campaign_id=%s FOR UPDATE", (self.tenant_id,campaign_id))
            parent=self._first(cursor)
            if parent is None: return None
            if parent["status"]=='active' and parent["deadline"]<=parent["observed_at"]:
                cursor.execute("UPDATE amazon_us.collection_campaign SET status='expired',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=%s AND campaign_id=%s",(self.tenant_id,campaign_id)); parent["status"]='expired'
                self._cancel_unshared_campaign_jobs(cursor,campaign_id)
            cursor.execute("""SELECT m.asin,m.ordinal,m.refresh_job_id,r.status AS job_status,r.requested_at,r.claimed_at,r.completed_at,s.status AS item_status,s.next_retry_at,j.outcome AS recovery_outcome,j.terminal AS recovery_terminal
                              FROM amazon_us.collection_campaign_member m
                              LEFT JOIN amazon_us.refresh_request r ON r.tenant_id=m.tenant_id AND r.job_id=m.refresh_job_id
                              LEFT JOIN amazon_us.item_state s ON s.tenant_id=m.tenant_id AND s.marketplace='US' AND s.asin=m.asin AND s.subject_type=m.subject_type
                              LEFT JOIN amazon_us.recovery_job j ON j.tenant_id=m.tenant_id AND j.asin=m.asin AND j.subject_type=m.subject_type AND j.stage='product'
                              WHERE m.tenant_id=%s AND m.campaign_id=%s ORDER BY m.ordinal""", (self.tenant_id,campaign_id))
            members=[]; counts={name:0 for name in ("pending","running","deferred","succeeded","variant","failed","cancelled")}
            for raw in cursor.fetchall():
                row=dict(raw); status=str(row.get("job_status") or "queued")
                if status=="claimed": projected="running"
                elif status=="completed": projected="variant" if row.get("recovery_outcome")=="variant" else "succeeded"
                elif status in {"failed","cancelled"}: projected=status
                elif row.get("next_retry_at") is not None: projected="deferred"
                else: projected="pending"
                row["status"]=projected; counts[projected]+=1; members.append(row)
            total=len(members); terminal=counts["succeeded"]+counts["variant"]+counts["failed"]+counts["cancelled"]
            public={key:(value.isoformat() if hasattr(value,"isoformat") else value) for key,value in parent.items() if key not in {"request_hash","idempotency_key"}}
            if parent["status"]=='active' and total and terminal==total:
                cursor.execute("UPDATE amazon_us.collection_campaign SET status='completed',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=%s AND campaign_id=%s",(self.tenant_id,campaign_id)); parent["status"]='completed'
            conn.commit(); public["status"]=parent.get("status")
            public.update({"total":total,"terminal":terminal,"counts":counts,"members":[{key:(value.isoformat() if hasattr(value,"isoformat") else value) for key,value in row.items() if key not in {"next_retry_at"}} for row in members]})
            return public

    def _cancel_unshared_campaign_jobs(self,cursor:Any,campaign_id:str)->None:
        cursor.execute("""UPDATE amazon_us.refresh_request r SET status='cancelled',completed_at=CURRENT_TIMESTAMP
                          FROM amazon_us.collection_campaign_member m
                          WHERE m.tenant_id=%s AND m.campaign_id=%s AND r.tenant_id=m.tenant_id AND r.job_id=m.refresh_job_id AND r.status='queued' AND r.reason LIKE 'campaign:%%'
                            AND NOT EXISTS (SELECT 1 FROM amazon_us.collection_campaign_member other JOIN amazon_us.collection_campaign c ON c.tenant_id=other.tenant_id AND c.campaign_id=other.campaign_id WHERE other.tenant_id=m.tenant_id AND other.refresh_job_id=m.refresh_job_id AND other.campaign_id<>m.campaign_id AND c.status='active' AND c.deadline>CURRENT_TIMESTAMP)""",(self.tenant_id,campaign_id))

    def cancel_campaign(self,campaign_id:str)->dict[str,Any]|None:
        with self._connect_factory() as conn,conn.cursor() as cursor:
            cursor.execute("UPDATE amazon_us.collection_campaign SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE tenant_id=%s AND campaign_id=%s AND status='active' RETURNING campaign_id",(self.tenant_id,campaign_id))
            changed=cursor.fetchone()
            if changed is None:
                cursor.execute("SELECT campaign_id FROM amazon_us.collection_campaign WHERE tenant_id=%s AND campaign_id=%s",(self.tenant_id,campaign_id))
                if cursor.fetchone() is None: return None
            else: self._cancel_unshared_campaign_jobs(cursor,campaign_id)
            conn.commit()
        return self.load_campaign(campaign_id)

    def request_refresh_batch(self, marketplace: str, asins: list[str], requested_by: str, reason: str) -> list[dict[str, Any]]:
        normalized = list(dict.fromkeys(asins))
        if not 1 <= len(normalized) <= 5:
            raise ValueError("refresh batch requires 1 to 5 ASINs")
        with self._connect_factory() as conn:
            try:
                requests = []
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT DISTINCT ON (asin) asin,subject_type FROM amazon_us.item_state "
                        "WHERE tenant_id=%s AND marketplace=%s AND asin=ANY(%s) "
                        "ORDER BY asin,CASE subject_type WHEN 'own' THEN 0 WHEN 'competitor' THEN 1 ELSE 2 END",
                        (self.tenant_id, marketplace, normalized),
                    )
                    subjects = {dict(row)["asin"]: dict(row).get("subject_type", "own") for row in cursor.fetchall()}
                    missing = [asin for asin in normalized if asin not in subjects]
                    if missing:
                        raise KeyError(f"ASIN not found: {marketplace}/{','.join(missing)}")
                    for asin in normalized:
                        request = {
                            "job_id": f"refresh-{uuid.uuid4().hex}", "marketplace": marketplace, "asin": asin,
                            "requested_by": requested_by or "collection-api", "reason": reason or "on_demand", "status": "queued",
                        }
                        subject_type = subjects[asin]
                        cursor.execute(
                            "INSERT INTO amazon_us.refresh_request(job_id,tenant_id,marketplace,asin,subject_type,requested_by,reason,status) "
                            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,marketplace,asin,subject_type) "
                            "WHERE status IN ('queued','claimed') DO UPDATE SET job_id=amazon_us.refresh_request.job_id, "
                            "requested_by=CASE WHEN amazon_us.refresh_request.reason LIKE 'campaign:%%' THEN EXCLUDED.requested_by ELSE amazon_us.refresh_request.requested_by END, "
                            "reason=CASE WHEN amazon_us.refresh_request.reason LIKE 'campaign:%%' THEN 'direct:' || EXCLUDED.reason ELSE amazon_us.refresh_request.reason END RETURNING *",
                            (request["job_id"], self.tenant_id, marketplace, asin, subject_type, request["requested_by"], request["reason"], request["status"]),
                        )
                        inserted = cursor.fetchone()
                        newly_inserted = inserted is not None and str(dict(inserted).get("job_id")) == request["job_id"]
                        if newly_inserted:
                            # A completed refresh is one logical job. A later explicit refresh
                            # starts a fresh bounded job, but only successful terminal outcomes
                            # may receive new budget. Exhausted/identity failures cannot bypass
                            # their terminal state by repeatedly calling the API.
                            self._reset_successful_terminal_recovery(cursor, marketplace, asin, subject_type)
                        if inserted is None:
                            raise RuntimeError("refresh_request_not_persisted")
                        request = inserted if isinstance(inserted, dict) else dict(inserted)
                        request.setdefault("requested_at", _now())
                        requests.append(request)
                conn.commit()
                return requests
            except Exception:
                conn.rollback()
                raise

    def load_refresh_request(self, job_id: str) -> dict[str, Any] | None:
        with self._connect_factory() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM amazon_us.refresh_request WHERE tenant_id=%s AND job_id=%s", (self.tenant_id, job_id))
                row = cursor.fetchone()
                return dict(row) if row is not None else None

    def record_api_audit(self, *, agent_id: str, action: str, resource: str, outcome: str) -> None:
        with self._connect_factory() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO amazon_us.collection_api_audit "
                    "(tenant_id,agent_id,action,resource,outcome) VALUES(%s,%s,%s,%s,%s)",
                    (self.tenant_id, agent_id[:120], action[:80], resource[:240], outcome[:80]),
                )
            conn.commit()
