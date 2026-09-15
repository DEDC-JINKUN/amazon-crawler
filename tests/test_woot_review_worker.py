from __future__ import annotations

import hashlib
import json
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from postgres_review_storage import PostgresReviewStorage
from woot_review_provider import WootProviderError
from woot_review_worker import consume_jobs


class FakeDatabase:
    def __init__(self):
        self.executed = []
        self.many = []
        self.fetchone_values = []
        self.rowcount = 1
        self.due_count = 0

    def connect(self):
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, database):
        self.database = database

    def transaction(self):
        return nullcontext()

    def cursor(self):
        return FakeCursor(self.database)

    def close(self):
        pass


class FakeCursor:
    def __init__(self, database):
        self.database = database
        self.rowcount = database.rowcount

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        assert sql.count("%s") == len(params), (sql, params)
        self.database.executed.append((" ".join(sql.split()), params))
        if "SELECT current_database()" in sql:
            self.database.fetchone_values.insert(0, ("postgres", "amazon-reviews-schema-v1"))
        elif "SELECT count(*) FROM amazon_reviews.review_job_state" in sql:
            self.database.fetchone_values.insert(0, (self.database.due_count,))
        self.rowcount = self.database.rowcount
        return self

    def executemany(self, sql, rows):
        values = list(rows)
        assert all(sql.count("%s") == len(row) for row in values), (sql, values[:1])
        self.database.many.append((" ".join(sql.split()), values))
        return self

    def fetchone(self):
        return self.database.fetchone_values.pop(0) if self.database.fetchone_values else None


def task():
    return {
        "asin": "B0ABC12345",
        "attempts": 1,
        "max_attempts": 3,
        "lease_token": "lease-token",
        "lease_owner": "worker-1",
    }


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_success_run(tmp_path: Path) -> Path:
    root = tmp_path / "woot-live-success"
    body = root / "evidence" / "q01.body"
    body.parent.mkdir(parents=True)
    body.write_bytes(b'{"Reviews":[]}')
    manifest = [{
        "metadata_path": "evidence/q01.json",
        "body_path": "evidence/q01.body",
        "query": "star-0_sort-0",
        "star": 0,
        "sort": 0,
        "page": 1,
        "url": "https://www.woot.com/review/Reviews/B0ABC12345?page=1",
        "http_status": 200,
        "body_bytes": len(body.read_bytes()),
        "body_sha256": hashlib.sha256(body.read_bytes()).hexdigest(),
        "response_headers": {"content-type": "application/json"},
        "error_code": None,
    }]
    write_json(root / "evidence" / "q01.json", manifest[0])
    output = {
        "asin": "B0ABC12345",
        "mode": "adaptive",
        "coverage": {"status": "partial", "claim_100_percent": False},
        "reviews": [{
            "provider": "woot_reviews", "marketplace": "US", "asin": "B0ABC12345",
            "review_id": "fallback-1", "provider_review_id": None, "id_kind": "fallback_hash",
            "author": "Buyer", "rating": 5, "title": "Good", "body": "Works",
            "review_url": None, "origin_description": "Reviewed September 1, 2026",
            "locale": "en-US", "verified": True, "vine": False, "helpful_votes": 1,
            "review_images": [], "review_videos": [],
        }],
    }
    write_json(root / "evidence_manifest.json", manifest)
    write_json(root / "reviews.normalized.json", output)
    output_sha = hashlib.sha256((root / "reviews.normalized.json").read_bytes()).hexdigest()
    manifest_sha = hashlib.sha256((root / "evidence_manifest.json").read_bytes()).hexdigest()
    write_json(root / "receipt.json", {
        "run_id": root.name, "asin": "B0ABC12345", "status": "success",
        "started_at": "2026-09-14T00:00:00Z", "finished_at": "2026-09-14T00:00:01Z",
        "normalized_output_sha256": output_sha, "evidence_manifest_sha256": manifest_sha,
    })
    return root


