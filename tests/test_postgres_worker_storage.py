from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_storage():
    spec = importlib.util.spec_from_file_location(
        "postgres_worker_storage", ROOT / "scripts" / "postgres_worker_storage.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Cursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.executed: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params)))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class Connection:
    def __init__(self, rows=None):
        self.cursor_instance = Cursor(rows)
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class ScriptedCursor(Cursor):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)

    def execute(self, sql, params=()):
        super().execute(sql, params)
        self.rows = list(self.responses.pop(0) if self.responses else [])


class ScriptedConnection(Connection):
    def __init__(self, responses):
        self.cursor_instance = ScriptedCursor(responses)
        self.commits = 0
        self.rollbacks = 0


class FailingCursor(Cursor):
    def execute(self, sql, params=()):
        super().execute(sql, params)
        if len(self.executed) == 2:
            raise RuntimeError("forced database failure")


class FailingConnection(Connection):
    def __init__(self):
        self.cursor_instance = FailingCursor()
        self.commits = 0
        self.rollbacks = 0


def test_initialize_manifest_is_tenant_and_subject_scoped():
    storage = load_storage()
    connection = Connection()
    repository = storage.PostgresWorkerStorage(
        "postgresql://example", tenant_id="tenant-a", subject_type="own", connect=lambda: connection
    )

    repository.initialize_manifest(
        [{"asin": "B00RCPDCQU", "url": "https://www.amazon.com/dp/B00RCPDCQU", "marketplace": "US"}]
    )

    assert connection.commits == 1
    statements = "\n".join(sql for sql, _ in connection.cursor_instance.executed)
    assert "asin_master" in statements
    assert "item_state" in statements
    params = [params for _, params in connection.cursor_instance.executed]
    assert any("tenant-a" in values and "own" in values for values in params)


def test_claim_task_is_atomic_and_returns_lease():
    storage = load_storage()
    connection = ScriptedConnection([[{"asin": "B00RCPDCQU", "status": "running", "lease_token": "token-1"}]])
    repository = storage.PostgresWorkerStorage(
        "postgresql://example", tenant_id="tenant-a", subject_type="own", connect=lambda: connection
    )

    task = repository.claim_task("worker-1", lease_seconds=120)

    assert task["asin"] == "B00RCPDCQU"
    sql = connection.cursor_instance.executed[0][0]
    assert "FOR UPDATE" in sql and "SKIP LOCKED" in sql
    assert "lease_token" in sql and "lease_owner" in sql and "lease_expires_at" in sql
    assert "tenant-a" in connection.cursor_instance.executed[0][1]
    assert "attempts=s.attempts+1" not in sql.replace(" ", "")


def test_claim_task_can_filter_to_product_stage():
    storage = load_storage()
    connection = ScriptedConnection([[{"asin": "B00RCPDCQU", "status": "running", "lease_token": "token-1"}]])
    repository = storage.PostgresWorkerStorage(
        "postgresql://example", tenant_id="tenant-a", subject_type="own", connect=lambda: connection
    )

    task = repository.claim_task("worker-1", lease_seconds=120, task_stage="product")

    assert task["asin"] == "B00RCPDCQU"
    sql, params = connection.cursor_instance.executed[0]
    assert "s.task_stage=%s" in sql.replace(" ", "")
    assert "product" in params


