from __future__ import annotations

import hashlib
import importlib.util
import json
import threading
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collection_console.py"


def load_module():
    spec = importlib.util.spec_from_file_location("collection_console_woot_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def build_aggregate(root: Path) -> Path:
    root.mkdir()
    statuses = [
        {"asin": "B0AAA00001", "status": "success", "coverage_status": "partial", "fetched_unique_count": 2},
        {"asin": "B0AAA00002", "status": "success", "coverage_status": "source_limited", "fetched_unique_count": 1},
    ]
    reviews = [
        {
            "asin": "B0AAA00001", "review_id": "woot-fallback:1", "rating": 5,
            "title": "Good", "body": "Works", "author": "A", "review_date": "September 1, 2026",
            "verified": True, "vine": False, "helpful_votes": 2,
            "review_images": ["https://example.com/a.jpg"], "review_videos": [],
            "coverage_status": "partial", "id_kind": "fallback_hash", "source_output_sha256": "b" * 64,
        },
        {
            "asin": "B0AAA00001", "review_id": "woot-fallback:2", "rating": 1,
            "title": "Bad", "body": "Broke", "author": "B", "review_date": "August 1, 2026",
            "verified": False, "vine": True, "helpful_votes": 0,
            "review_images": [], "review_videos": ["https://example.com/v.mp4"],
            "coverage_status": "partial", "id_kind": "fallback_hash", "source_output_sha256": "c" * 64,
        },
        {
            "asin": "B0AAA00002", "review_id": "woot-fallback:3", "rating": 4,
            "title": "Fine", "body": "Okay", "author": "C", "review_date": "July 1, 2026",
            "verified": True, "vine": False, "helpful_votes": 1,
            "review_images": [], "review_videos": [],
            "coverage_status": "source_limited", "id_kind": "fallback_hash", "source_output_sha256": "d" * 64,
        },
    ]
    summary = {
        "schema_version": "woot-review-collection-summary-v1",
        "source_filename": "Asin list.xlsx", "source_sha256": "a" * 64,
        "source_unique_asins": 2, "successful_asins": 2, "failed_asins": 0,
        "pending_asins": 0, "unique_reviews": 3,
        "coverage_status_counts": {"partial": 1, "source_limited": 1},
        "claim_100_percent_asins": 0,
    }
    write_jsonl(root / "asin_status.jsonl", statuses)
    write_jsonl(root / "reviews.jsonl", reviews)
    write_json(root / "collection_summary.json", summary)
    write_json(root / "pending_asins.json", {"count": 0, "asins": []})
    files = []
    for name in ("asin_status.jsonl", "collection_summary.json", "pending_asins.json", "reviews.jsonl"):
        path = root / name
        files.append({
            "path": name, "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    write_json(root / "manifest.json", {"schema_version": "woot-review-aggregate-manifest-v1", "files": files})
    return root


def refresh_manifest(root: Path) -> None:
    files = []
    for name in ("asin_status.jsonl", "collection_summary.json", "pending_asins.json", "reviews.jsonl"):
        path = root / name
        files.append({
            "path": name, "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    write_json(root / "manifest.json", {"schema_version": "woot-review-aggregate-manifest-v1", "files": files})


def test_file_repository_validates_manifest_and_builds_bounded_views(tmp_path):
    module = load_module()
    repository = module.WootReviewConsoleRepository(build_aggregate(tmp_path / "aggregate"))
    overview = repository.load_overview()
    assert overview["availability"] == "available"
    assert overview["summary"]["successful_asins"] == 2
    assert overview["derived"] == {
        "zero_review_asins": 0, "with_images": 1, "with_videos": 1,
        "verified_reviews": 2, "vine_reviews": 1,
    }
    listed = repository.list_asins(query="AAA", coverage="source_limited", limit=10, offset=0)
    assert listed["total"] == 1
    assert listed["items"][0]["asin"] == "B0AAA00002"


class ReviewCursor:
    def __init__(self):
        self.sql = ""
        self.params = ()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.sql = " ".join(str(sql).split())
        self.params = params

    def fetchone(self):
        if "to_regclass('amazon_reviews.review_job_state')" in self.sql:
            return {"job_relation": "amazon_reviews.review_job_state", "record_relation": "amazon_reviews.review_record"}
        if "source_unique_asins" in self.sql:
            return {
                "source_unique_asins": 2, "successful_asins": 1, "pending_asins": 1,
                "failed_asins": 0, "zero_review_asins": 0,
                "job_observed_at": "2026-09-15T01:00:00+00:00",
            }
        if "unique_reviews" in self.sql:
            return {
                "unique_reviews": 3, "with_images": 1, "with_videos": 1,
                "verified_reviews": 2, "vine_reviews": 1,
                "record_observed_at": "2026-09-15T02:00:00+00:00",
            }
        if "FROM amazon_reviews.review_job_state" in self.sql and "COUNT(*) AS count" in self.sql:
            return {"count": 2}
        if "FROM amazon_reviews.review_job_state" in self.sql and "asin,status" in self.sql:
            return {
                "asin": "B0AAA00001", "status": "source_limited",
                "coverage_status": "partial", "fetched_unique_count": 2,
                "last_run_id": "run-1", "error_code": None,
                "updated_at": "2026-09-15T01:00:00+00:00",
            }
        if "FROM amazon_reviews.review_record" in self.sql and "COUNT(*) AS count" in self.sql:
            return {"count": 2}
        raise AssertionError(f"unexpected fetchone SQL: {self.sql}")

    def fetchall(self):
        if "GROUP BY coverage_status" in self.sql:
            return [
                {"coverage_status": "partial", "count": 1},
                {"coverage_status": "source_limited", "count": 1},
            ]
        if "FROM amazon_reviews.review_job_state" in self.sql and "ORDER BY asin" in self.sql:
            return [
                {
                    "asin": "B0AAA00001", "status": "source_limited",
                    "coverage_status": "partial", "fetched_unique_count": 2,
                    "last_run_id": "run-1", "error_code": None,
                    "updated_at": "2026-09-15T01:00:00+00:00",
                },
                {
                    "asin": "B0AAA00002", "status": "retryable",
                    "coverage_status": "source_limited", "fetched_unique_count": 1,
                    "last_run_id": "run-2", "error_code": "network_error",
                    "updated_at": "2026-09-15T01:30:00+00:00",
                },
            ]
        if "FROM amazon_reviews.review_record" in self.sql and "ORDER BY last_seen_at" in self.sql:
            return [
                {
                    "review_id": "woot-fallback:1", "provider_review_id": None,
                    "id_kind": "fallback_hash", "author": "A", "rating": 5,
                    "title": "Good", "body": "Works", "review_url": "https://example.test/review/1",
                    "review_date": "September 1, 2026", "locale": "US", "verified": True,
                    "vine": False, "helpful_votes": 2, "review_images": [], "review_videos": [],
                    "first_seen_at": "2026-09-14T01:00:00+00:00", "last_seen_at": "2026-09-15T01:00:00+00:00",
                }
            ]
        raise AssertionError(f"unexpected fetchall SQL: {self.sql}")


class ReviewConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return ReviewCursor()


def test_postgres_repository_builds_tenant_scoped_bounded_views():
    module = load_module()
    repository = module.PostgresReviewConsoleRepository("postgresql://fixture", "tenant-a")
    repository._connect = lambda: ReviewConnection()
    overview = repository.load_overview()
    assert overview["availability"] == "available"
    assert overview["source_type"] == "postgres"
    assert overview["tenant_id"] == "tenant-a"
    assert overview["summary"] == {
        "source_unique_asins": 2, "successful_asins": 1, "pending_asins": 1,
        "failed_asins": 0, "unique_reviews": 3,
        "coverage_status_counts": {"partial": 1, "source_limited": 1},
        "claim_100_percent_asins": 0,
    }
    listed = repository.list_asins(query="AAA", coverage="partial", limit=10, offset=0)
    assert listed["total"] == 2
    assert listed["items"][1]["error_code"] == "network_error"
    detail = repository.load_asin("B0AAA00001", limit=1, offset=0, rating=5, verified=True)
    assert detail["total"] == 2
    assert detail["next_offset"] == 1
    assert detail["items"][0]["body"] == "Works"


def test_postgres_review_failure_disables_only_review_surface():
    module = load_module()
    repository = module.PostgresReviewConsoleRepository("postgresql://fixture", "tenant-a")
    repository._connect = lambda: (_ for _ in ()).throw(OSError("database unavailable"))
    assert repository.load_overview()["error_code"] == "review_database_unavailable"
    assert repository.list_asins(limit=20, offset=0)["items"] == []
    assert repository.load_asin("B0AAA00001", limit=20, offset=0)["items"] == []


def test_file_repository_pages_and_filters_one_asin_without_cross_asin_leak(tmp_path):
    module = load_module()
    repository = module.WootReviewConsoleRepository(build_aggregate(tmp_path / "aggregate"))
    first = repository.load_asin("B0AAA00001", limit=1, offset=0)
    assert first["total"] == 2
    assert first["next_offset"] == 1
    assert [item["review_id"] for item in first["items"]] == ["woot-fallback:1"]
    second = repository.load_asin("B0AAA00001", limit=10, offset=0, rating=1, verified=False)
    assert second["total"] == 1
    assert second["items"][0]["body"] == "Broke"
    assert all(item["asin"] == "B0AAA00001" for item in second["items"])


def test_unbound_file_repository_fails_closed_when_http_tenant_is_selected(tmp_path):
    module = load_module()
    repository = module.WootReviewConsoleRepository(build_aggregate(tmp_path / "aggregate"))
    scoped = repository.for_tenant("tenant-b")
    assert scoped.load_overview()["availability"] == "unavailable"
    assert scoped.load_overview()["tenant_id"] == "tenant-b"


def test_review_repositories_reject_excessive_offsets(tmp_path):
    module = load_module()
    repositories = [
        module.PostgresReviewConsoleRepository("postgresql://fixture", "tenant-a"),
        module.WootReviewConsoleRepository(build_aggregate(tmp_path / "aggregate"), "tenant-a"),
    ]
    for repository in repositories:
        with pytest.raises(ValueError, match="offset must be between"):
            repository.list_asins(limit=20, offset=100_001)
        with pytest.raises(ValueError, match="offset must be between"):
            repository.load_asin("B0AAA00001", limit=20, offset=100_001)


def test_explicit_file_source_requires_tenant_binding(tmp_path, monkeypatch):
    module = load_module()
    root = build_aggregate(tmp_path / "aggregate")
    monkeypatch.setenv("AMAZON_US_POSTGRES_DSN", "postgresql://fixture")
    assert module.main(["--review-source", "files", "--woot-review-dir", str(root)]) == 1


def test_invalid_manifest_disables_only_review_surface(tmp_path):
    module = load_module()
    root = build_aggregate(tmp_path / "aggregate")
    (root / "reviews.jsonl").write_text("corrupted\n", encoding="utf-8")
    repository = module.WootReviewConsoleRepository(root)
    assert repository.load_overview()["availability"] == "unavailable"
    assert repository.load_asin("B0AAA00001", limit=20, offset=0)["items"] == []


def test_semantically_duplicated_review_id_is_rejected_even_with_valid_hashes(tmp_path):
    module = load_module()
    root = build_aggregate(tmp_path / "aggregate")
    rows = [json.loads(line) for line in (root / "reviews.jsonl").read_text(encoding="utf-8").splitlines()]
    rows[1]["review_id"] = rows[0]["review_id"]
    write_jsonl(root / "reviews.jsonl", rows)
    refresh_manifest(root)
    repository = module.WootReviewConsoleRepository(root)
    assert repository.load_overview()["availability"] == "unavailable"
    assert repository.load_overview()["error_code"] == "review_data_invalid"


class ProductRepository:
    tenant_id = "tenant-console"

    def load_identity(self):
        return {"tenant_id": self.tenant_id, "task_count": 0}


class TenantReviewRepository:
    def __init__(self, tenant_id=""):
        self.tenant_id = tenant_id

    def for_tenant(self, tenant_id):
        return type(self)(tenant_id)

    def load_overview(self):
        return {"availability": "available", "tenant_id": self.tenant_id, "summary": {}, "derived": {}}

    def list_asins(self, **_kwargs):
        return {"availability": "available", "tenant_id": self.tenant_id, "total": 0, "limit": 50, "offset": 0, "items": []}

    def load_asin(self, asin, **_kwargs):
        return {"availability": "available", "tenant_id": self.tenant_id, "asin": asin, "status": None, "total": 0, "limit": 20, "offset": 0, "next_offset": None, "items": []}


def test_console_exposes_read_only_review_summary_list_and_page_routes(tmp_path):
    module = load_module()
    reviews = module.WootReviewConsoleRepository(
        build_aggregate(tmp_path / "aggregate"), "tenant-console"
    )
    server = module.ConsoleServer(
        ("127.0.0.1", 0), ProductRepository(), review_repository=reviews,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(f"{base}/api/reviews/overview", timeout=2) as response:
            assert json.loads(response.read())["summary"]["unique_reviews"] == 3
        with urllib.request.urlopen(f"{base}/api/reviews/items?coverage=partial&limit=1&offset=0", timeout=2) as response:
            payload = json.loads(response.read())
            assert payload["total"] == 1
            assert len(payload["items"]) == 1
        with urllib.request.urlopen(f"{base}/api/reviews/B0AAA00001?limit=1&offset=1", timeout=2) as response:
            payload = json.loads(response.read())
            assert payload["total"] == 2
            assert payload["items"][0]["review_id"] == "woot-fallback:2"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_console_scopes_review_routes_to_selected_tenant():
    module = load_module()
    server = module.ConsoleServer(
        ("127.0.0.1", 0), ProductRepository(), review_repository=TenantReviewRepository(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(f"{base}/api/reviews/overview?tenant=tenant-b", timeout=2) as response:
            assert json.loads(response.read())["tenant_id"] == "tenant-b"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_static_console_has_review_summary_table_and_paginated_detail_contract():
    html = (ROOT / "console" / "index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "console" / "app.js").read_text(encoding="utf-8")
    assert 'id="reviewSummaryPanel"' in html
    assert 'id="reviewRows"' in html
    assert "/api/reviews/overview" in javascript
    assert "/api/reviews/items" in javascript
    assert "/api/reviews/" in javascript
    assert "review_images" in javascript
    assert "review_videos" in javascript
    assert "success:'Collected'" in javascript
    assert "coverage_status:'覆盖状态'" in javascript
    assert "source_type === 'postgres'" in javascript
    assert "正在读取评论数据" in html
    assert "ASIN已完成" in html
    assert "source_limited:'Source limited'" in javascript
    assert "retryable:'Retryable'" in javascript
    assert "reviewRequest: 0" in javascript
    assert "requestId !== state.reviewRequest" in javascript
    assert "tenantAtStart !== state.tenant" in javascript
    assert "container.dataset.requestId !== requestId" in javascript


def test_windows_console_launcher_passes_only_an_explicit_review_directory():
    source = (ROOT / "run_console_windows.ps1").read_text(encoding="utf-8-sig")
    assert "[string]$WootReviewDir = ''" in source
    assert "Resolve-Path (Join-Path $projectRoot $WootReviewDir)" in source
    assert "'--woot-review-dir', $resolvedWootReviews" in source
    assert "[string]$ReviewSource = 'auto'" in source
    assert "'--review-source', $ReviewSource" in source