def make_failed_run(tmp_path: Path) -> Path:
    root = tmp_path / "woot-live-failed"
    manifest = [{
        "metadata_path": "evidence/q01.failed.json", "body_path": None,
        "query": "star-0_sort-0", "star": 0, "sort": 0, "page": 1,
        "url": "https://www.woot.com/review/Reviews/B0ABC12345?page=1",
        "http_status": 429, "body_bytes": None, "body_sha256": None,
        "response_headers": {}, "error_code": "provider_rate_limited",
    }]
    write_json(root / "evidence" / "q01.failed.json", manifest[0])
    write_json(root / "plan.json", {"asin": "B0ABC12345", "mode": "adaptive"})
    write_json(root / "evidence_manifest.json", manifest)
    write_json(root / "receipt.failed.json", {
        "run_id": root.name, "asin": "B0ABC12345", "status": "failed",
        "started_at": "2026-09-14T00:00:00Z", "finished_at": "2026-09-14T00:00:01Z",
        "error_code": "provider_rate_limited",
    })
    return root


def test_claim_uses_independent_skip_locked_queue_only():
    database = FakeDatabase()
    database.fetchone_values = [(
        "tenant-a", "US", "B0ABC12345", "woot_reviews", "running", 1, 3,
        "lease-token", "worker-1", "2026-09-14T00:15:00Z", 0,
    )]
    storage = PostgresReviewStorage(database.connect, "tenant-a")
    claimed = storage.claim_job("worker-1", lease_seconds=900)
    assert claimed["asin"] == "B0ABC12345"
    sql = "\n".join(item[0] for item in database.executed)
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'retryable' END" in sql
    assert "amazon_reviews.review_job_state" in sql
    assert "amazon_us.item_state" not in sql
    assert "amazon_us.product_snapshot" not in sql


def test_coverage_status_mapping_is_fail_closed():
    from import_woot_reviews import _job_status_for_coverage

    assert _job_status_for_coverage("complete") == "succeeded"
    assert _job_status_for_coverage("no_reviews_verified") == "succeeded"
    assert _job_status_for_coverage("partial") == "source_limited"
    assert _job_status_for_coverage("source_limited") == "source_limited"
    assert _job_status_for_coverage("blocked") == "blocked"
    with pytest.raises(Exception, match="unsupported coverage"):
        _job_status_for_coverage("unknown")


def test_schedule_jobs_reports_actual_updated_rows():
    database = FakeDatabase()
    database.rowcount = 1
    storage = PostgresReviewStorage(database.connect, "tenant-a")
    updated = storage.schedule_jobs(["B0ABC12345", "B0ABC12346"])
    assert updated == 1
    statement, params = database.executed[-1]
    assert "asin=ANY(%s)" in statement
    assert params[3] == ["B0ABC12345", "B0ABC12346"]


def test_schedule_oldest_is_bounded_and_excludes_already_due_jobs():
    database = FakeDatabase()
    database.rowcount = 5
    storage = PostgresReviewStorage(database.connect, "tenant-a")
    assert storage.schedule_oldest(limit=5, min_age_hours=168) == 5
    statement, params = database.executed[-1]
    assert "FOR UPDATE OF job SKIP LOCKED LIMIT %s" in statement
    assert "job.next_due_at IS NULL" in statement
    assert params == ("tenant-a", "woot_reviews", 168, 5)
    assert any("pg_advisory_xact_lock" in sql for sql, _params in database.executed)
    with pytest.raises(ValueError, match="between 1 and 500"):
        storage.schedule_oldest(limit=501)


def test_schedule_oldest_subtracts_existing_due_and_running_work():
    database = FakeDatabase()
    database.due_count = 3
    database.rowcount = 2
    storage = PostgresReviewStorage(database.connect, "tenant-a")
    assert storage.schedule_oldest(limit=5, min_age_hours=24) == 2
    assert database.executed[-1][1][-1] == 2

    full = FakeDatabase()
    full.due_count = 5
    storage = PostgresReviewStorage(full.connect, "tenant-a")
    assert storage.schedule_oldest(limit=5) == 0
    assert not any("WITH candidate" in sql for sql, _params in full.executed)


