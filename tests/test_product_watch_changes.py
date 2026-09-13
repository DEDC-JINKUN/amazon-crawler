from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from product_watch import Observation, detect_changes


def make(kind: str, value, *, when: int, context=None, quality="valid", presence="observed", coverage=None, evidence=None):
    return Observation(tenant_id="t", marketplace="US", subject_type="competitor", requested_asin="B000000000", observed_asin="B000000000", kind=kind, observed_at=datetime(2026, 9, 5, tzinfo=timezone.utc) + timedelta(minutes=when), source_evidence_ids=(evidence or f"e-{when}",), value=value, context=context or {}, quality=quality, presence=presence, coverage=coverage or {})


def test_price_up_down_and_zero_are_precise_and_baseline_is_silent():
    baseline = make("price", {"amount": Decimal("0.00"), "currency": "USD"}, when=1, context={"currency": "USD"})
    up = make("price", {"amount": Decimal("1.00"), "currency": "USD"}, when=2, context={"currency": "USD"})
    down = make("price", {"amount": Decimal("0.00"), "currency": "USD"}, when=3, context={"currency": "USD"})
    assert detect_changes([baseline], []) == []
    events = detect_changes([up, down], [baseline])
    assert [event.event_type for event in events] == ["price_increase", "price_decrease"]
    assert events[0].delta["amount"] == Decimal("1.00")


def test_missing_failure_or_context_change_never_becomes_price_change():
    old = make("price", {"amount": Decimal("10"), "currency": "USD"}, when=1, context={"currency": "USD", "seller": "A"})
    unknown = make("price", None, when=2, context={"currency": "USD", "seller": "A"}, quality="unknown", presence="not_observed")
    seller_change = make("price", {"amount": Decimal("20"), "currency": "USD"}, when=3, context={"currency": "USD", "seller": "B"})
    assert detect_changes([unknown, seller_change], [old]) == []


def test_partial_unknown_currency_price_never_becomes_a_comparable_change():
    old = make("price", {"amount": Decimal("10"), "currency": None}, when=1, context={}, quality="partial")
    new = make("price", {"amount": Decimal("20"), "currency": None}, when=2, context={}, quality="partial")
    assert detect_changes([new], [old]) == []


def test_promotion_end_requires_explicit_absence_and_terms_are_not_unconditional_price():
    old = make("promotion", {"coupon": "Save 20% when buying 2"}, when=1)
    unknown = make("promotion", None, when=2, quality="unknown", presence="not_observed")
    ended = make("promotion", None, when=3, presence="confirmed_absent")
    assert detect_changes([unknown], [old]) == []
    event = detect_changes([ended], [old])[0]
    assert event.event_type == "promotion_ended" and event.new_value is None


def test_bsr_category_change_is_not_compared_and_keyword_partial_is_not_out_of_range():
    bsr = make("bsr", {"rank": 100}, when=1, context={"category_name": "A"})
    other_category = make("bsr", {"rank": 1}, when=2, context={"category_name": "B"})
    keyword = make("keyword_rank", {"organic_position": 2, "ad_position": None}, when=1, context={"keyword": "ornament"})
    partial = make("keyword_rank", None, when=2, context={"keyword": "ornament"}, quality="partial", presence="not_observed", coverage={"coverage_complete": False})
    absent = make("keyword_rank", None, when=3, context={"keyword": "ornament"}, presence="confirmed_absent", coverage={"coverage_complete": True, "searched_depth": 3})
    assert detect_changes([other_category, partial], [bsr, keyword]) == []
    assert detect_changes([absent], [keyword])[0].event_type == "keyword_rank_not_found"


def test_same_pair_and_rule_version_has_stable_single_event_identity():
    old = make("price", {"amount": Decimal("2"), "currency": "USD"}, when=1, context={"currency": "USD"})
    new = make("price", {"amount": Decimal("3"), "currency": "USD"}, when=2, context={"currency": "USD"})
    first = detect_changes([new], [old], rules={"rule_version": "r1"})[0]
    replay = detect_changes([new], [old], rules={"rule_version": "r1"})[0]
    changed_rule = detect_changes([new], [old], rules={"rule_version": "r2"})[0]
    assert first.event_id == replay.event_id and first.event_id != changed_rule.event_id


def test_ad_only_keyword_result_does_not_replace_the_organic_baseline():
    old = make("keyword_rank", {"organic_position": 2, "ad_position": None}, when=1, context={"keyword": "ornament"})
    ad_only = make("keyword_rank", {"organic_position": None, "ad_position": 1}, when=2, context={"keyword": "ornament"})
    later = make("keyword_rank", {"organic_position": 5, "ad_position": None}, when=3, context={"keyword": "ornament"})
    events = detect_changes([ad_only, later], [old])
    assert [event.event_type for event in events] == ["keyword_rank_declined"]
    assert events[0].old_value["organic_position"] == 2
