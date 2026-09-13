"""Offline normalization of existing snapshots/evidence; this module never fetches."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping

from .models import Observation, utc_datetime

NORMALIZER_VERSION = "product-watch-v1"
_PRICE_RE = re.compile(r"(?P<currency>USD|US\$|\$|HKD|EUR|GBP|C\$)?\s*(?P<number>\d[\d,]*(?:\.\d{1,4})?)", re.I)
_BSR_RE = re.compile(r"#\s*(?P<rank>[\d,]+)\s+in\s+(?P<category>[^#\r\n]+?)(?=\s+#\s*[\d,]+\s+in\s+|$)", re.I)
_BSR_DISPLAY_NOTE_RE = re.compile(r"\s+See\s+Top\s+100(?:\s+in\s+.*)?$", re.I)


def _get(mapping: Mapping[str, Any], *names: str) -> Any:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _evidence_identity(evidence: Mapping[str, Any]) -> str:
    for key in ("evidence_id", "id", "content_hash", "raw_html_path", "run_id"):
        value = evidence.get(key)
        if value not in (None, ""):
            return f"evidence:{value}"
    raise ValueError("evidence requires id, content_hash, raw_html_path, or run_id")


def _currency(value: str, supplied: Any = None) -> str | None:
    if supplied:
        return str(supplied).upper()
    match = _PRICE_RE.search(value)
    token = (match.group("currency") or "").upper() if match else ""
    return {"$": "USD", "US$": "USD", "USD": "USD", "HKD": "HKD", "EUR": "EUR", "GBP": "GBP", "C$": "CAD"}.get(token)


def _price(value: Any) -> tuple[Decimal | None, str | None]:
    if value in (None, ""):
        return None, None
    text = str(value).strip()
    match = _PRICE_RE.search(text)
    if not match:
        return None, text
    try:
        return Decimal(match.group("number").replace(",", "")), text
    except InvalidOperation:
        return None, text


def _base(snapshot: Mapping[str, Any], evidence: Mapping[str, Any], *, tenant_id: str, marketplace: str, subject_type: str, requested_asin: str, observed_at: datetime | str | None) -> dict[str, Any]:
    observed_asin = str(_get(snapshot, "observed_asin", "asin", "canonical_asin") or requested_asin).strip().upper()
    source_time = observed_at or _get(snapshot, "collected_at", "observed_at") or _get(evidence, "retrieved_at")
    if source_time is None:
        raise ValueError("snapshot/evidence requires observed_at or retrieved_at")
    return {
        "tenant_id": tenant_id, "marketplace": marketplace.upper(), "subject_type": subject_type,
        "requested_asin": requested_asin.strip().upper(), "observed_asin": observed_asin,
        "parent_asin": _get(snapshot, "parent_asin"),
        "observed_at": utc_datetime(source_time),
        "source_evidence_ids": (_evidence_identity(evidence),),
        "parser_version": _get(evidence, "parser_version") or _get(snapshot, "parser_version"),
        "normalizer_version": NORMALIZER_VERSION,
    }


def _failed(evidence: Mapping[str, Any]) -> bool:
    status = evidence.get("http_status")
    return bool(evidence.get("error_code") or evidence.get("block_reason") or (status is not None and int(status) >= 400))


def _uncomparable_context(evidence: Mapping[str, Any]) -> str | None:
    context = evidence.get("context_json") or {}
    if isinstance(context, str):
        try:
            import json
            context = json.loads(context)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    quality = str(context.get("context_quality") or "") if isinstance(context, Mapping) else ""
    return quality if quality in {"partial", "invalid"} else None


def _observation(base: Mapping[str, Any], kind: str, **values: Any) -> Observation:
    return Observation(kind=kind, **base, **values)


def _promotion_value(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any] | None, Any]:
    buy_box = _get(snapshot, "buy_box", "buy_box_json") or {}
    if not isinstance(buy_box, Mapping):
        return None, buy_box
    selected: dict[str, Any] = {}
    for key, value in buy_box.items():
        lowered = str(key).lower()
        if value not in (None, "") and any(token in lowered for token in ("coupon", "promotion", "promo", "reference price", "list price", "deal")):
            selected[str(key)] = value
    explicit = _get(snapshot, "promotion", "promotions")
    if explicit not in (None, "", {}, []):
        selected["promotion"] = explicit
    return (selected or None), buy_box


def _bsr_rows(specs: Any) -> list[tuple[int, str]]:
    if not isinstance(specs, Mapping):
        return []
    ranks = _get(specs, "Best Sellers Rank", "best_sellers_rank", "bsr")
    if ranks in (None, ""):
        return []
    rows: list[tuple[int, str]] = []
    for match in _BSR_RE.finditer(str(ranks)):
        category = re.sub(r"\s*\([^)]*\)", "", match.group("category"))
        category = _BSR_DISPLAY_NOTE_RE.sub("", category).strip()
        if category:
            rows.append((int(match.group("rank").replace(",", "")), category))
    return rows


def normalize_snapshot(snapshot: Mapping[str, Any], evidence: Mapping[str, Any], *, tenant_id: str = "default", marketplace: str = "US", subject_type: str = "own", requested_asin: str | None = None, observed_at: datetime | str | None = None) -> list[Observation]:
    """Normalize an already-captured product snapshot into observations.

    A failed capture creates an unknown price attempt only. Missing coupon text is
    intentionally *not* proof that a promotion ended.
    """
    requested = requested_asin or _get(snapshot, "requested_asin", "asin") or _get(evidence, "asin")
    if not requested:
        raise ValueError("requested_asin is required")
    base = _base(snapshot, evidence, tenant_id=tenant_id, marketplace=marketplace, subject_type=subject_type, requested_asin=str(requested), observed_at=observed_at)
    if _failed(evidence):
        failed_coverage = {"status": "failed", "error_code": evidence.get("error_code"), "block_reason": evidence.get("block_reason")}
        attempted_kinds = tuple(_get(snapshot, "attempted_kinds") or ("price", "promotion", "bsr"))
        attempt_contexts = _get(snapshot, "attempt_contexts") or {}
        result: list[Observation] = []
        for kind in attempted_kinds:
            contexts = attempt_contexts.get(kind, [{}]) if isinstance(attempt_contexts, Mapping) else [{}]
            for context in contexts:
                result.append(_observation(base, str(kind), raw_value=_get(snapshot, "price") if kind == "price" else None, context=dict(context or {}), quality="unknown", presence="not_observed", coverage=failed_coverage))
        return result

    context_quality = _uncomparable_context(evidence)
    if context_quality:
        coverage = {"status": "uncomparable_context", "context_quality": context_quality}
        # A partial geographic/context result remains an auditable attempt, not
        # a comparable price, promotion or BSR business fact.
        return [
            _observation(base, kind, raw_value=_get(snapshot, "price") if kind == "price" else None,
                context={}, quality="partial", presence="not_observed", coverage=coverage)
            for kind in ("price", "promotion", "bsr")
        ]

    result: list[Observation] = []
    price_raw = _get(snapshot, "price")
    amount, price_text = _price(price_raw)
    price_context = {
        "currency": _currency(price_text or "", _get(snapshot, "currency")),
        "seller": _get(snapshot, "seller", "merchant"), "offer": _get(snapshot, "offer"),
        "delivery_region": _get(snapshot, "delivery_region", "postal_code"),
    }
    if amount is None:
        result.append(_observation(base, "price", raw_value=price_raw, context={key: value for key, value in price_context.items() if value is not None}, quality="partial", presence="not_observed", coverage={"status": "snapshot_succeeded", "field": "price"}))
    else:
        currency_known = price_context["currency"] is not None
        result.append(_observation(base, "price", value={"amount": amount, "currency": price_context["currency"]}, raw_value=price_text, context={key: value for key, value in price_context.items() if value is not None}, quality="valid" if currency_known else "partial", coverage={"status": "observed", "currency_known": currency_known}))

    promotion, promotion_raw = _promotion_value(snapshot)
    if promotion:
        result.append(_observation(base, "promotion", value=promotion, raw_value=promotion_raw, context={key: value for key, value in price_context.items() if value is not None}, coverage={"status": "observed", "conditional": True}))
    elif bool(_get(snapshot, "promotion_confirmed_absent")):
        result.append(_observation(base, "promotion", raw_value=promotion_raw, context={key: value for key, value in price_context.items() if value is not None}, presence="confirmed_absent", coverage={"status": "explicitly_confirmed_absent"}))
    else:
        result.append(_observation(base, "promotion", raw_value=promotion_raw, context={key: value for key, value in price_context.items() if value is not None}, quality="unknown", presence="not_observed", coverage={"status": "coupon_text_not_proof_of_absence"}))

    specs = _get(snapshot, "specs", "specs_json") or {}
    bsr_rows = _bsr_rows(specs)
    category_ids = _get(snapshot, "bsr_category_ids") or {}
    for rank, category in bsr_rows:
        category_id = category_ids.get(category) if isinstance(category_ids, Mapping) else None
        if category_id is None and len(bsr_rows) == 1:
            category_id = _get(snapshot, "bsr_category_id")
        result.append(_observation(base, "bsr", value={"rank": rank}, raw_value=_get(specs, "Best Sellers Rank", "best_sellers_rank", "bsr"), context={"category_name": category, "category_id": category_id}, coverage={"status": "observed", "category_id_available": bool(category_id), "category_identity": "source_name_only" if not category_id else "source_id"}))
    return result


def normalize_keyword_rank(payload: Mapping[str, Any], evidence: Mapping[str, Any], *, tenant_id: str = "default", marketplace: str = "US", subject_type: str = "own", requested_asin: str | None = None, observed_at: datetime | str | None = None) -> Observation:
    """Normalize C's search result payload without making a search request."""
    requested = requested_asin or _get(payload, "requested_asin", "asin") or _get(evidence, "asin")
    keyword = _get(payload, "keyword")
    if not requested or not keyword:
        raise ValueError("keyword and requested_asin are required")
    base = _base(payload, evidence, tenant_id=tenant_id, marketplace=marketplace, subject_type=subject_type, requested_asin=str(requested), observed_at=observed_at)
    coverage_complete = bool(_get(payload, "coverage_complete"))
    searched_depth = _get(payload, "searched_depth")
    context = {"keyword": str(keyword), "search_conditions": _get(payload, "search_conditions")}
    coverage = {"coverage_complete": coverage_complete, "searched_depth": searched_depth, "page": _get(payload, "page"), "card_index": _get(payload, "card_index")}
    if _failed(evidence) or str(_get(payload, "status") or "").lower() == "failed":
        return _observation(base, "keyword_rank", raw_value=dict(payload), context=context, quality="unknown", presence="not_observed", coverage={**coverage, "status": "failed"})
    organic = _get(payload, "organic_position")
    ad = _get(payload, "ad_position")
    if organic not in (None, "") or ad not in (None, ""):
        return _observation(base, "keyword_rank", value={"organic_position": int(organic) if organic not in (None, "") else None, "ad_position": int(ad) if ad not in (None, "") else None, "page": _get(payload, "page"), "card_index": _get(payload, "card_index")}, raw_value=dict(payload), context=context, coverage={**coverage, "status": "found"})
    if coverage_complete:
        return _observation(base, "keyword_rank", raw_value=dict(payload), context=context, presence="confirmed_absent", coverage={**coverage, "status": "not_found_in_covered_range"})
    return _observation(base, "keyword_rank", raw_value=dict(payload), context=context, quality="partial", presence="not_observed", coverage={**coverage, "status": "partial_or_missing_page"})
