"""PostgreSQL task storage for the production Amazon worker.

The worker-facing API is deliberately small.  It owns task claiming and
lease checks; parsing and transport remain in the existing worker layers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
import uuid
from typing import Any, Callable


class LeaseLostError(RuntimeError):
    """Raised when a worker tries to write a task it no longer owns."""


class PostgresWorkerStorage:
    """Tenant-scoped PostgreSQL storage used by one or more crawler workers."""

    VALID_STATUSES = {"pending", "running", "product_done", "reviews_pending", "succeeded", "blocked", "failed"}

    def __init__(
        self,
        dsn: str,
        tenant_id: str,
        subject_type: str = "own",
        *,
        connect: Callable[[], Any] | None = None,
        default_lease_seconds: int = 300,
    ) -> None:
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if subject_type not in {"own", "competitor", "candidate"}:
            raise ValueError("invalid subject_type")
        if default_lease_seconds <= 0:
            raise ValueError("default_lease_seconds must be positive")
        self.dsn = dsn
        self.tenant_id = tenant_id.strip()
        self.subject_type = subject_type
        self.default_lease_seconds = int(default_lease_seconds)
        self._connect_factory = connect or self._connect

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL worker storage requires optional dependency psycopg") from exc
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _as_dict(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        return dict(row) if isinstance(row, Mapping) else dict(row)

    @staticmethod
    def _jsonb(value: Any) -> Any:
        try:
            from psycopg.types.json import Jsonb
        except ImportError:
            return value
        return Jsonb(value)

    @staticmethod
    def _nullable_int(value: Any) -> int | None:
        return None if value in (None, "") else int(value)

    @staticmethod
    def _int(value: Any, default: int = 0) -> int:
        return default if value in (None, "") else int(value)

    @staticmethod
    def _nullable_bool(value: Any) -> bool | None:
        return None if value in (None, "") else bool(value)

    @classmethod
    def _state_value(cls, name: str, value: Any) -> Any:
        if name in {
            "next_review_page", "review_page_limit", "reported_rating_count", "reported_review_count",
            "fetched_review_count", "review_pages_fetched",
        }:
            return cls._nullable_int(value)
        return value

    def initialize_manifest(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """Upsert manifest identity and create pending state without resetting progress."""
        count = 0
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    for row in rows:
                        asin = str(row.get("asin", "")).strip().upper()
                        url = str(row.get("url", "")).strip()
                        marketplace = str(row.get("marketplace", "US")).strip().upper()
                        if not asin or not url or marketplace != "US":
                            raise ValueError("manifest rows require asin, url, and US marketplace")
                        source_type = str(row.get("source_type") or row.get("source_site_label") or "manifest")
                        source_url = row.get("source_url") or url
                        priority = int(row.get("priority", 50))
                        cursor.execute(
                            """
                            INSERT INTO amazon_us.asin_master
                              (tenant_id, marketplace, asin, subject_type, source_type, source_url, priority)
                            VALUES (%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (tenant_id, marketplace, asin, subject_type) DO UPDATE SET
                              source_type=EXCLUDED.source_type, source_url=EXCLUDED.source_url,
                              priority=EXCLUDED.priority, updated_at=CURRENT_TIMESTAMP
                            """,
                            (self.tenant_id, marketplace, asin, self.subject_type, source_type, source_url, priority),
                        )
                        cursor.execute(
                            """
                            INSERT INTO amazon_us.item_state
                              (tenant_id, marketplace, asin, subject_type, url, status)
                            VALUES (%s,%s,%s,%s,%s,'pending')
                            ON CONFLICT (tenant_id, marketplace, asin, subject_type) DO UPDATE SET
                              url=EXCLUDED.url, updated_at=CURRENT_TIMESTAMP
                            """,
                            (self.tenant_id, marketplace, asin, self.subject_type, url),
                        )
                        count += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return count

    def claim_task(
        self,
        worker_id: str,
        *,
        lease_seconds: int | None = None,
        task_stage: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim one eligible task using row locking and a lease token."""
        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id is required")
        seconds = int(lease_seconds if lease_seconds is not None else self.default_lease_seconds)
        if seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        stage = task_stage.strip() if task_stage else None
        if stage not in {None, "product", "reviews"}:
            raise ValueError("task_stage must be product or reviews")
        stage_clause = "AND s.task_stage=%s" if stage else ""
        token = uuid.uuid4().hex
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    sql = f"""
                        WITH candidate AS (
                          SELECT s.tenant_id, s.marketplace, s.asin, s.subject_type
                          FROM amazon_us.item_state s
                          JOIN amazon_us.asin_master m USING (tenant_id, marketplace, asin, subject_type)
                          WHERE s.tenant_id=%s AND s.marketplace='US' AND s.subject_type=%s
                            AND (s.lease_expires_at IS NULL OR s.lease_expires_at <= CURRENT_TIMESTAMP)
                            AND ((s.status IN ('pending','reviews_pending'))
                              OR (s.status='failed' AND s.attempts < s.max_attempts))
                            AND (s.next_retry_at IS NULL OR s.next_retry_at <= CURRENT_TIMESTAMP)
                            {stage_clause}
                          ORDER BY m.priority DESC, s.updated_at, s.asin
                          FOR UPDATE SKIP LOCKED
                          LIMIT 1
                        ), claimed AS (
                          UPDATE amazon_us.item_state s
                          SET status='running', resume_status=s.status,
                              lease_token=%s, lease_owner=%s,
                              lease_expires_at=CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                              updated_at=CURRENT_TIMESTAMP
                          FROM candidate c
                          WHERE s.tenant_id=c.tenant_id AND s.marketplace=c.marketplace
                            AND s.asin=c.asin AND s.subject_type=c.subject_type
                          RETURNING s.*
                        ), history AS (
                          INSERT INTO amazon_us.state_history
                            (tenant_id, marketplace, asin, subject_type, from_status, to_status, reason)
                          SELECT tenant_id, marketplace, asin, subject_type, resume_status, status, 'lease_claimed'
                          FROM claimed
                        )
                        SELECT * FROM claimed
                        """
                    params: list[Any] = [self.tenant_id, self.subject_type]
                    if stage:
                        params.append(stage)
                    params.extend([token, worker_id.strip(), seconds])
                    cursor.execute(sql, tuple(params))
                    task = self._as_dict(cursor.fetchone())
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return task

    def load_latest_proxy_capacity(self, *, max_age_seconds: int = 3600) -> dict[str, Any] | None:
        """Read the newest tenant-scoped canary fact without claiming collection work."""
        seconds = int(max_age_seconds)
        if seconds < 1 or seconds > 86400:
            raise ValueError("max_age_seconds must be between 1 and 86400")
        with self._connect_factory() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT operation_id,canary_status,planned_slots,tested_slots,available_slots,
                           unique_egress_count,duplicate_egress_count,requested_capacity,
                           required_slots,slot_budget,slot_capacity,capacity_gate_status,capacity_gate_reason,
                           capacity_config_hash,credential_generation,canary_p95_latency_ms,
                           capacity_detail_json,finished_at,
                           finished_at + (%s * INTERVAL '1 second') AS fact_expires_at,
                           CURRENT_TIMESTAMP AS observed_at,
                           finished_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 second') AS is_fresh
                    FROM amazon_us.operation_run
                    WHERE tenant_id=%s AND operation_type='canary' AND finished_at IS NOT NULL
                    ORDER BY started_at DESC LIMIT 1
                    """,
                    (seconds, seconds, self.tenant_id),
                )
                return self._as_dict(cursor.fetchone())

    @staticmethod
    def _iso_time(value: Any) -> str | None:
        if value is None:
            return None
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    @staticmethod
    def _capacity_snapshot(fact: Mapping[str, Any] | None) -> dict[str, Any]:
        if fact is None:
            return {}
        result = {
            key: fact.get(key)
            for key in (
                "canary_status", "planned_slots", "tested_slots", "available_slots",
                "unique_egress_count", "duplicate_egress_count", "requested_capacity",
                "required_slots", "slot_capacity", "capacity_gate_status", "capacity_gate_reason",
                "slot_budget",
                "canary_p95_latency_ms",
            )
        }
        if result.get("canary_p95_latency_ms") is not None:
            result["canary_p95_latency_ms"] = float(result["canary_p95_latency_ms"])
        return result

    def reserve_proxy_capacity(
        self,
        *,
        reservation_id: str,
        owner_id: str,
        capacity_config_hash: str,
        credential_generation: str,
        requested_capacity: int,
        required_slots: int,
        slot_budget: int,
        max_age_seconds: int,
        lease_seconds: int,
    ) -> dict[str, Any]:
        """Atomically reserve distinct redacted canary slots across all tenants and consumers."""
        for value, name in ((reservation_id, "reservation_id"), (owner_id, "owner_id")):
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", str(value or "")):
                raise ValueError(f"invalid {name}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(capacity_config_hash or "")):
            raise ValueError("invalid capacity_config_hash")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,100}", str(credential_generation or "")):
            raise ValueError("invalid credential_generation")
        if min(int(requested_capacity), int(required_slots), int(slot_budget), int(max_age_seconds), int(lease_seconds)) < 1:
            raise ValueError("capacity reservation values must be positive")
        if int(slot_budget) > 5 or int(max_age_seconds) > 86400 or int(lease_seconds) > 3600:
            raise ValueError("capacity reservation limits are out of range")
        expected_required = (int(requested_capacity) + int(slot_budget) - 1) // int(slot_budget)
        if int(required_slots) != expected_required:
            raise ValueError("required_slots does not match requested capacity")
        try:
            from proxy_capacity_gate import evaluate_capacity_snapshot
        except ModuleNotFoundError:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from proxy_capacity_gate import evaluate_capacity_snapshot

        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (capacity_config_hash,))
                    cursor.execute(
                        """
                        UPDATE amazon_us.proxy_capacity_reservation
                        SET status='expired',reason='reservation_expired',updated_at=CURRENT_TIMESTAMP
                        WHERE capacity_config_hash=%s AND status='active' AND expires_at <= CURRENT_TIMESTAMP
                        """,
                        (capacity_config_hash,),
                    )
                    cursor.execute(
                        """
                        SELECT reservation_id,owner_id,canary_operation_id,capacity_config_hash,
                               credential_generation,requested_capacity,required_slots,reserved_slots,
                               slot_ids_json,status,reason,fact_finished_at,fact_expires_at,expires_at,
                               capacity_snapshot_json
                        FROM amazon_us.proxy_capacity_reservation
                        WHERE reservation_id=%s FOR UPDATE
                        """,
                        (reservation_id,),
                    )
                    existing = self._as_dict(cursor.fetchone())
                    if existing is not None:
                        if (
                            existing.get("owner_id") != owner_id
                            or existing.get("capacity_config_hash") != capacity_config_hash
                            or int(existing.get("requested_capacity") or 0) != int(requested_capacity)
                            or int(existing.get("required_slots") or 0) != int(required_slots)
                        ):
                            raise ValueError("capacity reservation identity conflict")
                        result = {
                            "status": existing.get("status"), "reason": existing.get("reason"),
                            "reservation_id": existing.get("reservation_id"), "owner_id": existing.get("owner_id"),
                            "canary_operation_id": existing.get("canary_operation_id"),
                            "capacity_config_hash": existing.get("capacity_config_hash"),
                            "credential_generation": existing.get("credential_generation"),
                            "requested_capacity": int(existing.get("requested_capacity") or 0),
                            "required_slots": int(existing.get("required_slots") or 0),
                            "reserved_slots": int(existing.get("reserved_slots") or 0),
                            "slot_ids": list(existing.get("slot_ids_json") or []),
                            "fact_finished_at": self._iso_time(existing.get("fact_finished_at")),
                            "fact_expires_at": self._iso_time(existing.get("fact_expires_at")),
                            "reservation_expires_at": self._iso_time(existing.get("expires_at")),
                            "capacity_snapshot": dict(existing.get("capacity_snapshot_json") or {}),
                        }
                        conn.commit()
                        return result
                    cursor.execute(
                        """
                        SELECT operation_id,canary_status,planned_slots,tested_slots,available_slots,
                               unique_egress_count,duplicate_egress_count,requested_capacity,
                               required_slots,slot_budget,slot_capacity,capacity_gate_status,capacity_gate_reason,
                               capacity_config_hash,credential_generation,canary_p95_latency_ms,
                               capacity_detail_json,finished_at,
                               finished_at + (%s * INTERVAL '1 second') AS fact_expires_at,
                               CURRENT_TIMESTAMP AS observed_at,
                               finished_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 second') AS is_fresh
                        FROM amazon_us.operation_run
                        WHERE tenant_id=%s AND operation_type='canary' AND finished_at IS NOT NULL
                        ORDER BY started_at DESC LIMIT 1 FOR UPDATE
                        """,
                        (max_age_seconds, max_age_seconds, self.tenant_id),
                    )
                    fact = self._as_dict(cursor.fetchone())
                    decision = evaluate_capacity_snapshot(
                        fact,
                        expected_config_hash=capacity_config_hash,
                        slot_budget=slot_budget,
                        requested_actions=requested_capacity,
                    )
                    if fact is not None and str(fact.get("credential_generation") or "") != credential_generation:
                        decision = {**decision, "status": "denied", "reason": "credential_generation_mismatch"}
                    slot_ids: list[str] = []
                    if decision["status"] == "allowed" and fact is not None:
                        sessions = list((fact.get("capacity_detail_json") or {}).get("sessions") or [])
                        usable = [
                            str(item.get("session_id")) for item in sessions
                            if isinstance(item, Mapping) and item.get("status") == "available" and item.get("usable") is True
                        ]
                        if (
                            len(usable) != int(fact.get("unique_egress_count") or -1)
                            or len(set(usable)) != len(usable)
                            or any(not re.fullmatch(r"session-\d{2}", value) for value in usable)
                        ):
                            decision = {**decision, "status": "denied", "reason": "capacity_fact_inconsistent"}
                        else:
                            cursor.execute(
                                """
                                SELECT slot_ids_json FROM amazon_us.proxy_capacity_reservation
                                WHERE capacity_config_hash=%s AND status='active' AND expires_at>CURRENT_TIMESTAMP
                                FOR UPDATE
                                """,
                                (capacity_config_hash,),
                            )
                            occupied = {
                                str(slot_id)
                                for row in cursor.fetchall()
                                for slot_id in list((dict(row) if isinstance(row, Mapping) else {"slot_ids_json": row[0]}).get("slot_ids_json") or [])
                            }
                            slot_ids = [slot_id for slot_id in usable if slot_id not in occupied][:required_slots]
                            if len(slot_ids) < required_slots:
                                decision = {**decision, "status": "denied", "reason": "capacity_reserved_elsewhere"}
                                slot_ids = []
                    now = (fact or {}).get("observed_at") or datetime.now(timezone.utc)
                    fact_finished = (fact or {}).get("finished_at")
                    fact_expires = (fact or {}).get("fact_expires_at")
                    reservation_expires = min(fact_expires, now + timedelta(seconds=lease_seconds)) if fact_expires else None
                    status = "active" if decision["status"] == "allowed" and len(slot_ids) == required_slots else "denied"
                    reason = "capacity_reserved" if status == "active" else str(decision.get("reason") or "capacity_reservation_denied")
                    snapshot = self._capacity_snapshot(fact)
                    result = {
                        "status": status, "reason": reason, "reservation_id": reservation_id,
                        "owner_id": owner_id, "canary_operation_id": (fact or {}).get("operation_id"),
                        "capacity_config_hash": capacity_config_hash,
                        "credential_generation": credential_generation,
                        "requested_capacity": requested_capacity, "required_slots": required_slots,
                        "reserved_slots": len(slot_ids), "slot_ids": slot_ids,
                        "fact_finished_at": self._iso_time(fact_finished),
                        "fact_expires_at": self._iso_time(fact_expires),
                        "reservation_expires_at": self._iso_time(reservation_expires),
                        "capacity_snapshot": snapshot,
                    }
                    cursor.execute(
                        """
                        INSERT INTO amazon_us.proxy_capacity_reservation
                          (reservation_id,tenant_id,owner_id,canary_operation_id,capacity_config_hash,
                           credential_generation,requested_capacity,required_slots,reserved_slots,slot_ids_json,
                           status,reason,fact_finished_at,fact_expires_at,expires_at,capacity_snapshot_json)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb)
                        """,
                        (
                            reservation_id, self.tenant_id, owner_id, result["canary_operation_id"], capacity_config_hash,
                            credential_generation, requested_capacity, required_slots, len(slot_ids), json.dumps(slot_ids),
                            status, reason, fact_finished, fact_expires, reservation_expires,
                            json.dumps(snapshot, ensure_ascii=False),
                        ),
                    )
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def validate_proxy_capacity_reservation(
        self,
        reservation_id: str,
        owner_id: str,
        *,
        max_age_seconds: int,
        lease_seconds: int,
    ) -> dict[str, Any]:
        """Revalidate and renew one reservation without extending past its canary fact expiry."""
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT r.*,CURRENT_TIMESTAMP AS observed_at,
                               o.operation_id AS authorizing_canary_operation_id,o.canary_status,o.capacity_gate_status,
                               o.capacity_gate_reason,o.capacity_config_hash AS fact_config_hash,
                               o.credential_generation AS fact_credential_generation,o.finished_at AS authorizing_finished_at,
                               o.finished_at + (%s * INTERVAL '1 second') AS authorizing_fact_expires_at
                        FROM amazon_us.proxy_capacity_reservation r
                        LEFT JOIN amazon_us.operation_run o
                          ON o.operation_id=r.canary_operation_id AND o.tenant_id=r.tenant_id
                         AND o.operation_type='canary' AND o.finished_at IS NOT NULL
                        WHERE r.reservation_id=%s AND r.owner_id=%s
                        FOR UPDATE OF r
                        """,
                        (max_age_seconds, reservation_id, owner_id),
                    )
                    row = self._as_dict(cursor.fetchone())
                    reason = None
                    if row is None:
                        reason = "capacity_reservation_missing"
                    elif row.get("status") != "active":
                        reason = str(row.get("reason") or "capacity_reservation_inactive")
                    elif row.get("expires_at") is None or row.get("expires_at") <= row.get("observed_at"):
                        reason = "capacity_reservation_expired"
                    elif row.get("authorizing_canary_operation_id") != row.get("canary_operation_id"):
                        reason = "capacity_evidence_missing"
                    elif row.get("authorizing_fact_expires_at") is None or row.get("authorizing_fact_expires_at") <= row.get("observed_at"):
                        reason = "capacity_evidence_stale"
                    elif row.get("canary_status") not in {"succeeded", "partial"} or row.get("capacity_gate_status") != "allowed":
                        reason = "canary_fact_denied"
                    elif row.get("fact_config_hash") != row.get("capacity_config_hash"):
                        reason = "capacity_config_mismatch"
                    elif row.get("fact_credential_generation") != row.get("credential_generation"):
                        reason = "credential_generation_mismatch"
                    if reason:
                        if row is not None and row.get("status") == "active":
                            cursor.execute(
                                "UPDATE amazon_us.proxy_capacity_reservation SET status='expired',reason=%s,updated_at=CURRENT_TIMESTAMP WHERE reservation_id=%s",
                                (reason, reservation_id),
                            )
                        conn.commit()
                        return {"status": "denied", "reason": reason, "reservation_id": reservation_id}
                    new_expiry = min(
                        row["authorizing_fact_expires_at"],
                        row["observed_at"] + timedelta(seconds=lease_seconds),
                    )
                    cursor.execute(
                        "UPDATE amazon_us.proxy_capacity_reservation SET expires_at=%s,updated_at=CURRENT_TIMESTAMP WHERE reservation_id=%s",
                        (new_expiry, reservation_id),
                    )
                    result = {
                        "status": "active", "reason": "capacity_reserved",
                        "reservation_id": reservation_id, "owner_id": owner_id,
                        "canary_operation_id": row.get("canary_operation_id"),
                        "capacity_config_hash": row.get("capacity_config_hash"),
                        "credential_generation": row.get("credential_generation"),
                        "requested_capacity": int(row.get("requested_capacity") or 0),
                        "required_slots": int(row.get("required_slots") or 0),
                        "reserved_slots": int(row.get("reserved_slots") or 0),
                        "slot_ids": list(row.get("slot_ids_json") or []),
                        "fact_finished_at": self._iso_time(row.get("fact_finished_at")),
                        "fact_expires_at": self._iso_time(row.get("fact_expires_at")),
                        "reservation_expires_at": self._iso_time(new_expiry),
                        "capacity_snapshot": dict(row.get("capacity_snapshot_json") or {}),
                    }
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def release_proxy_capacity(self, reservation_id: str, owner_id: str) -> bool:
        with self._connect_factory() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE amazon_us.proxy_capacity_reservation
                    SET status='released',reason='capacity_released',released_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                    WHERE reservation_id=%s AND owner_id=%s AND status='active'
                    """,
                    (reservation_id, owner_id),
                )
                changed = cursor.rowcount == 1
            conn.commit()
        return changed

    def update_task(
        self,
        asin: str,
        lease_token: str,
        worker_id: str,
        status: str,
        *,
        reason: str | None = None,
        last_error: str | None = None,
        task_stage: str | None = None,
        resume_status: str | None = None,
    ) -> bool:
        """Update a task only while the supplied worker still owns its lease."""
        if status not in self.VALID_STATUSES:
            raise ValueError(f"invalid task status: {status}")
        if not lease_token or not worker_id:
            raise ValueError("lease_token and worker_id are required")
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT status FROM amazon_us.item_state
                        WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s
                          AND lease_token=%s AND lease_owner=%s
                          AND lease_expires_at > CURRENT_TIMESTAMP
                        FOR UPDATE
                        """,
                        (self.tenant_id, asin, self.subject_type, lease_token, worker_id),
                    )
                    current = self._as_dict(cursor.fetchone())
                    if current is None:
                        conn.rollback()
                        return False
                    cursor.execute(
                        """
                        UPDATE amazon_us.item_state
                        SET status=%s, last_error=%s,
                            task_stage=COALESCE(%s, task_stage),
                            resume_status=COALESCE(%s, resume_status),
                            lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s
                          AND lease_token=%s AND lease_owner=%s
                          AND lease_expires_at > CURRENT_TIMESTAMP
                        RETURNING status
                        """,
                        (status, last_error, task_stage, resume_status, self.tenant_id, asin, self.subject_type, lease_token, worker_id),
                    )
                    changed = self._as_dict(cursor.fetchone())
                    if changed is None:
                        conn.rollback()
                        return False
                    cursor.execute(
                        """
                        INSERT INTO amazon_us.state_history
                          (tenant_id, marketplace, asin, subject_type, from_status, to_status, reason)
                        VALUES (%s,'US',%s,%s,%s,%s,%s)
                        """,
                        (self.tenant_id, asin, self.subject_type, current["status"], status, reason),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return True

    def reclaim_expired_leases(self) -> int:
        """Return only expired running leases to their resumable status."""
        reclaimed = 0
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        WITH expired AS (
                          SELECT tenant_id, marketplace, asin, subject_type, status AS from_status,
                                 COALESCE(NULLIF(resume_status,''),'pending') AS to_status
                          FROM amazon_us.item_state
                          WHERE tenant_id=%s AND marketplace='US' AND subject_type=%s
                            AND status='running' AND lease_expires_at IS NOT NULL
                            AND lease_expires_at <= CURRENT_TIMESTAMP
                          FOR UPDATE SKIP LOCKED
                        ), changed AS (
                          UPDATE amazon_us.item_state s
                          SET status=e.to_status, lease_token=NULL, lease_owner=NULL,
                              lease_expires_at=NULL, updated_at=CURRENT_TIMESTAMP
                          FROM expired e
                          WHERE s.tenant_id=e.tenant_id AND s.marketplace=e.marketplace
                            AND s.asin=e.asin AND s.subject_type=e.subject_type
                            RETURNING s.asin, s.status AS to_status
                        )
                        SELECT * FROM changed
                        """,
                        (self.tenant_id, self.subject_type),
                    )
                    rows = [self._as_dict(row) for row in cursor.fetchall()]
                    cursor.execute(
                        """
                        UPDATE amazon_us.refresh_request r
                        SET status='queued',claimed_at=NULL,completed_at=NULL
                        WHERE r.tenant_id=%s AND r.marketplace='US' AND r.subject_type=%s
                          AND r.status='claimed'
                          AND NOT EXISTS (
                            SELECT 1 FROM amazon_us.item_state s
                            WHERE s.tenant_id=r.tenant_id AND s.marketplace=r.marketplace
                              AND s.asin=r.asin AND s.subject_type=r.subject_type
                              AND s.status='running' AND s.lease_expires_at > CURRENT_TIMESTAMP
                          )
                        """,
                        (self.tenant_id, self.subject_type),
                    )
                    for row in rows:
                        cursor.execute(
                            """
                            INSERT INTO amazon_us.state_history
                              (tenant_id, marketplace, asin, subject_type, from_status, to_status, reason)
                            VALUES (%s,'US',%s,%s,%s,%s,'lease_expired')
                            """,
                            (self.tenant_id, row["asin"], self.subject_type, "running", row["to_status"]),
                        )
                    reclaimed = len(rows)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return reclaimed

    def claim_refresh_request(self) -> dict[str, Any] | None:
        """Atomically claim the oldest queued refresh request in this scope."""
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        WITH next_request AS (
                          SELECT job_id
                          FROM amazon_us.refresh_request
                          WHERE tenant_id=%s AND marketplace='US' AND subject_type=%s AND status='queued'
                          ORDER BY requested_at, job_id
                          FOR UPDATE SKIP LOCKED
                          LIMIT 1
                        )
                        UPDATE amazon_us.refresh_request r
                        SET status='claimed', claimed_at=CURRENT_TIMESTAMP
                        FROM next_request n
                        WHERE r.job_id=n.job_id AND r.tenant_id=%s
                        RETURNING r.*
                        """,
                        (self.tenant_id, self.subject_type, self.tenant_id),
                    )
                    request = self._as_dict(cursor.fetchone())
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return request

    def has_pending_refresh_task(self) -> bool:
        """Read whether this Agent scope has claimable refresh work without taking a lease."""
        with self._connect_factory() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT EXISTS (
                      SELECT 1
                      FROM amazon_us.refresh_request r
                      JOIN amazon_us.item_state s
                        ON s.tenant_id=r.tenant_id AND s.marketplace=r.marketplace
                       AND s.asin=r.asin AND s.subject_type=r.subject_type
                      WHERE r.tenant_id=%s AND r.marketplace='US' AND r.subject_type=%s
                        AND r.status='queued'
                        AND (s.status<>'running' OR s.lease_expires_at <= CURRENT_TIMESTAMP)
                    ) AS has_pending
                    """,
                    (self.tenant_id, self.subject_type),
                )
                row = cursor.fetchone()
        if isinstance(row, Mapping):
            return bool(row.get("has_pending"))
        return bool(row[0]) if row else False

    def claim_refresh_task(self, worker_id: str, *, lease_seconds: int | None = None) -> dict[str, Any] | None:
        """Atomically claim a queued refresh request and lease its requested ASIN."""
        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id is required")
        seconds = int(lease_seconds if lease_seconds is not None else self.default_lease_seconds)
        if seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        token = uuid.uuid4().hex
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        WITH next_request AS (
                          SELECT r.job_id,r.tenant_id,r.marketplace,r.asin,r.subject_type,s.status AS previous_status
                          FROM amazon_us.refresh_request r
                          JOIN amazon_us.item_state s
                            ON s.tenant_id=r.tenant_id AND s.marketplace=r.marketplace
                           AND s.asin=r.asin AND s.subject_type=r.subject_type
                          WHERE r.tenant_id=%s AND r.marketplace='US' AND r.subject_type=%s
                            AND r.status='queued'
                            AND (s.status<>'running' OR s.lease_expires_at <= CURRENT_TIMESTAMP)
                          ORDER BY r.requested_at,r.job_id
                          FOR UPDATE OF r,s SKIP LOCKED
                          LIMIT 1
                        ), marked_request AS (
                          UPDATE amazon_us.refresh_request r
                          SET status='claimed',claimed_at=CURRENT_TIMESTAMP
                          FROM next_request n
                          WHERE r.job_id=n.job_id AND r.tenant_id=n.tenant_id
                          RETURNING r.job_id
                        ), claimed AS (
                          UPDATE amazon_us.item_state s
                          SET status='running',resume_status='pending',attempts=0,task_stage='product',
                              next_review_url=NULL,next_review_page=NULL,next_retry_at=NULL,
                              block_reason=NULL,last_error=NULL,lease_token=%s,lease_owner=%s,
                              lease_expires_at=CURRENT_TIMESTAMP+(%s*INTERVAL '1 second'),updated_at=CURRENT_TIMESTAMP
                          FROM next_request n
                          WHERE s.tenant_id=n.tenant_id AND s.marketplace=n.marketplace
                            AND s.asin=n.asin AND s.subject_type=n.subject_type
                          RETURNING s.*
                        ), history AS (
                          INSERT INTO amazon_us.state_history
                            (tenant_id,marketplace,asin,subject_type,from_status,to_status,reason)
                          SELECT c.tenant_id,c.marketplace,c.asin,c.subject_type,n.previous_status,c.status,'refresh_lease_claimed'
                          FROM claimed c CROSS JOIN next_request n
                        )
                        SELECT c.*,m.job_id FROM claimed c CROSS JOIN marked_request m
                        """,
                        (self.tenant_id, self.subject_type, token, worker_id.strip(), seconds),
                    )
                    task = self._as_dict(cursor.fetchone())
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return task

    def finish_refresh_request(self, job_id: str, status: str) -> None:
        if status not in {"queued", "completed", "failed", "cancelled"}:
            raise ValueError(f"invalid refresh status: {status}")
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE amazon_us.refresh_request
                        SET status=%s,completed_at=CASE WHEN %s IN ('completed','failed','cancelled') THEN CURRENT_TIMESTAMP ELSE NULL END
                        WHERE job_id=%s AND tenant_id=%s
                        """,
                        (status, status, job_id, self.tenant_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def fail_claimed_refreshes(self, worker_id: str, reason: str = "agent_refresh_worker_failed") -> int:
        """Fail refresh jobs still owned by one failed worker and release their leases."""
        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id is required")
        safe_reason = str(reason or "agent_refresh_worker_failed")[:240]
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT r.job_id,s.marketplace,s.asin,s.subject_type,s.status AS previous_status
                        FROM amazon_us.refresh_request r
                        JOIN amazon_us.item_state s
                          ON s.tenant_id=r.tenant_id AND s.marketplace=r.marketplace
                         AND s.asin=r.asin AND s.subject_type=r.subject_type
                        WHERE r.tenant_id=%s AND r.subject_type=%s AND r.status='claimed'
                          AND s.status='running' AND s.lease_owner=%s
                        FOR UPDATE OF r,s
                        """,
                        (self.tenant_id, self.subject_type, worker_id.strip()),
                    )
                    rows = [self._as_dict(row) for row in cursor.fetchall()]
                    for row in rows:
                        cursor.execute(
                            "UPDATE amazon_us.refresh_request SET status='failed',completed_at=CURRENT_TIMESTAMP "
                            "WHERE tenant_id=%s AND job_id=%s AND status='claimed'",
                            (self.tenant_id, row["job_id"]),
                        )
                        cursor.execute(
                            "UPDATE amazon_us.item_state SET status='failed',resume_status=NULL,next_retry_at=NULL,"
                            "block_reason=NULL,last_error=%s,lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,"
                            "updated_at=CURRENT_TIMESTAMP WHERE tenant_id=%s AND marketplace=%s AND asin=%s "
                            "AND subject_type=%s AND status='running' AND lease_owner=%s",
                            (safe_reason, self.tenant_id, row["marketplace"], row["asin"], row["subject_type"], worker_id.strip()),
                        )
                        cursor.execute(
                            "INSERT INTO amazon_us.state_history "
                            "(tenant_id,marketplace,asin,subject_type,from_status,to_status,reason) "
                            "VALUES(%s,%s,%s,%s,%s,'failed',%s)",
                            (self.tenant_id, row["marketplace"], row["asin"], row["subject_type"], row["previous_status"], safe_reason),
                        )
                conn.commit()
                return len(rows)
            except Exception:
                conn.rollback()
                raise

    def enqueue_due_refreshes(self, *, min_age_hours: int = 24, limit: int = 1000) -> int:
        """Queue stale product snapshots once per scoped ASIN."""
        hours = int(min_age_hours)
        batch_limit = int(limit)
        if hours < 1 or batch_limit < 1:
            raise ValueError("min_age_hours and limit must be positive")
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        WITH due AS (
                          SELECT p.tenant_id,p.marketplace,p.asin,p.subject_type
                          FROM amazon_us.product_latest p
                          JOIN amazon_us.item_state s
                            ON s.tenant_id=p.tenant_id AND s.marketplace=p.marketplace
                           AND s.asin=p.asin AND s.subject_type=p.subject_type
                          WHERE p.tenant_id=%s AND p.marketplace='US' AND p.subject_type=%s
                            AND p.collected_at <= CURRENT_TIMESTAMP-(%s*INTERVAL '1 hour')
                            AND s.status<>'running'
                            AND NOT EXISTS (
                              SELECT 1 FROM amazon_us.refresh_request r
                              WHERE r.tenant_id=p.tenant_id AND r.marketplace=p.marketplace
                                AND r.asin=p.asin AND r.subject_type=p.subject_type
                                AND r.status IN ('queued','claimed')
                            )
                          ORDER BY p.collected_at,p.asin
                          LIMIT %s
                        )
                        INSERT INTO amazon_us.refresh_request
                          (job_id,tenant_id,marketplace,asin,subject_type,requested_by,reason,status)
                        SELECT 'scheduled-'||md5(d.tenant_id||d.marketplace||d.asin||d.subject_type||clock_timestamp()::text||random()::text),
                               d.tenant_id,d.marketplace,d.asin,d.subject_type,'scheduler','stale_snapshot','queued'
                        FROM due d
                        ON CONFLICT DO NOTHING
                        RETURNING job_id
                        """,
                        (self.tenant_id, self.subject_type, hours, batch_limit),
                    )
                    queued = len(cursor.fetchall())
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return queued

    def save_product_result(
        self,
        *,
        task: Mapping[str, Any],
        evidence: Mapping[str, Any],
        product: Mapping[str, Any],
        media: Iterable[Mapping[str, Any]],
        content_modules: Iterable[Mapping[str, Any]],
        review_summary: Mapping[str, Any],
        next_status: str,
        state_fields: Mapping[str, Any],
        reason: str,
    ) -> bool:
        """Atomically persist one validated product action and release its lease."""
        if next_status not in self.VALID_STATUSES:
            raise ValueError(f"invalid task status: {next_status}")
        asin = str(task.get("asin", "")).strip().upper()
        lease_token = str(task.get("lease_token", ""))
        lease_owner = str(task.get("lease_owner", ""))
        if not asin or not lease_token or not lease_owner:
            raise ValueError("task requires asin, lease_token, and lease_owner")
        allowed_state_fields = {
            "task_stage", "resume_status", "next_review_url", "next_review_page",
            "next_retry_at", "review_page_limit", "reported_rating_count",
            "reported_review_count", "reported_count_source", "fetched_review_count",
            "review_pages_fetched", "block_reason", "last_error",
        }
        unknown = set(state_fields) - allowed_state_fields
        if unknown:
            raise ValueError(f"unsupported state fields: {sorted(unknown)}")
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT status FROM amazon_us.item_state
                        WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s
                          AND lease_token=%s AND lease_owner=%s
                          AND lease_expires_at > CURRENT_TIMESTAMP
                        FOR UPDATE
                        """,
                        (self.tenant_id, asin, self.subject_type, lease_token, lease_owner),
                    )
                    current = self._as_dict(cursor.fetchone())
                    if current is None:
                        conn.rollback()
                        return False
                    cursor.execute(
                        """
                        INSERT INTO amazon_us.collection_evidence
                          (tenant_id,marketplace,asin,subject_type,run_id,url,http_status,transfer_bytes,
                           retrieved_at,source_type,content_hash,raw_html_path,block_reason,parser_version,error_code,context_json)
                        VALUES (%s,'US',%s,%s,%s,%s,%s,%s,COALESCE(%s,CURRENT_TIMESTAMP),%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            self.tenant_id, asin, self.subject_type, evidence.get("run_id"), evidence.get("url"),
                            self._nullable_int(evidence.get("http_status")), self._nullable_int(evidence.get("transfer_bytes")), evidence.get("retrieved_at"),
                            evidence.get("source_type"), evidence.get("content_hash"), evidence.get("raw_html_path"),
                            evidence.get("block_reason"), evidence.get("parser_version"), evidence.get("error_code"),
                            self._jsonb(evidence.get("context_json") or {}),
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO amazon_us.product_snapshot
                          (tenant_id,marketplace,asin,subject_type,canonical_url,availability,title,brand,rating,
                           reported_rating_count,reported_review_count,review_count,review_count_source,price,
                           bullets,product_description,specs,buy_box,top_reviews,review_link,review_section_anchor,
                           aplus_present,collected_at,status)
                        VALUES (%s,'US',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                COALESCE(%s,CURRENT_TIMESTAMP),%s)
                        """,
                        (
                            self.tenant_id, asin, self.subject_type, product.get("canonical_url"),
                            product.get("availability"), product.get("title"), product.get("brand"), product.get("rating"),
                            self._nullable_int(product.get("reported_rating_count")), self._nullable_int(product.get("reported_review_count")),
                            product.get("review_count"), product.get("review_count_source"), product.get("price"),
                            self._jsonb(product.get("bullets") or []), product.get("product_description"),
                            self._jsonb(product.get("specs") or {}), self._jsonb(product.get("buy_box") or {}),
                            self._jsonb(product.get("top_reviews") or []), product.get("review_link"),
                            product.get("review_section_anchor"), bool(product.get("aplus_present")),
                            product.get("collected_at"), product.get("status") or "product_done",
                        ),
                    )
                    cursor.execute(
                        "DELETE FROM amazon_us.media_asset WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s",
                        (self.tenant_id, asin, self.subject_type),
                    )
                    for item in media:
                        cursor.execute(
                            """
                            INSERT INTO amazon_us.media_asset
                              (tenant_id,marketplace,asin,subject_type,placement,entry_type,thumbnail_url,display_url,
                               asset_url,poster_url,ordinal,is_primary,width,height,alt_text,variant_asin,load_status,
                               failure_reason,unique_key)
                            VALUES (%s,'US',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (tenant_id,marketplace,asin,subject_type,unique_key) DO UPDATE SET
                              placement=EXCLUDED.placement, entry_type=EXCLUDED.entry_type,
                              asset_url=EXCLUDED.asset_url, load_status=EXCLUDED.load_status,
                              failure_reason=EXCLUDED.failure_reason
                            """,
                            (
                                self.tenant_id, asin, self.subject_type, item.get("placement"), item.get("entry_type"),
                                item.get("thumbnail_url"), item.get("display_url"), item.get("asset_url"),
                                item.get("poster_url"), self._nullable_int(item.get("ordinal")), self._nullable_bool(item.get("is_primary")), item.get("width"),
                                item.get("height"), item.get("alt_text"), item.get("variant_asin"), item.get("load_status"),
                                item.get("failure_reason"), item.get("unique_key"),
                            ),
                        )
                    cursor.execute(
                        "DELETE FROM amazon_us.content_module WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s",
                        (self.tenant_id, asin, self.subject_type),
                    )
                    for item in content_modules:
                        cursor.execute(
                            """
                            INSERT INTO amazon_us.content_module
                              (tenant_id,marketplace,asin,subject_type,module_type,position,order_index,text,image_url,
                               link_url,status,unique_key)
                            VALUES (%s,'US',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (tenant_id,marketplace,asin,subject_type,unique_key) DO UPDATE SET
                              position=EXCLUDED.position, order_index=EXCLUDED.order_index, text=EXCLUDED.text,
                              image_url=EXCLUDED.image_url, link_url=EXCLUDED.link_url, status=EXCLUDED.status
                            """,
                            (
                                self.tenant_id, asin, self.subject_type, item.get("module_type"), self._int(item.get("position"), 0),
                                self._nullable_int(item.get("order_index")), item.get("text"), item.get("image_url"), item.get("link_url"),
                                item.get("status"), item.get("unique_key"),
                            ),
                        )
                    cursor.execute(
                        """
                        INSERT INTO amazon_us.review_summary
                          (tenant_id,marketplace,asin,subject_type,reported_rating_count,reported_review_count,
                           reported_count_source,fetched_count,pages_fetched,next_page,status,updated_at)
                        VALUES (%s,'US',%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                        ON CONFLICT (tenant_id,marketplace,asin,subject_type) DO UPDATE SET
                          reported_rating_count=EXCLUDED.reported_rating_count,
                          reported_review_count=EXCLUDED.reported_review_count,
                          reported_count_source=EXCLUDED.reported_count_source,
                          fetched_count=EXCLUDED.fetched_count, pages_fetched=EXCLUDED.pages_fetched,
                          next_page=EXCLUDED.next_page, status=EXCLUDED.status, updated_at=CURRENT_TIMESTAMP
                        """,
                        (
                            self.tenant_id, asin, self.subject_type, self._nullable_int(review_summary.get("reported_rating_count")),
                            self._nullable_int(review_summary.get("reported_review_count")), review_summary.get("reported_count_source"),
                            self._int(review_summary.get("fetched_count"), 0), self._int(review_summary.get("pages_fetched"), 0),
                            review_summary.get("next_page"), review_summary.get("status"),
                        ),
                    )
                    assignments = [
                        "status=%s", "attempts=0", "last_error=NULL", "block_reason=NULL", "next_retry_at=NULL"
                    ]
                    params: list[Any] = [next_status]
                    for name, value in state_fields.items():
                        assignments.append(f"{name}=%s")
                        params.append(self._state_value(name, value))
                    assignments.extend([
                        "lease_token=NULL", "lease_owner=NULL", "lease_expires_at=NULL", "updated_at=CURRENT_TIMESTAMP"
                    ])
                    params.extend([self.tenant_id, asin, self.subject_type, lease_token, lease_owner])
                    cursor.execute(
                        f"UPDATE amazon_us.item_state SET {', '.join(assignments)} "
                        "WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s "
                        "AND lease_token=%s AND lease_owner=%s AND lease_expires_at > CURRENT_TIMESTAMP",
                        params,
                    )
                    cursor.execute(
                        """
                        INSERT INTO amazon_us.state_history
                          (tenant_id,marketplace,asin,subject_type,from_status,to_status,reason)
                        VALUES (%s,'US',%s,%s,%s,%s,%s)
                        """,
                        (self.tenant_id, asin, self.subject_type, current["status"], next_status, reason),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return True

    def save_failure(
        self,
        *,
        task: Mapping[str, Any],
        reason: str,
        error: str,
        evidence: Mapping[str, Any] | None = None,
        next_status: str = "failed",
        state_fields: Mapping[str, Any] | None = None,
        increment_attempts: bool = True,
        terminal: bool = False,
    ) -> bool:
        """Persist one failed or deferred action while releasing its lease."""
        if next_status not in self.VALID_STATUSES:
            raise ValueError(f"invalid task status: {next_status}")
        if terminal and next_status != "failed":
            raise ValueError("terminal failures must use failed status")
        asin = str(task.get("asin", "")).strip().upper()
        lease_token = str(task.get("lease_token", ""))
        lease_owner = str(task.get("lease_owner", ""))
        if not asin or not lease_token or not lease_owner:
            raise ValueError("task requires asin, lease_token, and lease_owner")
        allowed = {"task_stage", "resume_status", "next_review_url", "next_review_page", "next_retry_at", "block_reason"}
        fields = dict(state_fields or {})
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported failure state fields: {sorted(unknown)}")
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT status FROM amazon_us.item_state
                        WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s
                          AND lease_token=%s AND lease_owner=%s AND lease_expires_at > CURRENT_TIMESTAMP
                        FOR UPDATE
                        """,
                        (self.tenant_id, asin, self.subject_type, lease_token, lease_owner),
                    )
                    current = self._as_dict(cursor.fetchone())
                    if current is None:
                        conn.rollback()
                        return False
                    if evidence is not None:
                        cursor.execute(
                            """
                            INSERT INTO amazon_us.collection_evidence
                              (tenant_id,marketplace,asin,subject_type,run_id,url,http_status,transfer_bytes,
                               retrieved_at,source_type,content_hash,raw_html_path,block_reason,parser_version,error_code,context_json)
                            VALUES (%s,'US',%s,%s,%s,%s,%s,%s,COALESCE(%s,CURRENT_TIMESTAMP),%s,%s,%s,%s,%s,%s,%s)
                            """,
                            (
                                self.tenant_id, asin, self.subject_type, evidence.get("run_id"), evidence.get("url"),
                                self._nullable_int(evidence.get("http_status")), self._nullable_int(evidence.get("transfer_bytes")), evidence.get("retrieved_at"),
                                evidence.get("source_type"), evidence.get("content_hash"), evidence.get("raw_html_path"),
                                evidence.get("block_reason"), evidence.get("parser_version"), evidence.get("error_code"),
                                self._jsonb(evidence.get("context_json") or {}),
                            ),
                        )
                    assignments = ["status=%s", "last_error=%s"]
                    params: list[Any] = [next_status, error]
                    if terminal:
                        assignments.append("attempts=max_attempts")
                    elif increment_attempts:
                        assignments.append("attempts=attempts+1")
                    for name, value in fields.items():
                        assignments.append(f"{name}=%s")
                        params.append(self._state_value(name, value))
                    assignments.extend([
                        "lease_token=NULL", "lease_owner=NULL", "lease_expires_at=NULL", "updated_at=CURRENT_TIMESTAMP"
                    ])
                    params.extend([self.tenant_id, asin, self.subject_type, lease_token, lease_owner])
                    cursor.execute(
                        f"UPDATE amazon_us.item_state SET {', '.join(assignments)} "
                        "WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s "
                        "AND lease_token=%s AND lease_owner=%s AND lease_expires_at > CURRENT_TIMESTAMP",
                        params,
                    )
                    cursor.execute(
                        """
                        INSERT INTO amazon_us.state_history
                          (tenant_id,marketplace,asin,subject_type,from_status,to_status,reason)
                        VALUES (%s,'US',%s,%s,%s,%s,%s)
                        """,
                        (self.tenant_id, asin, self.subject_type, current["status"], next_status, reason),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return True

    def save_review_result(
        self,
        *,
        task: Mapping[str, Any],
        evidence: Mapping[str, Any],
        page: Mapping[str, Any],
        records: Iterable[Mapping[str, Any]],
        summary: Mapping[str, Any],
        next_status: str,
        state_fields: Mapping[str, Any],
        reason: str,
        increment_attempts: bool = False,
    ) -> bool:
        """Atomically persist one review page, its records, summary, and checkpoint."""
        if next_status not in self.VALID_STATUSES:
            raise ValueError(f"invalid task status: {next_status}")
        asin = str(task.get("asin", "")).strip().upper()
        lease_token = str(task.get("lease_token", ""))
        lease_owner = str(task.get("lease_owner", ""))
        if not asin or not lease_token or not lease_owner:
            raise ValueError("task requires asin, lease_token, and lease_owner")
        allowed = {
            "task_stage", "resume_status", "next_review_url", "next_review_page", "next_retry_at",
            "fetched_review_count", "review_pages_fetched", "block_reason", "last_error",
        }
        unknown = set(state_fields) - allowed
        if unknown:
            raise ValueError(f"unsupported review state fields: {sorted(unknown)}")
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT status FROM amazon_us.item_state
                        WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s
                          AND lease_token=%s AND lease_owner=%s AND lease_expires_at > CURRENT_TIMESTAMP
                        FOR UPDATE
                        """,
                        (self.tenant_id, asin, self.subject_type, lease_token, lease_owner),
                    )
                    current = self._as_dict(cursor.fetchone())
                    if current is None:
                        conn.rollback()
                        return False
                    cursor.execute(
                        """
                        INSERT INTO amazon_us.collection_evidence
                          (tenant_id,marketplace,asin,subject_type,run_id,url,http_status,transfer_bytes,
                           retrieved_at,source_type,content_hash,raw_html_path,block_reason,parser_version,error_code,context_json)
                        VALUES (%s,'US',%s,%s,%s,%s,%s,%s,COALESCE(%s,CURRENT_TIMESTAMP),%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            self.tenant_id, asin, self.subject_type, evidence.get("run_id"), evidence.get("url"),
                            self._nullable_int(evidence.get("http_status")), self._nullable_int(evidence.get("transfer_bytes")), evidence.get("retrieved_at"),
                            evidence.get("source_type"), evidence.get("content_hash"), evidence.get("raw_html_path"),
                            evidence.get("block_reason"), evidence.get("parser_version"), evidence.get("error_code"),
                            self._jsonb(evidence.get("context_json") or {}),
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO amazon_us.review_page_state
                          (tenant_id,marketplace,asin,subject_type,page,url,status,next_url,fetched_at)
                        VALUES (%s,'US',%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                        ON CONFLICT (tenant_id,marketplace,asin,subject_type,page) DO UPDATE SET
                          url=EXCLUDED.url,status=EXCLUDED.status,next_url=EXCLUDED.next_url,fetched_at=CURRENT_TIMESTAMP
                        """,
                        (self.tenant_id, asin, self.subject_type, self._int(page.get("page"), 1), page.get("url"), page.get("status"), page.get("next_url")),
                    )
                    for record in records:
                        cursor.execute(
                            """
                            INSERT INTO amazon_us.review_record
                              (tenant_id,marketplace,asin,subject_type,review_id,rating,title,body,review_url,
                               review_date,locale,verified,body_truncated,review_images,page,unique_key)
                            VALUES (%s,'US',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (tenant_id,marketplace,asin,subject_type,review_id) DO UPDATE SET
                              rating=EXCLUDED.rating,title=EXCLUDED.title,body=EXCLUDED.body,
                              review_url=EXCLUDED.review_url,review_date=EXCLUDED.review_date,
                              verified=EXCLUDED.verified,body_truncated=EXCLUDED.body_truncated,
                              review_images=EXCLUDED.review_images,page=EXCLUDED.page,unique_key=EXCLUDED.unique_key
                            """,
                            (
                                self.tenant_id, asin, self.subject_type, record.get("review_id"), record.get("rating"),
                                record.get("title"), record.get("body"), record.get("review_url"), record.get("review_date"),
                                record.get("locale"), self._nullable_bool(record.get("verified")), self._nullable_bool(record.get("body_truncated")),
                                self._jsonb(record.get("review_images") or []), self._nullable_int(record.get("page")), record.get("unique_key"),
                            ),
                        )
                    cursor.execute(
                        """
                        INSERT INTO amazon_us.review_summary
                          (tenant_id,marketplace,asin,subject_type,reported_rating_count,reported_review_count,
                           reported_count_source,fetched_count,pages_fetched,next_page,status,updated_at)
                        VALUES (%s,'US',%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                        ON CONFLICT (tenant_id,marketplace,asin,subject_type) DO UPDATE SET
                          fetched_count=EXCLUDED.fetched_count,pages_fetched=EXCLUDED.pages_fetched,
                          next_page=EXCLUDED.next_page,status=EXCLUDED.status,updated_at=CURRENT_TIMESTAMP
                        """,
                        (
                            self.tenant_id, asin, self.subject_type, self._nullable_int(summary.get("reported_rating_count")),
                            self._nullable_int(summary.get("reported_review_count")), summary.get("reported_count_source"),
                            self._int(summary.get("fetched_count"), 0), self._int(summary.get("pages_fetched"), 0),
                            summary.get("next_page"), summary.get("status"),
                        ),
                    )
                    assignments = ["status=%s", "attempts=attempts+1" if increment_attempts else "attempts=0"]
                    params: list[Any] = [next_status]
                    for name, value in state_fields.items():
                        assignments.append(f"{name}=%s")
                        params.append(self._state_value(name, value))
                    assignments.extend([
                        "lease_token=NULL", "lease_owner=NULL", "lease_expires_at=NULL", "updated_at=CURRENT_TIMESTAMP"
                    ])
                    params.extend([self.tenant_id, asin, self.subject_type, lease_token, lease_owner])
                    cursor.execute(
                        f"UPDATE amazon_us.item_state SET {', '.join(assignments)} "
                        "WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s "
                        "AND lease_token=%s AND lease_owner=%s AND lease_expires_at > CURRENT_TIMESTAMP",
                        params,
                    )
                    cursor.execute(
                        """
                        INSERT INTO amazon_us.state_history
                          (tenant_id,marketplace,asin,subject_type,from_status,to_status,reason)
                        VALUES (%s,'US',%s,%s,%s,%s,%s)
                        """,
                        (self.tenant_id, asin, self.subject_type, current["status"], next_status, reason),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return True


__all__ = ["LeaseLostError", "PostgresWorkerStorage"]
