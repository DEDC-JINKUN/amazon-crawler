#!/usr/bin/env python3
"""Freeze an auditable 20-member release cohort inside an existing campaign."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Mapping


SAFE_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,160}")


class ReleaseGateConflict(ValueError):
    """A gate identity was reused for a different request."""


class ReleaseGateUnavailable(RuntimeError):
    """The existing campaign cannot supply one complete fixed cohort."""


def _safe_id(value: str, name: str) -> str:
    normalized = str(value or "").strip()
    if not SAFE_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid {name}")
    return normalized


def _request_hash(campaign_id: str, release_id: str, selection_limit: int) -> str:
    payload = json.dumps(
        {"campaign_id": campaign_id, "release_id": release_id, "selection_limit": selection_limit},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _selection_hash(campaign_id: str, members: list[dict[str, Any]]) -> str:
    payload = {
        "campaign_id": campaign_id,
        "members": [
            {
                "asin": item["asin"],
                "subject_type": item["subject_type"],
                "refresh_job_id": item["refresh_job_id"],
                "ordinal": int(item["ordinal"]),
            }
            for item in members
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class CollectionReleaseGateStore:
    def __init__(
        self, dsn: str, *, tenant_id: str, connect: Callable[[], Any] | None = None,
    ) -> None:
        if not str(dsn or "").strip() or not str(tenant_id or "").strip():
            raise ValueError("dsn and tenant_id are required")
        self.dsn = str(dsn)
        self.tenant_id = str(tenant_id).strip()
        if connect is None:
            import psycopg
            from psycopg.rows import dict_row
            connect = lambda: psycopg.connect(self.dsn, row_factory=dict_row)
        self._connect = connect

    @staticmethod
    def _dict(row: Any) -> dict[str, Any] | None:
        return dict(row) if isinstance(row, Mapping) else None

    def _load(self, cursor: Any, gate_id: str) -> dict[str, Any] | None:
        cursor.execute(
            "SELECT * FROM amazon_us.collection_release_gate WHERE tenant_id=%s AND gate_id=%s",
            (self.tenant_id, gate_id),
        )
        gate = self._dict(cursor.fetchone())
        if gate is None:
            return None
        cursor.execute(
            """SELECT asin,subject_type,refresh_job_id,ordinal,release_gate_ordinal
            FROM amazon_us.collection_campaign_member
            WHERE tenant_id=%s AND campaign_id=%s AND release_gate_id=%s
            ORDER BY release_gate_ordinal""",
            (self.tenant_id, gate["campaign_id"], gate_id),
        )
        members = [dict(row) for row in cursor.fetchall()]
        public = {
            key: value.isoformat() if hasattr(value, "isoformat") else value
            for key, value in gate.items()
            if key not in {"request_hash", "receipt_json"}
        }
        public["members"] = members
        return public

    def start(
        self, *, campaign_id: str, gate_id: str, idempotency_key: str,
        release_id: str, selection_limit: int = 20, created_by: str,
    ) -> dict[str, Any]:
        campaign_id = _safe_id(campaign_id, "campaign_id")
        gate_id = _safe_id(gate_id, "gate_id")
        idempotency_key = _safe_id(idempotency_key, "idempotency_key")
        release_id = _safe_id(release_id, "release_id")
        created_by = _safe_id(created_by, "created_by")
        selection_limit = int(selection_limit)
        if selection_limit != 20:
            raise ValueError("release gate selection_limit must be exactly 20")
        request_hash = _request_hash(campaign_id, release_id, selection_limit)

        with self._connect() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                        (f"collection-release-gate:{self.tenant_id}:{campaign_id}",),
                    )
                    cursor.execute(
                        """SELECT * FROM amazon_us.collection_release_gate
                        WHERE tenant_id=%s AND (gate_id=%s OR idempotency_key=%s OR campaign_id=%s)
                        FOR UPDATE""",
                        (self.tenant_id, gate_id, idempotency_key, campaign_id),
                    )
                    existing = self._dict(cursor.fetchone())
                    if existing is not None:
                        same = (
                            existing["gate_id"] == gate_id
                            and existing["idempotency_key"] == idempotency_key
                            and existing["campaign_id"] == campaign_id
                            and existing["release_id"] == release_id
                            and int(existing["selection_limit"]) == selection_limit
                            and existing["request_hash"] == request_hash
                        )
                        if not same:
                            raise ReleaseGateConflict("release gate identity conflict")
                        result = self._load(cursor, gate_id)
                        if result is None:
                            raise RuntimeError("release gate disappeared")
                        result["duplicate"] = True
                        conn.commit()
                        return result

                    cursor.execute(
                        """SELECT campaign_id,status,deadline,CURRENT_TIMESTAMP AS observed_at
                        FROM amazon_us.collection_campaign
                        WHERE tenant_id=%s AND campaign_id=%s FOR UPDATE""",
                        (self.tenant_id, campaign_id),
                    )
                    campaign = self._dict(cursor.fetchone())
                    if (
                        campaign is None or campaign["status"] != "active"
                        or campaign["deadline"] <= campaign["observed_at"]
                    ):
                        raise ReleaseGateUnavailable("campaign is not active")

                    cursor.execute(
                        """SELECT m.asin,m.subject_type,m.refresh_job_id,m.ordinal
                        FROM amazon_us.collection_campaign_member m
                        JOIN amazon_us.refresh_request r
                          ON r.tenant_id=m.tenant_id AND r.job_id=m.refresh_job_id
                        JOIN amazon_us.item_state s
                          ON s.tenant_id=m.tenant_id AND s.marketplace=m.marketplace
                         AND s.asin=m.asin AND s.subject_type=m.subject_type
                        WHERE m.tenant_id=%s AND m.campaign_id=%s
                          AND m.release_gate_id IS NULL
                          AND r.status='queued' AND s.status='pending'
                          AND (s.lease_expires_at IS NULL OR s.lease_expires_at<=CURRENT_TIMESTAMP)
                          AND (s.next_retry_at IS NULL OR s.next_retry_at<=CURRENT_TIMESTAMP)
                        ORDER BY m.ordinal
                        FOR UPDATE OF m,r,s SKIP LOCKED
                        LIMIT %s""",
                        (self.tenant_id, campaign_id, selection_limit),
                    )
                    members = [dict(row) for row in cursor.fetchall()]
                    if len(members) != selection_limit:
                        raise ReleaseGateUnavailable("insufficient pending campaign members")
                    selection_hash = _selection_hash(campaign_id, members)
                    criteria = {
                        "marketplace": "US",
                        "refresh_status": "queued",
                        "item_status": "pending",
                        "lease": "absent_or_expired",
                        "next_retry_at": "due",
                        "order": "campaign_member.ordinal",
                    }
                    cursor.execute(
                        """INSERT INTO amazon_us.collection_release_gate
                          (tenant_id,gate_id,campaign_id,idempotency_key,request_hash,release_id,status,
                           selection_limit,selection_criteria,selection_hash,selected_count,created_by)
                        VALUES(%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,%s,%s)""",
                        (
                            self.tenant_id, gate_id, campaign_id, idempotency_key, request_hash,
                            release_id, selection_limit, json.dumps(criteria), selection_hash,
                            len(members), created_by,
                        ),
                    )
                    for gate_ordinal, member in enumerate(members):
                        cursor.execute(
                            """UPDATE amazon_us.collection_campaign_member
                            SET release_gate_id=%s,release_gate_ordinal=%s
                            WHERE tenant_id=%s AND campaign_id=%s AND asin=%s AND subject_type=%s""",
                            (
                                gate_id, gate_ordinal, self.tenant_id, campaign_id,
                                member["asin"], member["subject_type"],
                            ),
                        )
                    cursor.execute(
                        "UPDATE amazon_us.collection_campaign SET release_gate_id=%s,updated_at=CURRENT_TIMESTAMP "
                        "WHERE tenant_id=%s AND campaign_id=%s",
                        (gate_id, self.tenant_id, campaign_id),
                    )
                    result = self._load(cursor, gate_id)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if result is None:
            raise RuntimeError("release gate was not persisted")
        result["duplicate"] = False
        return result


__all__ = ["CollectionReleaseGateStore", "ReleaseGateConflict", "ReleaseGateUnavailable"]