def test_manifest_transaction_rolls_back_as_one_unit_on_database_failure():
    storage = load_storage()
    connection = FailingConnection()
    repository = storage.PostgresWorkerStorage(
        "postgresql://example", tenant_id="tenant-a", subject_type="own", connect=lambda: connection
    )

    with pytest.raises(RuntimeError, match="forced database failure"):
        repository.initialize_manifest(
            [{"asin": "B00RCPDCQU", "url": "https://www.amazon.com/dp/B00RCPDCQU", "marketplace": "US"}]
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_update_task_rejects_wrong_lease_without_history():
    storage = load_storage()
    connection = ScriptedConnection([[]])
    repository = storage.PostgresWorkerStorage(
        "postgresql://example", tenant_id="tenant-a", subject_type="own", connect=lambda: connection
    )

    assert repository.update_task("B00RCPDCQU", "wrong", "worker-1", "succeeded") is False
    assert len(connection.cursor_instance.executed) == 1
    assert "state_history" not in connection.cursor_instance.executed[0][0]


def test_update_task_writes_history_for_lease_holder():
    storage = load_storage()
    connection = ScriptedConnection(
        [[{"status": "running"}], [{"status": "succeeded"}], []]
    )
    repository = storage.PostgresWorkerStorage(
        "postgresql://example", tenant_id="tenant-a", subject_type="own", connect=lambda: connection
    )

    assert repository.update_task("B00RCPDCQU", "token-1", "worker-1", "succeeded", reason="parsed") is True
    statements = "\n".join(sql for sql, _ in connection.cursor_instance.executed)
    assert "state_history" in statements
    assert connection.commits == 1


def test_reclaim_only_selects_expired_leases():
    storage = load_storage()
    connection = ScriptedConnection([[{"asin": "B00RCPDCQU", "from_status": "running", "to_status": "pending"}]])
    repository = storage.PostgresWorkerStorage(
        "postgresql://example", tenant_id="tenant-a", subject_type="own", connect=lambda: connection
    )

    assert repository.reclaim_expired_leases() == 1
    statements = "\n".join(sql for sql, _ in connection.cursor_instance.executed)
    assert "lease_expires_at" in statements
    assert "<= CURRENT_TIMESTAMP" in statements
    assert "state_history" in statements
    assert "UPDATE amazon_us.refresh_request" in statements
    assert "status='queued'" in statements.replace(" ", "")


def test_claim_refresh_request_is_tenant_and_subject_scoped():
    storage = load_storage()
    connection = ScriptedConnection([[{"job_id": "job-1", "status": "claimed"}]])
    repository = storage.PostgresWorkerStorage(
        "postgresql://example", tenant_id="tenant-a", subject_type="competitor", connect=lambda: connection
    )

    request = repository.claim_refresh_request()

    assert request["job_id"] == "job-1"
    sql = connection.cursor_instance.executed[0][0]
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "refresh_request" in sql
    assert "tenant-a" in connection.cursor_instance.executed[0][1]
    assert "competitor" in connection.cursor_instance.executed[0][1]


def test_claim_refresh_task_leases_requested_asin_in_same_transaction():
    storage = load_storage()
    connection = ScriptedConnection([[{"job_id": "job-1", "asin": "B00RCPDCQU", "status": "running", "lease_token": "token-1"}]])
    repository = storage.PostgresWorkerStorage(
        "postgresql://example", tenant_id="tenant-a", subject_type="own", connect=lambda: connection
    )

    task = repository.claim_refresh_task("worker-1", lease_seconds=120)

    assert task["job_id"] == "job-1"
    sql = connection.cursor_instance.executed[0][0]
    assert "refresh_request" in sql
    assert "item_state" in sql
    assert "FOR UPDATE" in sql and "SKIP LOCKED" in sql
    assert "lease_token" in sql
    assert "'pending',status" not in sql.replace(" ", "")
    assert "previous_status" in sql


def test_finish_refresh_request_is_tenant_scoped():
    storage = load_storage()
    connection = Connection()
    repository = storage.PostgresWorkerStorage(
        "postgresql://example", tenant_id="tenant-a", subject_type="own", connect=lambda: connection
    )

    repository.finish_refresh_request("job-1", "completed")

    sql, params = connection.cursor_instance.executed[0]
    assert "tenant_id=%s" in sql
    assert params[0] == "completed"
    assert params[-2:] == ("job-1", "tenant-a")


def test_enqueue_due_refreshes_uses_latest_snapshot_and_deduplicates_active_jobs():
    storage = load_storage()
    connection = ScriptedConnection([[{"job_id": "scheduled-1"}, {"job_id": "scheduled-2"}]])
    repository = storage.PostgresWorkerStorage(
        "postgresql://example", tenant_id="tenant-a", subject_type="own", connect=lambda: connection
    )

    assert repository.enqueue_due_refreshes(min_age_hours=24, limit=10) == 2
    sql, params = connection.cursor_instance.executed[0]
    assert "product_latest" in sql
    assert "NOT EXISTS" in sql
    assert "status IN ('queued','claimed')" in sql
    assert params == ("tenant-a", "own", 24, 10)


def test_save_product_result_is_one_lease_guarded_transaction():
    storage = load_storage()
    connection = ScriptedConnection([[{"status": "running"}]])
    repository = storage.PostgresWorkerStorage(
        "postgresql://example", tenant_id="tenant-a", subject_type="own", connect=lambda: connection
    )

    saved = repository.save_product_result(
        task={"asin": "B00RCPDCQU", "lease_token": "token-1", "lease_owner": "worker-1"},
        evidence={
            "run_id": "run-1", "url": "https://www.amazon.com/dp/B00RCPDCQU",
            "http_status": 200, "retrieved_at": "2026-08-28T00:00:00+00:00",
            "source_type": "http_html", "content_hash": "a" * 64, "context_json": {},
        },
        product={
            "canonical_url": "https://www.amazon.com/dp/B00RCPDCQU", "title": "Example",
            "bullets": ["one"], "specs": {"color": "black"}, "buy_box": {},
            "top_reviews": [], "aplus_present": False, "status": "product_done",
            "reported_rating_count": "", "reported_review_count": "",
        },
        media=[{"placement": "gallery", "ordinal": "", "is_primary": 1, "unique_key": "media-1"}],
        content_modules=[{"module_type": "bullet", "position": "", "order_index": "", "unique_key": "content-1"}],
        review_summary={"reported_rating_count": "", "reported_review_count": "", "status": "not_available"},
        next_status="succeeded",
        state_fields={"task_stage": "complete", "next_review_url": None, "next_review_page": None},
        reason="no_paginated_review_link",
    )

    assert saved is True
    statements = "\n".join(sql for sql, _ in connection.cursor_instance.executed)
    for table in ("collection_evidence", "product_snapshot", "media_asset", "content_module", "review_summary", "item_state", "state_history"):
        assert table in statements
    assert connection.commits == 1
    assert connection.rollbacks == 0
    executed = connection.cursor_instance.executed
    product_params = next(params for sql, params in executed if "INSERT INTO amazon_us.product_snapshot" in sql)
    media_params = next(params for sql, params in executed if "INSERT INTO amazon_us.media_asset" in sql)
    content_params = next(params for sql, params in executed if "INSERT INTO amazon_us.content_module" in sql)
    summary_params = next(params for sql, params in executed if "INSERT INTO amazon_us.review_summary" in sql)
    assert product_params[8:10] == (None, None)
    assert media_params[9] is None and media_params[10] is True
    assert content_params[4] == 0 and content_params[5] is None
    assert summary_params[3:5] == (None, None)
    compact_statements = statements.replace(" ", "")
    assert "last_error=NULL" in compact_statements
    assert "block_reason=NULL" in compact_statements
    assert "next_retry_at=NULL" in compact_statements


def test_save_failure_increments_attempts_and_releases_lease():
    storage = load_storage()
    connection = ScriptedConnection([[{"status": "running"}]])
    repository = storage.PostgresWorkerStorage(
        "postgresql://example", tenant_id="tenant-a", subject_type="own", connect=lambda: connection
    )

    assert repository.save_failure(
        task={"asin": "B00RCPDCQU", "lease_token": "token-1", "lease_owner": "worker-1"},
        reason="fetch_error",
        error="timeout",
    ) is True
    statements = "\n".join(sql for sql, _ in connection.cursor_instance.executed).replace(" ", "")
    assert "attempts=attempts+1" in statements
    assert "lease_token=NULL" in statements
    assert "state_history" in statements


def test_save_terminal_failure_exhausts_attempts_and_releases_lease():
    storage = load_storage()
    connection = ScriptedConnection([[{"status": "running"}]])
    repository = storage.PostgresWorkerStorage(
        "postgresql://example", tenant_id="tenant-a", subject_type="own", connect=lambda: connection
    )

    assert repository.save_failure(
        task={"asin": "B00RCPDCQU", "lease_token": "token-1", "lease_owner": "worker-1"},
        reason="asin_mismatch",
        error="asin_mismatch",
        terminal=True,
    ) is True
    statements = "\n".join(sql for sql, _ in connection.cursor_instance.executed).replace(" ", "")
    assert "attempts=max_attempts" in statements
    assert "attempts=attempts+1" not in statements
    assert "lease_token=NULL" in statements


def test_save_review_result_persists_page_records_summary_and_checkpoint():
    storage = load_storage()
    connection = ScriptedConnection([[{"status": "running"}]])
    repository = storage.PostgresWorkerStorage(
        "postgresql://example", tenant_id="tenant-a", subject_type="own", connect=lambda: connection
    )

    assert repository.save_review_result(
        task={"asin": "B00RCPDCQU", "lease_token": "token-1", "lease_owner": "worker-1"},
        evidence={"run_id": "run-1", "url": "https://www.amazon.com/reviews", "http_status": 200, "content_hash": "c" * 64, "context_json": {}},
        page={"page": 1, "url": "https://www.amazon.com/reviews", "status": "fetched", "next_url": None},
        records=[{"review_id": "R1", "title": "Good", "body": "Works", "unique_key": "US|B00RCPDCQU|R1"}],
        summary={"fetched_count": 1, "pages_fetched": 1, "next_page": None, "status": "exhausted"},
        next_status="succeeded",
        state_fields={"task_stage": "complete", "next_review_url": None, "next_review_page": None, "fetched_review_count": 1, "review_pages_fetched": 1},
        reason="reviews_exhausted",
    ) is True
    statements = "\n".join(sql for sql, _ in connection.cursor_instance.executed)
    for table in ("collection_evidence", "review_page_state", "review_record", "review_summary", "item_state", "state_history"):
        assert table in statements
    assert connection.commits == 1
