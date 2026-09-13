from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from product_watch import normalize_keyword_rank, normalize_snapshot


EVIDENCE = {"content_hash": "a" * 64, "retrieved_at": "2026-09-05T01:00:00+00:00", "parser_version": "parser-1"}


def test_snapshot_normalizes_decimal_price_multiple_bsr_and_variant_identity():
    observations = normalize_snapshot({
        "asin": "B000000001", "price": "$9.99", "specs": {"Best Sellers Rank": "#1,693,662 in Home & Kitchen () See Top 100 in Home & Kitchen #25,305 in Decorative Hanging Ornaments"},
    }, EVIDENCE, requested_asin="B000000000")
    price = next(item for item in observations if item.kind == "price")
    ranks = [item for item in observations if item.kind == "bsr"]
    assert price.value == {"amount": Decimal("9.99"), "currency": "USD"}
    assert price.requested_asin == "B000000000" and price.observed_asin == "B000000001"
    assert [(item.value["rank"], item.context["category_name"]) for item in ranks] == [(1693662, "Home & Kitchen"), (25305, "Decorative Hanging Ornaments")]
    assert all(item.coverage["category_identity"] == "source_name_only" for item in ranks)


def test_bsr_category_removes_empty_or_display_parentheses_but_keeps_multiple_categories():
    observations = normalize_snapshot({
        "asin": "B000000000", "price": "$9.99",
        "specs": {"Best Sellers Rank": "#1 in Books (See Top 100 in Books) #2 in History () See Top 100 in History"},
    }, EVIDENCE)
    ranks = [item for item in observations if item.kind == "bsr"]
    assert [item.context["category_name"] for item in ranks] == ["Books", "History"]


def test_missing_coupon_is_not_confirmed_absent_but_explicit_signal_is():
    normal = normalize_snapshot({"asin": "B000000000", "price": "$3.99", "buy_box": {}}, EVIDENCE)
    promotion = next(item for item in normal if item.kind == "promotion")
    assert promotion.presence == "not_observed" and promotion.quality == "unknown"
    explicit = normalize_snapshot({"asin": "B000000000", "price": "$3.99", "promotion_confirmed_absent": True}, EVIDENCE)
    assert next(item for item in explicit if item.kind == "promotion").presence == "confirmed_absent"


def test_failed_evidence_does_not_fake_zero_or_missing_value():
    failed = normalize_snapshot({"asin": "B000000000", "price": ""}, {**EVIDENCE, "error_code": "captcha"})
    assert [item.kind for item in failed] == ["price", "promotion", "bsr"]
    assert all(item.quality == "unknown" and item.presence == "not_observed" for item in failed)
    category_failed = normalize_snapshot({"asin": "B000000000", "attempt_contexts": {"bsr": [{"category_name": "Home & Kitchen"}]}}, {**EVIDENCE, "error_code": "captcha"})
    assert next(item for item in category_failed if item.kind == "bsr").context == {"category_name": "Home & Kitchen"}


def test_partial_location_context_is_an_attempt_not_a_comparable_business_fact():
    observations = normalize_snapshot(
        {"asin": "B000000000", "price": "$9.99", "specs": {"Best Sellers Rank": "#1 in Books"}},
        {**EVIDENCE, "context_json": {"context_quality": "partial"}},
    )
    assert [item.kind for item in observations] == ["price", "promotion", "bsr"]
    assert all(item.quality == "partial" and item.presence == "not_observed" for item in observations)
    assert all(item.coverage["status"] == "uncomparable_context" for item in observations)


def test_keyword_coverage_distinguishes_found_absent_partial_and_failure():
    common = {"asin": "B000000000", "keyword": "ornament"}
    found = normalize_keyword_rank({**common, "organic_position": 3, "ad_position": 1, "coverage_complete": True, "searched_depth": 3}, EVIDENCE)
    absent = normalize_keyword_rank({**common, "coverage_complete": True, "searched_depth": 3}, EVIDENCE)
    partial = normalize_keyword_rank({**common, "coverage_complete": False, "searched_depth": 1}, EVIDENCE)
    failed = normalize_keyword_rank({**common, "coverage_complete": True}, {**EVIDENCE, "error_code": "timeout"})
    assert found.value["organic_position"] == 3 and found.value["ad_position"] == 1
    assert absent.presence == "confirmed_absent" and absent.coverage["status"] == "not_found_in_covered_range"
    assert partial.presence == "not_observed" and partial.quality == "partial"
    assert failed.presence == "not_observed" and failed.quality == "unknown"


def test_same_input_has_stable_observation_ids_and_changes_on_tenant_or_variant():
    payload = {"asin": "B000000000", "price": "$0.00"}
    first = normalize_snapshot(payload, EVIDENCE, tenant_id="tenant-a")[0]
    again = normalize_snapshot(payload, EVIDENCE, tenant_id="tenant-a")[0]
    other_tenant = normalize_snapshot(payload, EVIDENCE, tenant_id="tenant-b")[0]
    other_variant = normalize_snapshot({**payload, "asin": "B000000001"}, EVIDENCE, tenant_id="tenant-a", requested_asin="B000000000")[0]
    assert first.observation_id == again.observation_id
    assert first.value["amount"] == Decimal("0.00")
    assert first.observation_id != other_tenant.observation_id != other_variant.observation_id


def test_price_without_a_currency_is_partial_and_timestamp_is_required_for_reproducibility():
    observations = normalize_snapshot({"asin": "B000000000", "price": "10.00"}, EVIDENCE)
    assert observations[0].quality == "partial" and observations[0].coverage["currency_known"] is False
    missing_time = {"content_hash": "b" * 64}
    try:
        normalize_snapshot({"asin": "B000000000", "price": "$10.00"}, missing_time)
    except ValueError as exc:
        assert "observed_at" in str(exc) or "retrieved_at" in str(exc)
    else:
        raise AssertionError("missing timestamp must fail")


def test_offline_cli_uses_the_same_normalizer_on_a_real_sample_derivative(tmp_path):
    output = tmp_path / "observations.json"
    command = [sys.executable, str(ROOT / "scripts" / "normalize_product_observations.py"), "--input", str(ROOT / "tests" / "fixtures" / "product_watch_snapshot.json"), "--output", str(output)]
    assert subprocess.run(command, cwd=ROOT, check=False).returncode == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert [item["kind"] for item in result] == ["price", "promotion", "bsr", "bsr"]
    assert result[0]["value"] == {"amount": "9.99", "currency": "USD"}
