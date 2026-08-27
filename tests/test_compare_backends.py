from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "compare_backends.py"
spec = spec_from_file_location("compare_backends", MODULE_PATH)
compare_backends = module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(compare_backends)


class FakeRepository:
    def __init__(self, counts, product):
        self.status = {"counts": counts, "refresh_requests": {}}
        self.product = product

    def load_job_status(self):
        return self.status

    def load_product(self, marketplace, asin):
        return self.product.get(asin)


def test_compare_repositories_matches_status_and_sample():
    product = {"A1": {"task": {"status": "pending"}, "source": None, "counts": {"media": 0, "content_modules": 0}}}
    result = compare_backends.compare_repositories(
        FakeRepository({"pending": 1}, product),
        FakeRepository({"pending": 1}, product),
        ["A1"],
    )
    assert result["ok"] is True
    assert result["sample_mismatches"] == []


def test_compare_repositories_reports_mismatch_without_writing():
    left = {"A1": {"task": {"status": "pending"}, "source": None, "counts": {"media": 0, "content_modules": 0}}}
    right = {"A1": {"task": {"status": "product_done"}, "source": "http_html", "counts": {"media": 1, "content_modules": 0}}}
    result = compare_backends.compare_repositories(
        FakeRepository({"pending": 1}, left),
        FakeRepository({"product_done": 1}, right),
        ["A1"],
    )
    assert result["ok"] is False
    assert result["task_status_match"] is False
    assert result["sample_mismatches"][0]["asin"] == "A1"


def test_compare_repositories_detects_transfer_evidence_loss():
    left = {"A1": {"task": {"status": "succeeded"}, "source": "http_html", "evidence": {"transfer_bytes": 1200}, "counts": {}}}
    right = {"A1": {"task": {"status": "succeeded"}, "source": "http_html", "evidence": {"transfer_bytes": None}, "counts": {}}}
    result = compare_backends.compare_repositories(
        FakeRepository({"succeeded": 1}, left),
        FakeRepository({"succeeded": 1}, right),
        ["A1"],
    )
    assert result["ok"] is False
    assert result["sample_mismatches"][0]["sqlite"]["transfer_bytes"] == 1200
