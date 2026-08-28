from __future__ import annotations

import importlib.util
from pathlib import Path


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

def test_postgres_runner_claims_and_persists_a_product_without_sqlite():
    worker = load_worker()
    storage = Storage()
    config = dict(worker.DEFAULTS)
    config.update({"max_actions_per_run": 1, "raw_html_dir": None, "context": {}})

    assert worker.run_postgres_actions(storage, Adapter(), config, limit=1, worker_id="worker-a") == 1
    assert len(storage.saved) == 1
    assert storage.saved[0]["next_status"] == "succeeded"
    assert storage.saved[0]["evidence"]["transfer_bytes"] == 321


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


def test_postgres_runner_persists_review_checkpoint_without_sqlite():
    worker = load_worker()
    storage = ReviewStorage()
    config = dict(worker.DEFAULTS)
    config.update({"max_actions_per_run": 1, "raw_html_dir": None, "context": {}, "review_page_limit": 0})

    assert worker.run_postgres_actions(storage, ReviewAdapter(), config, limit=1, worker_id="worker-a") == 1
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

    assert worker.run_postgres_actions(storage, adapter, config, limit=1, worker_id="worker-a") == 1
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

    assert worker.run_postgres_actions(storage, adapter, config, limit=1, worker_id="worker-a") == 1
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

    assert worker.run_postgres_actions(storage, Adapter(), config, limit=1, worker_id="worker-a") == 1
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

    assert worker.run_postgres_actions(storage, adapter, config, limit=1, worker_id="worker-a") == 1
    assert adapter.calls == ["http", "browser"]
    assert storage.saved[0]["next_status"] == "succeeded"


def test_postgres_runner_rejects_product_without_core_title():
    worker = load_worker()
    storage = Storage()
    config = dict(worker.DEFAULTS)
    config.update({"max_actions_per_run": 1, "raw_html_dir": None, "context": {}})

    assert worker.run_postgres_actions(storage, MissingTitleAdapter(), config, limit=1, worker_id="worker-a") == 1
    assert storage.saved[0]["reason"] == "missing_core_fields"
    assert storage.saved[0]["error"] == "missing_core_fields:title"