def test_schedule_oldest_due_count_matches_all_claimable_statuses():
    database = FakeDatabase()
    storage = PostgresReviewStorage(database.connect, "tenant-a")
    storage.schedule_oldest(limit=5)
    statement = next(
        sql for sql, _params in database.executed
        if "SELECT count(*) FROM amazon_reviews.review_job_state" in sql
    )
    assert "status IN ('succeeded','source_limited')" in statement
    assert "next_due_at IS NOT NULL AND next_due_at <= CURRENT_TIMESTAMP" in statement
    assert "status='running'" in statement


def test_manual_schedule_preserves_retry_backoff():
    database = FakeDatabase()
    storage = PostgresReviewStorage(database.connect, "tenant-a")
    storage.schedule_jobs(["B0ABC12345"])
    assert "status IN ('succeeded','source_limited')" in database.executed[-1][0]
    storage.schedule_all_existing()
    assert "status IN ('succeeded','source_limited')" in database.executed[-1][0]


def test_http_403_without_error_code_is_still_a_blocked_checkpoint(tmp_path):
    root = tmp_path / "run"
    body = root / "evidence" / "blocked.body"
    body.parent.mkdir(parents=True)
    body.write_bytes(b"blocked")
    metadata = root / "evidence" / "blocked.json"
    write_json(metadata, {"http_status": 403})
    manifest = [{
        "metadata_path": "evidence/blocked.json",
        "body_path": "evidence/blocked.body",
        "query": "star-0_sort-0", "page": 1, "star": 0, "sort": 0,
        "url": "https://www.woot.com/review/Reviews/B0ABC12345?page=1",
        "http_status": 403, "body_bytes": len(body.read_bytes()),
        "body_sha256": hashlib.sha256(body.read_bytes()).hexdigest(),
        "response_headers": {}, "error_code": None,
    }]
    _rows, checkpoints = PostgresReviewStorage._evidence_rows(
        "run-1", "tenant-a", "B0ABC12345", root, manifest, "2026-09-14T00:00:00Z",
    )
    assert checkpoints[0][8] == "blocked"


def test_save_success_persists_run_evidence_checkpoint_record_and_observation(tmp_path):
    database = FakeDatabase()
    database.fetchone_values = [("running", 1, 3)]
    storage = PostgresReviewStorage(database.connect, "tenant-a")
    result = storage.save_success(task(), make_success_run(tmp_path))
    assert result["status"] == "source_limited"
    assert result["review_count"] == 1
    sql = "\n".join(item[0] for item in database.executed)
    many = "\n".join(item[0] for item in database.many)
    for table in (
        "review_collection_run", "review_raw_evidence", "review_query_checkpoint",
        "review_record", "review_observation", "review_job_state",
    ):
        assert table in sql + many
    observation_rows = next(rows for statement, rows in database.many if "review_observation" in statement)
    receipt = json.loads((tmp_path / "woot-live-success" / "receipt.json").read_text(encoding="utf-8"))
    assert observation_rows[0][7] == receipt["normalized_output_sha256"]
    assert "amazon_us.item_state" not in sql + many
    assert "attempts=0" in sql


def test_save_failure_persists_failed_run_and_stops_retrying_on_rate_limit(tmp_path):
    database = FakeDatabase()
    database.fetchone_values = [("running", 1, 3)]
    storage = PostgresReviewStorage(database.connect, "tenant-a")
    result = storage.save_failure(
        task(), error_code="provider_rate_limited", run_dir=make_failed_run(tmp_path),
    )
    assert result["status"] == "blocked"
    sql = "\n".join(item[0] for item in database.executed)
    many = "\n".join(item[0] for item in database.many)
    assert "review_collection_run" in sql
    assert "review_raw_evidence" in many
    assert "review_query_checkpoint" in many
    assert "amazon_us.item_state" not in sql + many


def test_save_success_rejects_tampered_evidence_body(tmp_path):
    root = make_success_run(tmp_path)
    (root / "evidence" / "q01.body").write_bytes(b"tampered")
    database = FakeDatabase()
    storage = PostgresReviewStorage(database.connect, "tenant-a")
    with pytest.raises(Exception, match="byte count mismatch|SHA-256 mismatch"):
        storage.save_success(task(), root)
    assert database.executed == []


