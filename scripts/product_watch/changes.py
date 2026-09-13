"""Comparable observation change detection. No persistence or notification occurs here."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Mapping

from .models import ChangeEvent, Observation


def _watch_id(observation: Observation, rules: Mapping[str, Any]) -> str:
    watches = rules.get("watch_ids") or {}
    key = f"{observation.tenant_id}|{observation.marketplace}|{observation.subject_type}|{observation.requested_asin}|{observation.observed_asin}"
    return str(watches.get(key) or watches.get(observation.requested_asin) or rules.get("watch_id") or key)


def _comparable(before: Observation, after: Observation) -> bool:
    return before.kind == after.kind and before.comparison_identity() == after.comparison_identity()


def _eligible(observation: Observation) -> bool:
    return observation.quality == "valid" and observation.presence == "observed"


def _event(before: Observation, after: Observation, *, event_type: str, delta: dict[str, Any], rules: Mapping[str, Any], severity: str = "info", reason: str = "") -> ChangeEvent:
    return ChangeEvent(
        event_type=event_type, watch_id=_watch_id(after, rules), before_observation_id=before.observation_id,
        after_observation_id=after.observation_id or "", old_value=before.value, new_value=after.value,
        delta=delta, comparison_context=after.comparison_identity(),
        observed_window={"before": before.observed_at.isoformat(), "after": after.observed_at.isoformat()},
        quality="valid", evidence_ids=tuple(sorted(set(before.source_evidence_ids + after.source_evidence_ids))),
        rule_version=str(rules.get("rule_version") or "product-watch-v1"), severity=severity, reason=reason,
    )


def _rank_event(before: Observation, after: Observation, rules: Mapping[str, Any], field: str, prefix: str) -> ChangeEvent | None:
    old = (before.value or {}).get(field)
    new = (after.value or {}).get(field)
    if old in (None, "") or new in (None, ""):
        return None
    old_rank, new_rank = int(old), int(new)
    if old_rank == new_rank:
        return None
    event_type = f"{prefix}_rank_improved" if new_rank < old_rank else f"{prefix}_rank_declined"
    return _event(before, after, event_type=event_type, delta={"field": field, "old": old_rank, "new": new_rank, "change": new_rank - old_rank}, rules=rules, severity="important" if abs(new_rank - old_rank) >= int(rules.get("rank_change_threshold", 1)) else "info", reason="lower numeric rank is better")


def _detect(before: Observation, after: Observation, rules: Mapping[str, Any]) -> ChangeEvent | None:
    if after.kind == "price":
        old = (before.value or {}).get("amount")
        new = (after.value or {}).get("amount")
        if old is None or new is None:
            return None
        old_amount, new_amount = Decimal(str(old)), Decimal(str(new))
        if old_amount == new_amount:
            return None
        delta = new_amount - old_amount
        threshold = Decimal(str(rules.get("price_change_threshold", "0")))
        event_type = "price_increase" if delta > 0 else "price_decrease"
        return _event(before, after, event_type=event_type, delta={"old": old_amount, "new": new_amount, "amount": delta, "currency": (after.value or {}).get("currency")}, rules=rules, severity="important" if abs(delta) >= threshold else "info", reason="comparable currency and offer context")
    if after.kind == "promotion":
        if before.value == after.value:
            return None
        return _event(before, after, event_type="promotion_changed", delta={"old": before.value, "new": after.value}, rules=rules, severity="important", reason="conditional promotion text/terms changed")
    if after.kind == "bsr":
        return _rank_event(before, after, rules, "rank", "bsr")
    if after.kind == "keyword_rank":
        return _rank_event(before, after, rules, "organic_position", "keyword")
    return None


def _promotion_end(before: Observation, after: Observation, rules: Mapping[str, Any]) -> ChangeEvent | None:
    if after.kind != "promotion" or after.presence != "confirmed_absent" or not _eligible(before):
        return None
    return _event(before, after, event_type="promotion_ended", delta={"old": before.value, "new": None}, rules=rules, severity="important", reason="source explicitly confirmed promotion absence")


def _keyword_not_found(before: Observation, after: Observation, rules: Mapping[str, Any]) -> ChangeEvent | None:
    if after.kind != "keyword_rank" or after.presence != "confirmed_absent" or not _eligible(before):
        return None
    if not bool(after.coverage.get("coverage_complete")):
        return None
    if (before.value or {}).get("organic_position") in (None, ""):
        return None
    return _event(before, after, event_type="keyword_rank_not_found", delta={"old": before.value.get("organic_position"), "new": None, "searched_depth": after.coverage.get("searched_depth")}, rules=rules, severity="important", reason="complete searched range did not contain organic result")


def detect_changes(current: Iterable[Observation], history: Iterable[Observation], *, rules: Mapping[str, Any] | None = None) -> list[ChangeEvent]:
    """Compare observations to the latest prior comparable valid observation.

    First valid observations establish a baseline. Unknown, partial and failed
    attempts never turn into a price/rank regression or promotion-end signal.
    """
    policy = dict(rules or {})
    result: list[ChangeEvent] = []
    previous = list(history)
    for after in sorted(current, key=lambda item: item.observed_at):
        candidates = [item for item in previous if item.observed_at < after.observed_at and _comparable(item, after) and _eligible(item)]
        if after.kind == "keyword_rank":
            candidates = [item for item in candidates if (item.value or {}).get("organic_position") not in (None, "")]
        if not candidates:
            previous.append(after)
            continue
        before = max(candidates, key=lambda item: item.observed_at)
        event = _promotion_end(before, after, policy) or _keyword_not_found(before, after, policy)
        if event is None and _eligible(after):
            event = _detect(before, after, policy)
        if event is not None:
            result.append(event)
        previous.append(after)
    return result
