"""Validate the market and delivery context of a rendered product page."""
from __future__ import annotations

import json
import re
from typing import Any, Mapping


def _visible_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def validate_context(data: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> list[str]:
    context = context or {}
    expected_country = str(context.get("expected_country") or "").strip().upper()
    expected_currency = str(context.get("expected_currency") or "").strip().upper()
    errors: list[str] = []
    price = _visible_text(data.get("price", "")).upper()
    if expected_currency == "USD":
        non_us_markers = ("HKD", "CNY", "RMB", "EUR", "GBP", "JPY", "CAD", "AUD")
        if any(marker in price for marker in non_us_markers):
            errors.append("currency_mismatch")
    elif expected_currency and expected_currency not in price and price:
        errors.append("currency_not_observed")
    if expected_country == "US":
        page_text = " ".join(_visible_text(data.get(key, "")) for key in ("availability", "buy_box", "product_description")).lower()
        if re.search(r"deliver to\s+(hong kong|china|canada|united kingdom|australia)", page_text):
            errors.append("delivery_country_mismatch")
    return errors