class WorkerStorage:
    tenant_id = "tenant-a"
    provider = "woot_reviews"

    def __init__(self, jobs, run_dir=None):
        self.jobs = list(jobs)
        self.run_dir = run_dir
        self.failures = []

    def claim_job(self, worker_id, lease_seconds):
        del worker_id, lease_seconds
        return self.jobs.pop(0) if self.jobs else None

    def save_success(self, claimed, run_dir):
        del claimed, run_dir
        return {
            "asin": "B0ABC12345", "status": "succeeded", "review_count": 2,
            "request_count": 3, "response_body_bytes": 120, "run_id": "run-1",
            "coverage_status": "partial",
        }

    def save_failure(self, claimed, *, error_code, run_dir=None):
        self.failures.append((claimed, error_code, run_dir))
        return {"asin": claimed["asin"], "status": "blocked", "error_code": error_code}


def test_worker_counts_success_without_pdp_side_effects(tmp_path):
    storage = WorkerStorage([task()])
    result = consume_jobs(
        storage, output_root=tmp_path, limit=2, worker_id="worker-1",
        plan_builder=lambda asin, **_kwargs: {"asin": asin},
        collector=lambda plan, output_root: {"run_dir": str(output_root / plan["asin"])},
    )
    assert result["status"] == "success"
    assert result["claimed"] == 1
    assert result["succeeded"] == 1
    assert result["reviews"] == 2
    assert result["pdp_truth_written"] is False


def test_worker_stops_batch_on_provider_block(tmp_path):
    storage = WorkerStorage([task(), task()])

    def blocked(_plan, *, output_root):
        del output_root
        error = WootProviderError("provider_blocked", "blocked")
        error.run_dir = str(tmp_path / "failed-run")
        raise error

    result = consume_jobs(
        storage, output_root=tmp_path, limit=2, worker_id="worker-1",
        plan_builder=lambda asin, **_kwargs: {"asin": asin}, collector=blocked,
    )
    assert result["status"] == "blocked"
    assert result["claimed"] == 1
    assert result["blocked"] == 1
    assert result["stopped_on_provider_block"] is True
    assert storage.failures[0][2].endswith("failed-run")


def test_worker_keeps_blocked_summary_after_an_earlier_retryable(tmp_path):
    storage = WorkerStorage([task(), task()])
    calls = 0

    def mixed(_plan, *, output_root):
        nonlocal calls
        del output_root
        calls += 1
        code = "network_error" if calls == 1 else "provider_rate_limited"
        raise WootProviderError(code, code)

    def save_failure(claimed, *, error_code, run_dir=None):
        del run_dir
        status = "blocked" if error_code == "provider_rate_limited" else "retryable"
        return {"asin": claimed["asin"], "status": status, "error_code": error_code}

    storage.save_failure = save_failure
    result = consume_jobs(
        storage, output_root=tmp_path, limit=2, worker_id="worker-1",
        plan_builder=lambda asin, **_kwargs: {"asin": asin}, collector=mixed,
    )
    assert result["retryable"] == 1
    assert result["blocked"] == 1
    assert result["status"] == "blocked"
    assert result["stopped_on_provider_block"] is True


def test_worker_does_not_relabel_database_save_error_as_provider_failure(tmp_path):
    storage = WorkerStorage([task()])

    def broken_save(_claimed, _run_dir):
        raise RuntimeError("database unavailable")

    storage.save_success = broken_save
    with pytest.raises(RuntimeError, match="database unavailable"):
        consume_jobs(
            storage, output_root=tmp_path, limit=1, worker_id="worker-1",
            plan_builder=lambda asin, **_kwargs: {"asin": asin},
            collector=lambda plan, output_root: {"run_dir": str(output_root / plan["asin"])},
        )
    assert storage.failures == []


def test_worker_cli_rejects_multiple_schedule_scopes(monkeypatch, capsys):
    import woot_review_worker

    class FakeStorage:
        @classmethod
        def from_environment(cls, *_args, **_kwargs):
            return cls()

    monkeypatch.setattr(woot_review_worker, "PostgresReviewStorage", FakeStorage)
    result = woot_review_worker.main([
        "--tenant-id", "tenant-a", "--schedule-existing",
        "--schedule-asins", "B0ABC12345", "--schedule-only",
    ])
    assert result == 2
    assert "choose one" in capsys.readouterr().err
