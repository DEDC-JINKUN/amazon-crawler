"""PostgreSQL task storage for the production Amazon worker.

The worker-facing API is deliberately small.  It owns task claiming and
lease checks; parsing and transport remain in the existing worker layers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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

    def claim_task(self, worker_id: str, *, lease_seconds: int | None = None) -> dict[str, Any] | None:
        """Atomically claim one eligible task using row locking and a lease token."""
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
                        WITH candidate AS (
                          SELECT s.tenant_id, s.marketplace, s.asin, s.subject_type
                          FROM amazon_us.item_state s
                          JOIN amazon_us.asin_master m USING (tenant_id, marketplace, asin, subject_type)
                          WHERE s.tenant_id=%s AND s.marketplace='US' AND s.subject_type=%s
                            AND (s.lease_expires_at IS NULL OR s.lease_expires_at <= CURRENT_TIMESTAMP)
                            AND ((s.status IN ('pending','reviews_pending'))
                              OR (s.status='failed' AND s.attempts < s.max_attempts))
                            AND (s.next_retry_at IS NULL OR s.next_retry_at <= CURRENT_TIMESTAMP)
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
                        """,
                        (self.tenant_id, self.subject_type, token, worker_id.strip(), seconds),
                    )
                    task = self._as_dict(cursor.fetchone())
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return task

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
                    assignments = ["status=%s", "attempts=0"]
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
    ) -> bool:
        """Persist one failed or deferred action while releasing its lease."""
        if next_status not in self.VALID_STATUSES:
            raise ValueError(f"invalid task status: {next_status}")
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
                    if increment_attempts:
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
