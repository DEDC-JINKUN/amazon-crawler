"""Independent PostgreSQL leases and persistence for Woot review jobs."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from import_woot_reviews import OBSERVATION_SQL, PROVIDER, RECORD_SQL, _canonical
from review_database_identity import DEFAULT_PRODUCTION_DATABASE, assert_review_database_identity


class ReviewStorageError(RuntimeError):
    pass


class PostgresReviewStorage:
    def __init__(
        self,
        connect: Callable[[], Any],
        tenant_id: str,
        provider: str = PROVIDER,
        expected_database: str = DEFAULT_PRODUCTION_DATABASE,
    ):
        self.connect = connect
        self.tenant_id = str(tenant_id or "").strip()
        self.provider = str(provider or "").strip()
        self.expected_database = str(expected_database or "").strip()
        if not self.tenant_id or not self.provider or not self.expected_database:
            raise ValueError("tenant_id, provider, and expected_database are required")

    @classmethod
    def from_environment(
        cls,
        tenant_id: str,
        *,
        dsn_env: str = "AMAZON_US_POSTGRES_DSN",
        provider: str = PROVIDER,
        expected_database: str = DEFAULT_PRODUCTION_DATABASE,
    ) -> "PostgresReviewStorage":
        dsn = os.environ.get(dsn_env, "").strip()
        if not dsn:
            raise ValueError(f"PostgreSQL DSN environment variable is required: {dsn_env}")
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required for review worker storage") from exc
        return cls(lambda: psycopg.connect(dsn), tenant_id, provider, expected_database)

    @staticmethod
    def _transaction(connection):
        transaction = getattr(connection, "transaction", None)
        return transaction() if callable(transaction) else nullcontext()

    @staticmethod
    def _dict(row: Any, columns: Iterable[str] | None = None) -> dict[str, Any] | None:
        if row is None:
            return None
        if isinstance(row, Mapping):
            return dict(row)
        if columns is None:
            raise ReviewStorageError("tuple row requires column names")
        return dict(zip(columns, row))

    def seed_jobs(self, asins: Iterable[str], *, max_attempts: int = 3) -> int:
        values = []
        seen = set()
        for raw in asins:
            asin = str(raw or "").strip().upper()
            if len(asin) != 10 or not asin.isalnum():
                raise ValueError(f"invalid ASIN: {raw}")
            if asin not in seen:
                seen.add(asin)
                values.append((self.tenant_id, asin, self.provider, max_attempts))
        connection = self.connect()
        try:
            with self._transaction(connection), connection.cursor() as cursor:
                assert_review_database_identity(cursor, self.expected_database)
                cursor.executemany(
                    """
                    INSERT INTO amazon_reviews.review_job_state
                      (tenant_id,marketplace,asin,provider,status,max_attempts)
                    VALUES (%s,'US',%s,%s,'pending',%s)
                    ON CONFLICT (tenant_id,marketplace,asin,provider) DO NOTHING
                    """,
                    values,
                )
            return len(values)
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    def schedule_jobs(self, asins: Iterable[str], *, due_at: datetime | None = None) -> int:
        """Make existing completed jobs eligible for a separate review refresh."""
        values = []
        seen = set()
        scheduled_at = due_at or datetime.now(timezone.utc)
        for raw in asins:
            asin = str(raw or "").strip().upper()
            if len(asin) != 10 or not asin.isalnum():
                raise ValueError(f"invalid ASIN: {raw}")
            if asin not in seen:
                seen.add(asin)
                values.append(asin)
        connection = self.connect()
        try:
            with self._transaction(connection), connection.cursor() as cursor:
                assert_review_database_identity(cursor, self.expected_database)
                cursor.execute(
                    """
                    UPDATE amazon_reviews.review_job_state
                    SET next_due_at=%s,updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=%s AND marketplace='US' AND provider=%s AND asin=ANY(%s)
                      AND status IN ('succeeded','source_limited')
                    """,
                    (scheduled_at, self.tenant_id, self.provider, values),
                )
                return int(getattr(cursor, "rowcount", 0) or 0)
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    def schedule_all_existing(self, *, due_at: datetime | None = None) -> int:
        """Schedule every non-running review job for this tenant/provider."""
        connection = self.connect()
        try:
            with self._transaction(connection), connection.cursor() as cursor:
                assert_review_database_identity(cursor, self.expected_database)
                cursor.execute(
                    """
                    UPDATE amazon_reviews.review_job_state
                    SET next_due_at=%s,updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=%s AND marketplace='US' AND provider=%s
                      AND status IN ('succeeded','source_limited')
                    """,
                    (due_at or datetime.now(timezone.utc), self.tenant_id, self.provider),
                )
                return int(getattr(cursor, "rowcount", 0) or 0)
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    def schedule_oldest(self, *, limit: int, min_age_hours: int = 24) -> int:
        """Schedule a bounded oldest-first slice without touching recent or already-due jobs."""
        if limit < 1 or limit > 500:
            raise ValueError("schedule limit must be between 1 and 500")
        if min_age_hours < 0:
            raise ValueError("min_age_hours must be non-negative")
        connection = self.connect()
        try:
            with self._transaction(connection), connection.cursor() as cursor:
                assert_review_database_identity(cursor, self.expected_database)
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"amazon_reviews:schedule:{self.tenant_id}:{self.provider}",),
                )
                cursor.execute(
                    """
                    SELECT count(*) FROM amazon_reviews.review_job_state
                    WHERE tenant_id=%s AND marketplace='US' AND provider=%s
                      AND (
                        (status IN ('pending','retryable')
                          AND (next_due_at IS NULL OR next_due_at <= CURRENT_TIMESTAMP))
                        OR status='running'
                        OR (status IN ('succeeded','source_limited')
                          AND next_due_at IS NOT NULL AND next_due_at <= CURRENT_TIMESTAMP)
                      )
                    """,
                    (self.tenant_id, self.provider),
                )
                due_row = cursor.fetchone()
                remaining = max(0, limit - int(due_row[0] if due_row else 0))
                if remaining == 0:
                    return 0
                cursor.execute(
                    """
                    WITH candidate AS (
                      SELECT job.tenant_id,job.marketplace,job.asin,job.provider
                      FROM amazon_reviews.review_job_state AS job
                      LEFT JOIN amazon_reviews.review_collection_run AS run
                        ON run.tenant_id=job.tenant_id AND run.run_id=job.last_run_id
                      WHERE job.tenant_id=%s AND job.marketplace='US' AND job.provider=%s
                        AND job.status IN ('succeeded','source_limited')
                        AND job.next_due_at IS NULL
                        AND COALESCE(run.finished_at,job.updated_at)
                            <= CURRENT_TIMESTAMP-(%s * INTERVAL '1 hour')
                      ORDER BY COALESCE(run.finished_at,job.updated_at),job.asin
                      FOR UPDATE OF job SKIP LOCKED LIMIT %s
                    )
                    UPDATE amazon_reviews.review_job_state AS job
                    SET next_due_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                    FROM candidate
                    WHERE job.tenant_id=candidate.tenant_id
                      AND job.marketplace=candidate.marketplace
                      AND job.asin=candidate.asin
                      AND job.provider=candidate.provider
                    """,
                    (self.tenant_id, self.provider, min_age_hours, remaining),
                )
                return int(getattr(cursor, "rowcount", 0) or 0)
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    def claim_job(self, worker_id: str, *, lease_seconds: int = 900) -> dict[str, Any] | None:
        worker = str(worker_id or "").strip()
        if not worker:
            raise ValueError("worker_id is required")
        if lease_seconds < 30:
            raise ValueError("lease_seconds must be at least 30")
        token = uuid.uuid4().hex
        connection = self.connect()
        try:
            with self._transaction(connection), connection.cursor() as cursor:
                assert_review_database_identity(cursor, self.expected_database)
                cursor.execute(
                    """
                    UPDATE amazon_reviews.review_job_state
                    SET status=CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'retryable' END,
                        lease_token=NULL,lease_owner=NULL,lease_expires_at=NULL,
                        last_error='lease_expired',updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=%s AND provider=%s AND status='running'
                      AND lease_expires_at <= CURRENT_TIMESTAMP
                    """,
                    (self.tenant_id, self.provider),
                )
                cursor.execute(
                    """
                    WITH candidate AS (
                      SELECT tenant_id,marketplace,asin,provider
                      FROM amazon_reviews.review_job_state
                      WHERE tenant_id=%s AND provider=%s
                        AND (
                          status IN ('pending','retryable')
                          OR (status IN ('succeeded','source_limited') AND next_due_at IS NOT NULL)
                        )
                        AND attempts < max_attempts
                        AND (next_due_at IS NULL OR next_due_at <= CURRENT_TIMESTAMP)
                      ORDER BY COALESCE(next_due_at,'1970-01-01'::timestamptz),updated_at,asin
                      FOR UPDATE SKIP LOCKED LIMIT 1
                    )
                    UPDATE amazon_reviews.review_job_state AS job
                    SET status='running',attempts=attempts+1,lease_token=%s,lease_owner=%s,
                        lease_expires_at=CURRENT_TIMESTAMP+(%s * INTERVAL '1 second'),
                        last_error=NULL,updated_at=CURRENT_TIMESTAMP
                    FROM candidate
                    WHERE job.tenant_id=candidate.tenant_id AND job.marketplace=candidate.marketplace
                      AND job.asin=candidate.asin AND job.provider=candidate.provider
                    RETURNING job.tenant_id,job.marketplace,job.asin,job.provider,job.status,
                              job.attempts,job.max_attempts,job.lease_token,job.lease_owner,
                              job.lease_expires_at,job.fetched_review_count
                    """,
                    (self.tenant_id, self.provider, token, worker, lease_seconds),
                )
                row = cursor.fetchone()
                columns = (
                    "tenant_id", "marketplace", "asin", "provider", "status", "attempts",
                    "max_attempts", "lease_token", "lease_owner", "lease_expires_at",
                    "fetched_review_count",
                )
                return self._dict(row, columns)
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ReviewStorageError(f"expected JSON object: {path.name}")
        return payload

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _resolve_evidence_path(run_dir: Path, relative_path: Any) -> Path:
        value = str(relative_path or "").strip()
        if not value:
            raise ReviewStorageError("evidence path is missing")
        resolved = (run_dir / value).resolve()
        try:
            resolved.relative_to(run_dir)
        except ValueError as exc:
            raise ReviewStorageError("evidence path escapes the run directory") from exc
        return resolved

    def _lock_job(self, cursor, task: Mapping[str, Any]) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT status,attempts,max_attempts FROM amazon_reviews.review_job_state
            WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND provider=%s
              AND lease_token=%s AND lease_owner=%s AND lease_expires_at > CURRENT_TIMESTAMP
            FOR UPDATE
            """,
            (
                self.tenant_id, task.get("asin"), self.provider,
                task.get("lease_token"), task.get("lease_owner"),
            ),
        )
        row = self._dict(cursor.fetchone(), ("status", "attempts", "max_attempts"))
        if row is None:
            raise ReviewStorageError("review job lease is missing or expired")
        return row

    @staticmethod
    def _evidence_rows(run_id: str, tenant_id: str, asin: str, run_dir: Path, manifest: list[dict[str, Any]], captured_at: str):
        rows = []
        checkpoints = []
        for index, item in enumerate(manifest, start=1):
            metadata_path = str(item.get("metadata_path") or f"evidence-{index}.json")
            evidence_key = metadata_path.replace("\\", "/")
            metadata_file = PostgresReviewStorage._resolve_evidence_path(run_dir, metadata_path)
            if not metadata_file.is_file():
                raise ReviewStorageError(f"evidence metadata file is missing: {evidence_key}")
            body_path = item.get("body_path")
            if body_path:
                body_file = PostgresReviewStorage._resolve_evidence_path(run_dir, body_path)
                if not body_file.is_file():
                    raise ReviewStorageError(f"evidence body file is missing: {body_path}")
                expected_bytes = item.get("body_bytes")
                if expected_bytes is None or body_file.stat().st_size != int(expected_bytes):
                    raise ReviewStorageError(f"evidence body byte count mismatch: {body_path}")
                expected_sha = str(item.get("body_sha256") or "")
                if len(expected_sha) != 64 or PostgresReviewStorage._sha256(body_file) != expected_sha:
                    raise ReviewStorageError(f"evidence body SHA-256 mismatch: {body_path}")
                raw_ref = str(body_file)
            else:
                raw_ref = str(metadata_file)
            rows.append((
                tenant_id, run_id, evidence_key, asin, item.get("query"), item.get("page"),
                item.get("url"), item.get("http_status"), item.get("body_bytes"),
                item.get("body_sha256"), raw_ref, _canonical(item.get("response_headers") or {}),
                item.get("error_code"), captured_at,
            ))
            status = "fetched"
            if item.get("http_status") in {403, 429}:
                status = "blocked"
            elif item.get("http_status") == 404:
                status = "query_truncated" if int(item.get("page") or 1) > 1 else "query_unavailable"
            elif item.get("error_code") in {"provider_blocked", "provider_rate_limited"}:
                status = "blocked"
            elif item.get("error_code") or (item.get("http_status") and int(item["http_status"]) >= 400):
                status = "failed"
            query_key = str(item.get("query") or "request")
            checkpoints.append((
                tenant_id, run_id, asin, query_key, int(item.get("page") or 1),
                item.get("star"), item.get("sort"), None, status,
                item.get("body_sha256"), captured_at,
            ))
        return rows, checkpoints

    @staticmethod
    def _record_pair(
        tenant_id: str,
        run_id: str,
        asin: str,
        review: Mapping[str, Any],
        observed_at: str,
        coverage: str,
        source_output_sha256: str,
    ):
        payload = {
            key: review.get(key) for key in (
                "provider", "marketplace", "asin", "review_id", "provider_review_id", "id_kind",
                "author", "rating", "title", "body", "review_url", "review_date", "locale",
                "verified", "vine", "helpful_votes", "review_images", "review_videos",
            )
        }
        record_hash = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
        source_hash = str(source_output_sha256 or "")
        if len(source_hash) != 64:
            raise ReviewStorageError("normalized output SHA-256 is invalid")
        record = (
            tenant_id, "US", asin, PROVIDER, review.get("review_id"), review.get("provider_review_id"),
            review.get("id_kind"), review.get("author"), review.get("rating"), review.get("title"),
            review.get("body"), review.get("review_url"), review.get("review_date") or review.get("origin_description"),
            review.get("locale"), review.get("verified"), review.get("vine"), review.get("helpful_votes"),
            _canonical(review.get("review_images") or []), _canonical(review.get("review_videos") or []),
            record_hash, run_id, run_id, observed_at, observed_at,
        )
        observation = (
            tenant_id, run_id, "US", asin, PROVIDER, review.get("review_id"), coverage,
            source_hash, observed_at, _canonical(review),
        )
        return record, observation

    def save_success(self, task: Mapping[str, Any], run_dir: str | Path) -> dict[str, Any]:
        root = Path(run_dir).resolve()
        receipt = self._load_json(root / "receipt.json")
        output_path = root / "reviews.normalized.json"
        output = self._load_json(output_path)
        manifest = json.loads((root / "evidence_manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, list):
            raise ReviewStorageError("evidence manifest must be a list")
        if self._sha256(output_path) != receipt.get("normalized_output_sha256"):
            raise ReviewStorageError("normalized review output hash mismatch")
        if self._sha256(root / "evidence_manifest.json") != receipt.get("evidence_manifest_sha256"):
            raise ReviewStorageError("evidence manifest hash mismatch")
        asin = str(task.get("asin") or "").upper()
        if output.get("asin") != asin or receipt.get("asin") != asin:
            raise ReviewStorageError("review output ASIN mismatch")
        run_id = str(receipt.get("run_id") or "")
        coverage = output.get("coverage") or {}
        coverage_status = str(coverage.get("status") or "partial")
        if coverage_status in {"complete", "no_reviews_verified"}:
            run_status = "success"
            next_status = "succeeded"
        elif coverage_status in {"partial", "source_limited"}:
            run_status = "partial_success"
            next_status = "source_limited"
        elif coverage_status == "blocked":
            run_status = "blocked"
            next_status = "blocked"
        else:
            raise ReviewStorageError(f"unsupported review coverage status: {coverage_status}")
        reviews = output.get("reviews")
        if not run_id or not isinstance(reviews, list):
            raise ReviewStorageError("review output schema is invalid")
        observed_at = str(receipt.get("finished_at") or datetime.now(timezone.utc).isoformat())
        response_bytes = sum(int(item.get("body_bytes") or 0) for item in manifest)
        evidence_rows, checkpoints = self._evidence_rows(
            run_id, self.tenant_id, asin, root, manifest, observed_at,
        )
        output_sha256 = str(receipt.get("normalized_output_sha256") or "")
        pairs = [
            self._record_pair(
                self.tenant_id, run_id, asin, item, observed_at, coverage_status, output_sha256,
            )
            for item in reviews
        ]
        connection = self.connect()
        try:
            with self._transaction(connection), connection.cursor() as cursor:
                assert_review_database_identity(cursor, self.expected_database)
                self._lock_job(cursor, task)
                cursor.execute(
                    """
                    INSERT INTO amazon_reviews.review_collection_run
                      (tenant_id,run_id,provider,marketplace,mode,status,started_at,finished_at,
                       request_count,response_body_bytes,successful_asins,failed_asins,unique_reviews,
                       coverage_counts,source_manifest_sha256,metadata_json)
                    VALUES (%s,%s,%s,'US',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb)
                    """,
                    (
                        self.tenant_id, run_id, self.provider, output.get("mode") or "adaptive",
                        run_status, receipt.get("started_at"), observed_at, len(manifest), response_bytes,
                        0 if next_status == "blocked" else 1, 1 if next_status == "blocked" else 0,
                        len(reviews),
                        _canonical({coverage_status: 1}), receipt.get("evidence_manifest_sha256"),
                        _canonical({"run_dir": str(root), "pdp_truth_written": False}),
                    ),
                )
                if evidence_rows:
                    cursor.executemany(
                        """
                        INSERT INTO amazon_reviews.review_raw_evidence
                          (tenant_id,run_id,evidence_key,asin,query_key,page,url,http_status,body_bytes,
                           body_sha256,raw_ref,response_headers,error_code,captured_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                        """,
                        evidence_rows,
                    )
                if checkpoints:
                    cursor.executemany(
                        """
                        INSERT INTO amazon_reviews.review_query_checkpoint
                          (tenant_id,run_id,asin,query_key,page,star,sort_order,paging_next,status,response_sha256,updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (tenant_id,run_id,asin,query_key,page) DO UPDATE SET
                          status=EXCLUDED.status,response_sha256=EXCLUDED.response_sha256,updated_at=EXCLUDED.updated_at
                        """,
                        checkpoints,
                    )
                if pairs:
                    cursor.executemany(RECORD_SQL, [item[0] for item in pairs])
                    cursor.executemany(OBSERVATION_SQL, [item[1] for item in pairs])
                cursor.execute(
                    """
                    UPDATE amazon_reviews.review_job_state
                    SET status=%s,attempts=0,last_run_id=%s,coverage_status=%s,fetched_review_count=%s,
                        last_error=NULL,next_due_at=NULL,lease_token=NULL,lease_owner=NULL,
                        lease_expires_at=NULL,updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND provider=%s
                      AND lease_token=%s AND lease_owner=%s AND lease_expires_at > CURRENT_TIMESTAMP
                    """,
                    (
                        next_status, run_id, coverage_status, len(reviews), self.tenant_id, asin,
                        self.provider, task.get("lease_token"), task.get("lease_owner"),
                    ),
                )
                if getattr(cursor, "rowcount", 1) != 1:
                    raise ReviewStorageError("review job completion lost its lease")
            return {
                "run_id": run_id, "asin": asin, "status": next_status,
                "coverage_status": coverage_status, "review_count": len(reviews),
                "request_count": len(manifest), "response_body_bytes": response_bytes,
            }
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    def save_failure(
        self,
        task: Mapping[str, Any],
        *,
        error_code: str,
        run_dir: str | Path | None = None,
        retry_after_seconds: int = 900,
    ) -> dict[str, Any]:
        asin = str(task.get("asin") or "").upper()
        code = str(error_code or "review_fetch_error")
        blocked = code in {"provider_blocked", "provider_rate_limited"}
        attempts = int(task.get("attempts") or 0)
        max_attempts = int(task.get("max_attempts") or 3)
        next_status = "blocked" if blocked else "failed" if attempts >= max_attempts else "retryable"
        next_due = None if next_status in {"blocked", "failed"} else datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)
        failure_run = None
        evidence_rows: list[tuple[Any, ...]] = []
        checkpoints: list[tuple[Any, ...]] = []
        root = Path(run_dir).resolve() if run_dir else None
        if root is not None:
            receipt_path = root / "receipt.failed.json"
            manifest_path = root / "evidence_manifest.json"
            if not receipt_path.is_file() or not manifest_path.is_file():
                raise ReviewStorageError("failed review run is missing its receipt or evidence manifest")
            failure_run = self._load_json(receipt_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, list):
                raise ReviewStorageError("evidence manifest must be a list")
            if failure_run.get("asin") != asin:
                raise ReviewStorageError("failed review receipt ASIN mismatch")
            if str(failure_run.get("error_code") or "") != code:
                raise ReviewStorageError("failed review receipt error code mismatch")
            plan = self._load_json(root / "plan.json")
            if plan.get("asin") != asin:
                raise ReviewStorageError("failed review plan ASIN mismatch")
            failure_mode = str(plan.get("mode") or "adaptive")
            captured_at = str(failure_run.get("finished_at") or datetime.now(timezone.utc).isoformat())
            evidence_rows, checkpoints = self._evidence_rows(
                str(failure_run.get("run_id") or ""), self.tenant_id, asin, root, manifest, captured_at,
            )
        connection = self.connect()
        try:
            with self._transaction(connection), connection.cursor() as cursor:
                assert_review_database_identity(cursor, self.expected_database)
                self._lock_job(cursor, task)
                if failure_run is not None:
                    run_id = str(failure_run.get("run_id") or "")
                    if not run_id:
                        raise ReviewStorageError("failed review receipt has no run_id")
                    response_bytes = sum(int(item.get("body_bytes") or 0) for item in manifest)
                    cursor.execute(
                        """
                        INSERT INTO amazon_reviews.review_collection_run
                          (tenant_id,run_id,provider,marketplace,mode,status,started_at,finished_at,
                           request_count,response_body_bytes,successful_asins,failed_asins,unique_reviews,
                           coverage_counts,source_manifest_sha256,metadata_json)
                        VALUES (%s,%s,%s,'US',%s,%s,%s,%s,%s,%s,0,1,0,%s::jsonb,%s,%s::jsonb)
                        """,
                        (
                            self.tenant_id, run_id, self.provider, failure_mode,
                            "blocked" if blocked else "failed", failure_run.get("started_at"),
                            failure_run.get("finished_at"), len(manifest), response_bytes,
                            _canonical({"blocked" if blocked else "failed": 1}), self._sha256(manifest_path),
                            _canonical({"run_dir": str(root), "error_code": code, "pdp_truth_written": False}),
                        ),
                    )
                    if evidence_rows:
                        cursor.executemany(
                            """
                            INSERT INTO amazon_reviews.review_raw_evidence
                              (tenant_id,run_id,evidence_key,asin,query_key,page,url,http_status,body_bytes,
                               body_sha256,raw_ref,response_headers,error_code,captured_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                            """,
                            evidence_rows,
                        )
                    if checkpoints:
                        cursor.executemany(
                            """
                            INSERT INTO amazon_reviews.review_query_checkpoint
                              (tenant_id,run_id,asin,query_key,page,star,sort_order,paging_next,status,response_sha256,updated_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            """,
                            checkpoints,
                        )
                cursor.execute(
                    """
                    UPDATE amazon_reviews.review_job_state
                    SET status=%s,last_run_id=COALESCE(%s,last_run_id),last_error=%s,next_due_at=%s,lease_token=NULL,lease_owner=NULL,
                        lease_expires_at=NULL,updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND provider=%s
                      AND lease_token=%s AND lease_owner=%s AND lease_expires_at > CURRENT_TIMESTAMP
                    """,
                    (
                        next_status, failure_run.get("run_id") if failure_run else None, code, next_due,
                        self.tenant_id, asin, self.provider,
                        task.get("lease_token"), task.get("lease_owner"),
                    ),
                )
                if getattr(cursor, "rowcount", 1) != 1:
                    raise ReviewStorageError("review job failure update lost its lease")
            return {
                "asin": asin, "status": next_status, "error_code": code,
                "next_due_at": next_due.isoformat() if next_due else None,
                "run_dir": str(root) if root else None,
            }
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
