from __future__ import annotations

import importlib.util
import json
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "woot_review_provider.py"
SPEC = importlib.util.spec_from_file_location("woot_review_provider", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["woot_review_provider"] = MODULE
SPEC.loader.exec_module(MODULE)


def review(*, body="Works well", provider_id=None, image=None):
    item = {
        "Author": "Buyer One",
        "Title": "Useful &amp; safe",
        "Text": body,
        "OverallRating": 5,
        "OriginDescription": "Reviewed in the United States on September 1, 2026",
        "IsVerifiedPurchase": True,
        "IsVineReview": False,
        "HelpfulVotes": 3,
        "ImageUrls": [image] if image else [],
        "MediaUrls": [{"src": "https://media.example/video.mp4"}],
    }
    if provider_id:
        item["ReviewId"] = provider_id
    return item


class MatrixTransport:
    def __init__(self, *, pages=None, status=200, raw_body=None):
        self.pages = pages or {}
        self.status = status
        self.raw_body = raw_body
        self.calls = []
        self.user_agents = []

    def __call__(self, request, timeout):
        del timeout
        parsed = urllib.parse.urlparse(request.full_url)
        params = urllib.parse.parse_qs(parsed.query)
        key = (
            int(params["filter"][0]),
            int(params["sort"][0]),
            params.get("pagingNext", [None])[0],
        )
        self.calls.append(key)
        self.user_agents.append(request.get_header("User-agent"))
        if self.raw_body is not None:
            body = self.raw_body
        else:
            body = json.dumps(self.pages.get(key, {"Reviews": [], "PagingNext": ""})).encode()
        return MODULE.HttpResponse(self.status, {"content-type": "application/json"}, body)


class NetworkErrorTransport:
    def __call__(self, request, timeout):
        del request, timeout
        raise MODULE.WootProviderError("network_error", "simulated network failure")


def test_default_plan_is_us_max_mode_and_performs_no_io(tmp_path):
    plan = MODULE.build_plan("b0abc12345")
    assert plan["marketplace"] == "US"
    assert plan["query_count"] == 20
    assert plan["network_calls_performed"] == 0
    assert plan["database_writes_performed"] == 0
    assert plan["pdp_truth_written"] is False
    assert "Agent/woot-review" in plan["user_agent"]
    assert list(tmp_path.iterdir()) == []


def test_user_agent_is_configurable_but_transparent_agent_marker_is_mandatory():
    with pytest.raises(ValueError, match="Agent/<name>"):
        MODULE.build_plan("B0ABC12345", user_agent="Mozilla/5.0")

    transport = MatrixTransport()
    configured = "Mozilla/5.0 Firefox/142.0 Agent/review-worker-2"
    MODULE.collect_reviews(
        "B0ABC12345", mode="basic", transport=transport,
        request_delay_seconds=0, user_agent=configured,
    )
    assert transport.user_agents == [configured]


def test_cli_defaults_to_plan_without_network(monkeypatch, capsys):
    monkeypatch.setattr(MODULE, "_default_transport", lambda *_: pytest.fail("network called"))
    assert MODULE.main(["--asin", "B0ABC12345"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["query_count"] == 20
    assert payload["complete_requires_traceable_denominator"] is True


def test_collect_follows_cursor_deduplicates_and_preserves_rich_fields():
    first = review(body="Same full body", image="https://img.example/one.jpg")
    duplicate = review(body="Same full body", image="https://img.example/two.jpg")
    pages = {
        (5, 0, None): {"Reviews": [first], "PagingNext": "cursor-2"},
        (5, 0, "cursor-2"): {"Reviews": [duplicate], "PagingNext": ""},
    }
    transport = MatrixTransport(pages=pages)
    output = MODULE.collect_reviews(
        "B0ABC12345", transport=transport, request_delay_seconds=0,
        expected_written_reviews=1, expected_count_source="amazon_written_review_count",
    )
    assert output["coverage"]["status"] == "complete"
    assert output["coverage"]["claim_100_percent"] is True
    assert output["coverage"]["all_queries_exhausted"] is True
    assert output["raw_records_seen"] == 2
    assert output["duplicate_occurrences"] == 1
    assert len(output["reviews"]) == 1
    saved = output["reviews"][0]
    assert saved["id_kind"] == "fallback_hash"
    assert saved["title"] == "Useful & safe"
    assert saved["verified"] is True
    assert saved["vine"] is False
    assert saved["helpful_votes"] == 3
    assert saved["review_images"] == ["https://img.example/one.jpg", "https://img.example/two.jpg"]
    assert saved["review_videos"] == [{"src": "https://media.example/video.mp4"}]
    assert len(saved["source_refs"]) >= 2


def test_delay_is_applied_between_every_request(monkeypatch):
    sleeps = []
    monkeypatch.setattr(MODULE.time, "sleep", sleeps.append)
    transport = MatrixTransport()
    MODULE.collect_reviews(
        "B0ABC12345", mode="full", transport=transport, request_delay_seconds=0.25
    )
    assert len(transport.calls) == 5
    assert sleeps == [0.25, 0.25, 0.25, 0.25]


def test_adaptive_mode_stops_after_exhausted_under_cap_query():
    pages = {(0, 0, None): {
        "Reviews": [review(body="one"), review(body="two"), review(body="three")],
        "PagingNext": "",
    }}
    transport = MatrixTransport(pages=pages)
    output = MODULE.collect_reviews(
        "B0ABC12345", mode="adaptive", transport=transport, request_delay_seconds=0
    )
    assert len(transport.calls) == 1
    assert output["coverage"]["adaptive_short_circuit"] is True
    assert output["coverage"]["all_queries_exhausted"] is True
    assert output["coverage"]["status"] == "partial"
    assert len(output["reviews"]) == 3


def test_adaptive_mode_escalates_to_max_matrix_at_query_cap():
    pages = {(0, 0, None): {
        "Reviews": [review(body=f"review-{index}") for index in range(100)],
        "PagingNext": "",
    }}
    transport = MatrixTransport(pages=pages)
    output = MODULE.collect_reviews(
        "B0ABC12345", mode="adaptive", transport=transport, request_delay_seconds=0
    )
    assert len(transport.calls) == 21
    assert output["coverage"]["adaptive_short_circuit"] is False
    assert output["coverage"]["capped_queries"] == ["star-0_sort-0"]
    assert output["coverage"]["status"] == "source_limited"


def test_provider_review_id_is_preferred_over_fallback_hash():
    normalized = MODULE.normalize_review(
        "B0ABC12345", review(provider_id="R-123"), {"query": "star-5_sort-0", "page": 1}
    )
    assert normalized["review_id"] == "woot:R-123"
    assert normalized["provider_review_id"] == "R-123"
    assert normalized["id_kind"] == "provider"


def test_fallback_identity_uses_complete_body_and_date():
    base = review(body="A" * 120 + " first ending")
    changed = review(body="A" * 120 + " second ending")
    first = MODULE.normalize_review("B0ABC12345", base, {"query": "q", "page": 1})
    second = MODULE.normalize_review("B0ABC12345", changed, {"query": "q", "page": 1})
    assert first["review_id"] != second["review_id"]


def test_unknown_denominator_never_claims_complete():
    pages = {(5, 0, None): {"Reviews": [review()], "PagingNext": ""}}
    output = MODULE.collect_reviews(
        "B0ABC12345", transport=MatrixTransport(pages=pages), request_delay_seconds=0
    )
    assert output["coverage"]["status"] == "partial"
    assert output["coverage"]["claim_100_percent"] is False


def test_more_records_than_denominator_is_a_conflict_not_complete():
    pages = {(0, 0, None): {"Reviews": [review(body="one"), review(body="two")], "PagingNext": ""}}
    output = MODULE.collect_reviews(
        "B0ABC12345", mode="basic", transport=MatrixTransport(pages=pages),
        request_delay_seconds=0, expected_written_reviews=1,
        expected_count_source="amazon_written_review_count",
    )
    assert output["coverage"]["status"] == "partial"
    assert output["coverage"]["denominator_conflict"] is True
    assert output["coverage"]["claim_100_percent"] is False


def test_zero_reviews_requires_traceable_zero_denominator():
    unverified = MODULE.collect_reviews(
        "B0ABC12345", mode="basic", transport=MatrixTransport(), request_delay_seconds=0,
    )
    assert unverified["coverage"]["status"] == "partial"
    verified = MODULE.collect_reviews(
        "B0ABC12345", mode="basic", transport=MatrixTransport(), request_delay_seconds=0,
        expected_written_reviews=0, expected_count_source="amazon_written_review_count",
    )
    assert verified["coverage"]["status"] == "no_reviews_verified"
    assert verified["coverage"]["claim_100_percent"] is True


def test_positive_denominator_with_empty_provider_fails_closed():
    with pytest.raises(MODULE.WootProviderError, match="zero reviews") as caught:
        MODULE.collect_reviews(
            "B0ABC12345", mode="basic", transport=MatrixTransport(), request_delay_seconds=0,
            expected_written_reviews=2, expected_count_source="amazon_written_review_count",
        )
    assert caught.value.code == "unexpected_empty"


def test_response_schema_and_cursor_loop_fail_closed():
    invalid = MatrixTransport(raw_body=b'{"PagingNext":""}')
    with pytest.raises(MODULE.WootProviderError) as schema_error:
        MODULE.collect_reviews("B0ABC12345", mode="basic", transport=invalid, request_delay_seconds=0)
    assert schema_error.value.code == "schema_mismatch"

    pages = {
        (0, 0, None): {"Reviews": [review(body="one")], "PagingNext": "loop"},
        (0, 0, "loop"): {"Reviews": [review(body="two")], "PagingNext": "loop"},
    }
    with pytest.raises(MODULE.WootProviderError) as cursor_error:
        MODULE.collect_reviews(
            "B0ABC12345", mode="basic", transport=MatrixTransport(pages=pages), request_delay_seconds=0
        )
    assert cursor_error.value.code == "cursor_loop"


def test_blank_review_object_is_schema_mismatch():
    pages = {(0, 0, None): {"Reviews": [{}], "PagingNext": ""}}
    with pytest.raises(MODULE.WootProviderError) as caught:
        MODULE.collect_reviews(
            "B0ABC12345", mode="basic", transport=MatrixTransport(pages=pages),
            request_delay_seconds=0,
        )
    assert caught.value.code == "schema_mismatch"


@pytest.mark.parametrize(("status", "code"), [(403, "provider_blocked"), (429, "provider_rate_limited")])
def test_http_block_is_saved_before_failed_receipt(tmp_path, status, code):
    plan = MODULE.build_plan("B0ABC12345", mode="basic", request_delay_seconds=0)
    transport = MatrixTransport(status=status, raw_body=b'{"message":"blocked"}')
    with pytest.raises(MODULE.WootProviderError) as caught:
        MODULE.execute_collection(
            plan, output_root=tmp_path, transport=transport,
            now=datetime(2026, 9, 14, 6, 0, tzinfo=timezone.utc),
        )
    assert caught.value.code == code
    run_dir = next(tmp_path.iterdir())
    assert Path(caught.value.run_dir).resolve() == run_dir.resolve()
    failed = json.loads((run_dir / "receipt.failed.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "evidence_manifest.json").read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["error_code"] == code
    assert failed["coverage"]["status"] == "blocked"
    assert failed["coverage"]["claim_100_percent"] is False
    assert failed["evidence_files"] == 1
    assert manifest[0]["http_status"] == status
    body_path = run_dir / manifest[0]["body_path"]
    assert body_path.read_bytes() == b'{"message":"blocked"}'


def test_query_first_page_404_is_source_limited_and_other_queries_continue():
    calls = []

    def transport(request, timeout):
        del timeout
        params = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        key = (int(params["filter"][0]), int(params["sort"][0]))
        calls.append(key)
        if key == (5, 0):
            return MODULE.HttpResponse(404, {"content-type": "text/html"}, b"Woot: Page Not Found")
        body = json.dumps({"Reviews": [], "PagingNext": ""}).encode()
        return MODULE.HttpResponse(200, {"content-type": "application/json"}, body)

    output = MODULE.collect_reviews(
        "B0ABC12345", mode="max", transport=transport, request_delay_seconds=0
    )
    assert len(calls) == 20
    assert output["coverage"]["status"] == "source_limited"
    assert output["coverage"]["query_http_404s"] == [{
        "query": "star-5_sort-0", "page": 1, "kind": "query_unavailable",
    }]
    assert output["coverage"]["claim_100_percent"] is False


def test_query_cursor_404_keeps_prior_reviews_and_marks_query_truncated():
    calls = []

    def transport(request, timeout):
        del timeout
        params = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        key = (
            int(params["filter"][0]), int(params["sort"][0]),
            params.get("pagingNext", [None])[0],
        )
        calls.append(key)
        if key == (5, 0, None):
            body = json.dumps({"Reviews": [review(body="kept")], "PagingNext": "next"}).encode()
            return MODULE.HttpResponse(200, {"content-type": "application/json"}, body)
        if key == (5, 0, "next"):
            return MODULE.HttpResponse(404, {"content-type": "text/html"}, b"Woot: Page Not Found")
        body = json.dumps({"Reviews": [], "PagingNext": ""}).encode()
        return MODULE.HttpResponse(200, {"content-type": "application/json"}, body)

    output = MODULE.collect_reviews(
        "B0ABC12345", mode="max", transport=transport, request_delay_seconds=0
    )
    assert len(calls) == 21
    assert len(output["reviews"]) == 1
    assert output["reviews"][0]["body"] == "kept"
    assert output["coverage"]["status"] == "source_limited"
    assert output["coverage"]["query_http_404s"] == [{
        "query": "star-5_sort-0", "page": 2, "kind": "query_truncated",
    }]


def test_network_failure_has_attempt_evidence_and_failed_receipt(tmp_path):
    plan = MODULE.build_plan("B0ABC12345", mode="basic", request_delay_seconds=0)
    with pytest.raises(MODULE.WootProviderError) as caught:
        MODULE.execute_collection(
            plan, output_root=tmp_path, transport=NetworkErrorTransport(),
            now=datetime(2026, 9, 14, 6, 0, tzinfo=timezone.utc),
        )
    assert caught.value.code == "network_error"
    run_dir = next(tmp_path.iterdir())
    manifest = json.loads((run_dir / "evidence_manifest.json").read_text(encoding="utf-8"))
    failed = json.loads((run_dir / "receipt.failed.json").read_text(encoding="utf-8"))
    assert manifest[0]["error_code"] == "network_error"
    assert manifest[0]["body_path"] is None
    assert failed["evidence_files"] == 1
    assert failed["coverage"]["status"] == "blocked"


def test_request_budget_and_cap_are_reported_as_source_limited():
    many = [review(body=f"review-{index}") for index in range(100)]
    pages = {(5, 0, None): {"Reviews": many, "PagingNext": ""}}
    output = MODULE.collect_reviews(
        "B0ABC12345", transport=MatrixTransport(pages=pages), request_delay_seconds=0,
        max_requests=1, expected_written_reviews=120,
        expected_count_source="amazon_written_review_count",
    )
    assert output["coverage"]["status"] == "source_limited"
    assert output["coverage"]["request_budget_exhausted"] is True
    assert output["coverage"]["capped_queries"] == ["star-5_sort-0"]
    assert output["coverage"]["claim_100_percent"] is False


def test_success_run_writes_immutable_manifest_output_and_receipt(tmp_path):
    plan = MODULE.build_plan(
        "B0ABC12345", mode="basic", request_delay_seconds=0,
        expected_written_reviews=1, expected_count_source="amazon_written_review_count",
    )
    pages = {(0, 0, None): {"Reviews": [review(provider_id="R1")], "PagingNext": ""}}
    receipt = MODULE.execute_collection(
        plan, output_root=tmp_path, transport=MatrixTransport(pages=pages),
        now=datetime(2026, 9, 14, 6, 0, tzinfo=timezone.utc),
    )
    run_dir = Path(receipt["run_dir"])
    assert receipt["status"] == "success"
    assert receipt["coverage"]["status"] == "complete"
    assert (run_dir / "plan.json").exists()
    assert (run_dir / "evidence_manifest.json").exists()
    assert (run_dir / "reviews.normalized.json").exists()
    assert (run_dir / "receipt.json").exists()
    assert receipt["database_writes_performed"] == 0
    assert receipt["pdp_truth_written"] is False
