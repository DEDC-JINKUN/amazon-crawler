from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_worker():
    spec = importlib.util.spec_from_file_location("amazon_us_worker_postgres_test", ROOT / "scripts" / "amazon_us_worker.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Storage:
    def __init__(self):
        self.claims = 0
        self.saved = []

    def claim_task(self, worker_id, lease_seconds=None):
        self.claims += 1
        if self.claims > 1:
            return None
        return {
            "asin": "B00RCPDCQU", "marketplace": "US", "url": "https://www.amazon.com/dp/B00RCPDCQU",
            "status": "running", "task_stage": "product", "lease_token": "token-1", "lease_owner": worker_id,
            "reported_review_count": 0, "fetched_review_count": 0, "review_pages_fetched": 0,
        }

    def save_product_result(self, **payload):
        self.saved.append(payload)
        return True

    def save_failure(self, **payload):
        self.saved.append(payload)
        return True


class Adapter:
    source_type = "http_html"
    last_transfer_bytes = 321
    last_retry_after_seconds = None

    def fetch(self, url):
        return """
        <html><head><link rel="canonical" href="https://www.amazon.com/dp/B00RCPDCQU"></head><body>
          <input id="ASIN" value="B00RCPDCQU"><span id="productTitle">Example</span>
          <div id="averageCustomerReviewsAnchor">Reviews</div>
        </body></html>
        """, 200


class ReviewStorage(Storage):
    def claim_task(self, worker_id, lease_seconds=None):
        self.claims += 1
        if self.claims > 1:
            return None
        return {
            "asin": "B00RCPDCQU", "marketplace": "US", "url": "https://www.amazon.com/dp/B00RCPDCQU",
            "status": "running", "task_stage": "reviews", "next_review_page": 1,
            "next_review_url": "https://www.amazon.com/product-reviews/B00RCPDCQU",
            "lease_token": "token-1", "lease_owner": worker_id, "reported_review_count": 1,
            "reported_rating_count": 1, "reported_count_source": "header",
            "fetched_review_count": 0, "review_pages_fetched": 0,
        }

    def save_review_result(self, **payload):
        self.saved.append(payload)
        return True


class PortalReviewStorage(ReviewStorage):
    def claim_task(self, worker_id, lease_seconds=None):
        task = super().claim_task(worker_id, lease_seconds)
        if task is not None:
            task["next_review_url"] = "https://www.amazon.com/portal/customer-reviews/B00RCPDCQU"
        return task


class ReviewAdapter(Adapter):
    def fetch(self, url):
        return """
        <div data-hook="review" id="R1">
          <i data-hook="review-star-rating">5.0 out of 5 stars</i>
          <a data-hook="review-title">Good</a><span data-hook="review-body">Works</span>
        </div>
        """, 200


class AlternateReviewAdapter(ReviewAdapter):
    def __init__(self):
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        if "/portal/customer-reviews/" in url:
            return "<html><body>No review cards here</body></html>", 200
        return super().fetch(url)


class EmptyReviewAdapter(Adapter):
    def __init__(self):
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return "<html><body>No review cards here</body></html>", 200


class RefreshStorage(Storage):
    def __init__(self):
        super().__init__()
        self.finished = []

    def claim_refresh_task(self, worker_id, lease_seconds=None):
        if self.claims:
            return None
        self.claims += 1
        return {
            "job_id": "job-1", "asin": "B00RCPDCQU", "marketplace": "US",
            "url": "https://www.amazon.com/dp/B00RCPDCQU", "status": "running",
            "task_stage": "product", "lease_token": "token-1", "lease_owner": worker_id,
            "reported_review_count": 0, "fetched_review_count": 0, "review_pages_fetched": 0,
        }

    def claim_task(self, worker_id, lease_seconds=None):
        return None

    def finish_refresh_request(self, job_id, status):
        self.finished.append((job_id, status))


class ProductOnlyStorage(Storage):
    def claim_refresh_task(self, worker_id, lease_seconds=None):
        raise AssertionError("product-only batches must not claim refresh tasks")

    def claim_task(self, worker_id, lease_seconds=None, task_stage=None):
        assert task_stage == "product"
        return super().claim_task(worker_id, lease_seconds)


class ReviewsOnlyStorage(ReviewStorage):
    def claim_refresh_task(self, worker_id, lease_seconds=None):
        raise AssertionError("reviews-only batches must not claim refresh tasks")

    def claim_task(self, worker_id, lease_seconds=None, task_stage=None):
        assert task_stage == "reviews"
        return super().claim_task(worker_id, lease_seconds)


class MalformedReviewsOnlyStorage(Storage):
    def __init__(self, *, task_stage, next_review_url):
        super().__init__()
        self.task_stage = task_stage
        self.next_review_url = next_review_url

    def claim_refresh_task(self, worker_id, lease_seconds=None):
        raise AssertionError("reviews-only batches must not claim refresh tasks")

    def claim_task(self, worker_id, lease_seconds=None, task_stage=None):
        assert task_stage == "reviews"
        self.claims += 1
        if self.claims > 1:
            return None
        return {
            "asin": "B00RCPDCQU", "marketplace": "US",
            "url": "https://www.amazon.com/dp/B00RCPDCQU",
            "status": "running", "task_stage": self.task_stage,
            "next_review_page": 1, "next_review_url": self.next_review_url,
            "lease_token": "token-1", "lease_owner": worker_id,
            "reported_review_count": 1, "fetched_review_count": 0, "review_pages_fetched": 0,
        }


class NoFetchAdapter(Adapter):
    def fetch(self, url):
        raise AssertionError("malformed reviews-only tasks must fail before network fetch")


class IdentityMismatchStorage(Storage):
    def claim_task(self, worker_id, lease_seconds=None, task_stage=None):
        self.claims += 1
        if self.claims > 1:
            return None
        return {
            "asin": "B0B9ZFDZNJ", "marketplace": "US",
            "url": "https://www.amazon.com/dp/B0B9ZFDZNJ",
            "status": "running", "task_stage": "product",
            "lease_token": "token-1", "lease_owner": worker_id,
        }


class IdentityMismatchAdapter(Adapter):
    def fetch(self, _url):
        return """
          <html><head><link rel='canonical' href='https://www.amazon.com/dp/B0B9ZFZZZZ'></head>
          <body><input id='ASIN' value='B0B9ZFZZZZ'><span id='productTitle'>Sibling</span>
          <script>var x={parentAsin:'B0PARENT01',landingAsin:'B0B9ZFZZZZ',
          dimensionValuesDisplayData:{'B0B9ZFDZNJ':['A'],'B0B9ZFZZZZ':['B']}};</script></body></html>
        """, 200


class PostalFallbackAdapter(Adapter):
    def __init__(self):
        self.calls = []

    def fetch(self, url):
        self.calls.append("http")
        return """
        <html><head><link rel="canonical" href="https://www.amazon.com/dp/B00RCPDCQU"></head><body>
          <input id="ASIN" value="B00RCPDCQU"><span id="productTitle">Example</span>
          <div id="desktop_buybox">Delivering to Portland 97230 - Update location</div>
        </body></html>
        """, 200

    def fetch_browser(self, url):
        self.calls.append("browser")
        self.source_type = "selenium_dom"
        self.last_browser_context_confirmed = True
        return """
        <html><head><link rel="canonical" href="https://www.amazon.com/dp/B00RCPDCQU"></head><body>
          <input id="ASIN" value="B00RCPDCQU"><span id="productTitle">Example</span>
          <div id="desktop_buybox">Delivering to Los Angeles 90001 - Update location</div>
        </body></html>
        """, 200


class MissingTitleAdapter(Adapter):
    def fetch(self, url):
        return """
        <html><head><link rel="canonical" href="https://www.amazon.com/dp/B00RCPDCQU"></head><body>
          <input id="ASIN" value="B00RCPDCQU">
        </body></html>
        """, 200


class Missing404Adapter(Adapter):
    def fetch(self, url):
        return "<html><body>Not found</body></html>", 404


class Complete404Adapter(Adapter):
    def fetch(self, url):
        return """
        <html><head><link rel="canonical" href="https://www.amazon.com/dp/B00RCPDCQU"></head><body>
          <input id="ASIN" value="B00RCPDCQU"><span id="productTitle">Complete fallback body</span>
          <span class="a-price"><span class="a-offscreen">$19.99</span></span>
          <div id="desktop_buybox">Delivering to Los Angeles 90001</div>
        </body></html>
        """, 404


class SameAsinClpCanonicalAdapter(Adapter):
    def fetch(self, url):
        return """
        <html><head><link rel="canonical" href="https://www.amazon.com/clp/B00RCPDCQU"></head><body>
          <input id="ASIN" value="B00RCPDCQU"><span id="productTitle">Example</span>
        </body></html>
        """, 200


class DifferentAsinCanonicalAdapter(Adapter):
    def fetch(self, url):
        return """
        <html><head><link rel="canonical" href="https://www.amazon.com/example/dp/B00RCPDI50"></head><body>
          <input id="ASIN" value="B00RCPDCQU"><span id="productTitle">Replacement</span>
        </body></html>
        """, 200

def test_postgres_runner_claims_and_persists_a_product_without_sqlite():
    worker = load_worker()
    storage = Storage()
    config = dict(worker.DEFAULTS)
    config.update({"max_actions_per_run": 1, "raw_html_dir": None, "context": {}})

    assert worker._run_postgres_actions_impl(storage, Adapter(), config, limit=1, worker_id="worker-a") == 1
    assert len(storage.saved) == 1
    assert storage.saved[0]["next_status"] == "succeeded"
    assert storage.saved[0]["evidence"]["transfer_bytes"] == 321


def test_postgres_runner_accepts_same_asin_clp_canonical():
    worker = load_worker()
    storage = Storage()
    config = dict(worker.DEFAULTS)
    config.update({"max_actions_per_run": 1, "raw_html_dir": None, "context": {}})

    assert worker._run_postgres_actions_impl(storage, SameAsinClpCanonicalAdapter(), config, limit=1, worker_id="worker-a") == 1
    assert storage.saved[0]["next_status"] == "succeeded"


def test_postgres_runner_rejects_canonical_for_different_asin():
    worker = load_worker()
    storage = Storage()
    config = dict(worker.DEFAULTS)
    config.update({"max_actions_per_run": 1, "raw_html_dir": None, "context": {}})

    assert worker._run_postgres_actions_impl(storage, DifferentAsinCanonicalAdapter(), config, limit=1, worker_id="worker-a") == 1
    assert storage.saved[0]["reason"] == "asin_mismatch"


def test_worker_parser_exposes_explicit_postgres_runtime_options():
    worker = load_worker()
    args = worker.build_parser().parse_args([
        "--backend", "postgres", "--tenant-id", "tenant-a", "--subject-type", "own",
        "--worker-id", "worker-a", "--lease-seconds", "600",
    ])

    assert args.backend == "postgres"
    assert args.tenant_id == "tenant-a"
    assert args.subject_type == "own"
    assert args.worker_id == "worker-a"
    assert args.lease_seconds == 600
    assert worker.build_parser().parse_args([]).backend == "postgres"


def test_worker_parser_supports_product_only_batches():
    worker = load_worker()
    args = worker.build_parser().parse_args(["--product-only", "--run-id", "run-control-1"])

    assert args.product_only is True
    assert args.run_id == "run-control-1"


def test_worker_parser_supports_reviews_only_batches():
    worker = load_worker()
    args = worker.build_parser().parse_args(["--reviews-only", "--run-id", "run-reviews-1"])

    assert args.reviews_only is True
    assert args.run_id == "run-reviews-1"


def test_stage_only_flags_are_mutually_exclusive():
    worker = load_worker()

    with pytest.raises(SystemExit):
        worker.build_parser().parse_args(["--product-only", "--reviews-only"])


def test_product_only_rejects_legacy_sqlite_backend():
    worker = load_worker()
    args = worker.build_parser().parse_args(["--backend", "sqlite", "--product-only"])

    with pytest.raises(ValueError, match="PostgreSQL"):
        worker.validate_runtime_args(args)


def test_reviews_only_rejects_legacy_sqlite_backend():
    worker = load_worker()
    args = worker.build_parser().parse_args(["--backend", "sqlite", "--reviews-only"])

    with pytest.raises(ValueError, match="PostgreSQL"):
        worker.validate_runtime_args(args)


def test_live_collection_rejects_legacy_sqlite_backend_before_network():
    worker = load_worker()
    args = worker.build_parser().parse_args(["--backend", "sqlite", "--live"])

    with pytest.raises(ValueError, match="SQLite live collection is disabled"):
        worker.validate_runtime_args(args)


def test_sqlite_live_cli_fails_closed_in_a_no_network_subprocess():
    result = subprocess.run(
        [sys.executable, ROOT / "scripts" / "amazon_us_worker.py", "--backend", "sqlite", "--live"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 2
    assert "SQLite live collection is disabled" in result.stderr


def test_run_id_rejects_unsafe_characters():
    worker = load_worker()
    args = worker.build_parser().parse_args(["--run-id", "run id/unsafe"])

    with pytest.raises(ValueError, match="run-id"):
        worker.validate_runtime_args(args)


def test_postgres_runner_product_only_claims_only_product_stage():
    worker = load_worker()
    storage = ProductOnlyStorage()
    config = dict(worker.DEFAULTS)
    config.update({"max_actions_per_run": 1, "raw_html_dir": None, "context": {}})

    assert worker._run_postgres_actions_impl(
        storage, Adapter(), config, limit=1, worker_id="worker-a", product_only=True
    ) == 1
    assert storage.saved[0]["next_status"] == "succeeded"


def test_postgres_runner_reviews_only_claims_only_review_stage():
    worker = load_worker()
    storage = ReviewsOnlyStorage()
    config = dict(worker.DEFAULTS)
    config.update({"max_actions_per_run": 1, "raw_html_dir": None, "context": {}, "review_page_limit": 1})

    assert worker._run_postgres_actions_impl(
        storage, ReviewAdapter(), config, limit=1, worker_id="worker-a", reviews_only=True
    ) == 1
    assert len(storage.saved) == 1
    assert storage.saved[0]["reason"] == "reviews_exhausted"


@pytest.mark.parametrize(
    ("task_stage", "next_review_url"),
    [("reviews", None), ("product", "https://www.amazon.com/product-reviews/B00RCPDCQU")],
)
def test_postgres_runner_reviews_only_fails_closed_on_malformed_task(task_stage, next_review_url):
    worker = load_worker()
    storage = MalformedReviewsOnlyStorage(task_stage=task_stage, next_review_url=next_review_url)
    config = dict(worker.DEFAULTS)
    config.update({"max_actions_per_run": 1, "raw_html_dir": None, "context": {}})

    assert worker._run_postgres_actions_impl(
        storage, NoFetchAdapter(), config, limit=1, worker_id="worker-a", reviews_only=True
    ) == 1
    assert len(storage.saved) == 1
    assert storage.saved[0]["reason"] == "invalid_review_task"
    assert storage.saved[0]["evidence"]["error_code"] == "invalid_review_task"


def test_postgres_runner_persists_explicit_sibling_identity_without_product_data():
    worker = load_worker()
    storage = IdentityMismatchStorage()
    config = dict(worker.DEFAULTS)
    config.update({"max_actions_per_run": 1, "raw_html_dir": None, "context": {}})

    assert worker._run_postgres_actions_impl(
        storage, IdentityMismatchAdapter(), config, limit=1, worker_id="worker-a", product_only=True
    ) == 1
    assert len(storage.saved) == 1
    saved = storage.saved[0]
    assert saved["reason"] == "variant_redirect"
    assert saved["next_status"] == "succeeded"
    assert saved["increment_attempts"] is False
    assert saved["evidence"]["context_json"]["identity"] == {
        "requested_asin": "B0B9ZFDZNJ",
        "observed_asin": "B0B9ZFZZZZ",
        "canonical_asin": "B0B9ZFZZZZ",
        "parent_asin": "B0PARENT01",
        "child_asins": ["B0B9ZFDZNJ", "B0B9ZFZZZZ"],
        "canonical_valid_amazon": True,
    }
    assert all("product" not in entry for entry in storage.saved)


def test_agent_refresh_variant_resolution_completes_job_without_product_snapshot():
    worker = load_worker()

    class RefreshStorage(IdentityMismatchStorage):
        def __init__(self):
            super().__init__()
            self.finished = []

        def claim_refresh_task(self, worker_id, lease_seconds=None):
            task = self.claim_task(worker_id, lease_seconds)
            if task is not None:
                task["job_id"] = "refresh-variant"
            return task

        def finish_refresh_request(self, job_id, status):
            self.finished.append((job_id, status))

    storage = RefreshStorage()
    config = {**worker.DEFAULTS, "max_actions_per_run": 1, "raw_html_dir": None, "context": {}}

    assert worker._run_postgres_actions_impl(
        storage, IdentityMismatchAdapter(), config, limit=1, worker_id="agent-refresh",
    ) == 1
    assert storage.finished == [("refresh-variant", "completed")]
    assert storage.saved[0]["reason"] == "variant_redirect"
    assert all("product" not in entry for entry in storage.saved)


def test_product_fetch_failure_persists_run_evidence():
    worker = load_worker()

    class FailingAdapter(Adapter):
        last_transfer_bytes = 401602

        def fetch(self, url):
            raise worker.AdapterFetchError("incomplete response")

    storage = Storage()
    config = dict(worker.DEFAULTS)
    config.update({"max_actions_per_run": 1, "raw_html_dir": None, "context": {}})

    assert worker._run_postgres_actions_impl(
        storage, FailingAdapter(), config, limit=1, run_id="run-visible-3", worker_id="worker-a"
    ) == 1
    payload = storage.saved[0]
    assert payload["reason"] == "fetch_error"
    assert payload["evidence"]["run_id"] == "run-visible-3"
    assert payload["evidence"]["error_code"] == "fetch_error"
    assert payload["evidence"]["raw_html_path"] is None
    assert payload["evidence"]["transfer_bytes"] == 401602


def test_postgres_runner_persists_review_checkpoint_without_sqlite():
    worker = load_worker()
    storage = ReviewStorage()
    config = dict(worker.DEFAULTS)
    config.update({"max_actions_per_run": 1, "raw_html_dir": None, "context": {}, "review_page_limit": 0})

    assert worker._run_postgres_actions_impl(storage, ReviewAdapter(), config, limit=1, worker_id="worker-a") == 1
    assert len(storage.saved) == 1
    assert storage.saved[0]["next_status"] == "succeeded"
    assert storage.saved[0]["page"]["page"] == 1
    assert storage.saved[0]["summary"]["fetched_count"] == 1


def test_postgres_runner_uses_stable_review_url_when_portal_page_is_empty():
    worker = load_worker()
    storage = PortalReviewStorage()
    adapter = AlternateReviewAdapter()
    config = dict(worker.DEFAULTS)
    config.update({"max_actions_per_run": 1, "raw_html_dir": None, "context": {}, "review_page_limit": 0})

    assert worker._run_postgres_actions_impl(storage, adapter, config, limit=1, worker_id="worker-a") == 1
    assert len(adapter.calls) == 2
    assert "/product-reviews/B00RCPDCQU" in adapter.calls[1]
    assert storage.saved[0]["next_status"] == "succeeded"
    assert storage.saved[0]["summary"]["fetched_count"] == 1


def test_empty_review_failure_preserves_review_stage_and_cursor():
    worker = load_worker()
    storage = PortalReviewStorage()
    adapter = EmptyReviewAdapter()
    config = dict(worker.DEFAULTS)
    config.update({"max_actions_per_run": 1, "raw_html_dir": None, "context": {}, "review_page_limit": 0})

    assert worker._run_postgres_actions_impl(storage, adapter, config, limit=1, worker_id="worker-a") == 1
    payload = storage.saved[0]
    assert payload["next_status"] == "failed"
    assert payload["state_fields"]["task_stage"] == "reviews"
    assert payload["state_fields"]["next_review_url"]
    assert payload["state_fields"]["next_review_page"] == 1
    assert payload["summary"]["next_page"] == payload["state_fields"]["next_review_url"]


def test_postgres_runner_executes_and_completes_refresh_request():
    worker = load_worker()
    storage = RefreshStorage()
    config = dict(worker.DEFAULTS)
    config.update({"max_actions_per_run": 1, "raw_html_dir": None, "context": {}})

    assert worker._run_postgres_actions_impl(storage, Adapter(), config, limit=1, worker_id="worker-a") == 1
    assert storage.finished == [("job-1", "completed")]


def test_postgres_runner_retries_explicit_postal_mismatch_in_browser():
    worker = load_worker()
    storage = Storage()
    adapter = PostalFallbackAdapter()
    config = dict(worker.DEFAULTS)
    config.update({
        "max_actions_per_run": 1, "raw_html_dir": None,
        "context": {"expected_country": "US", "expected_currency": "USD", "postal_code": "90001"},
    })

    assert worker._run_postgres_actions_impl(storage, adapter, config, limit=1, worker_id="worker-a") == 1
    assert adapter.calls == ["http", "browser"]
    assert storage.saved[0]["next_status"] == "succeeded"


def test_postgres_runner_rejects_product_without_core_title():
    worker = load_worker()
    storage = Storage()
    config = dict(worker.DEFAULTS)
    config.update({"max_actions_per_run": 1, "raw_html_dir": None, "context": {}})

    assert worker._run_postgres_actions_impl(storage, MissingTitleAdapter(), config, limit=1, worker_id="worker-a") == 1
    assert storage.saved[0]["reason"] == "missing_core_fields"
    assert storage.saved[0]["error"] == "missing_core_fields:title"
    assert storage.saved[0].get("terminal", False) is False


def test_postgres_runner_marks_404_missing_asin_and_title_terminal():
    worker = load_worker()
    storage = Storage()
    config = dict(worker.DEFAULTS)
    config.update({
        "max_actions_per_run": 1,
        "raw_html_dir": None,
        "context": {"expected_country": "US", "expected_currency": "USD", "postal_code": "90001"},
    })

    assert worker._run_postgres_actions_impl(storage, Missing404Adapter(), config, limit=1, worker_id="worker-a") == 1
    assert storage.saved[0]["reason"] == "missing_core_fields"
    assert storage.saved[0]["error"] == "missing_core_fields:asin,title"
    assert storage.saved[0]["terminal"] is True
    assert storage.saved[0]["evidence"]["http_status"] == 404


def test_postgres_runner_does_not_terminalize_404_with_complete_identity():
    worker = load_worker()
    storage = Storage()
    config = dict(worker.DEFAULTS)
    config.update({
        "max_actions_per_run": 1,
        "raw_html_dir": None,
        "context": {"expected_country": "US", "expected_currency": "USD", "postal_code": "90001"},
    })

    assert worker._run_postgres_actions_impl(storage, Complete404Adapter(), config, limit=1, worker_id="worker-a") == 1
    assert "product" in storage.saved[0]
    assert storage.saved[0]["evidence"]["http_status"] == 404
    assert storage.saved[0]["evidence"]["error_code"] is None
