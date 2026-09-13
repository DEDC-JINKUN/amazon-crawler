"""Deterministic, evidence-first product-watch report rendering."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def report_for_events(events: Iterable[Mapping[str, Any]], *, report_kind: str, timezone_name: str = "UTC") -> dict[str, Any]:
    """Create text that remains useful when no LLM is configured.

    Event timestamps are observations windows, not asserted change times. Raw
    paths and connection details deliberately never enter this projection.
    """
    items = [dict(item) for item in events]
    if not items:
        raise ValueError("at least one event is required")
    rows: list[dict[str, Any]] = []
    lines = [f"Product watch {report_kind} report ({timezone_name})"]
    for event in items:
        comparison = event.get("comparison_context_json") or event.get("comparison_context") or {}
        window = event.get("observed_window_json") or event.get("observed_window") or {}
        delta = event.get("delta_json") or event.get("delta") or {}
        asin = comparison.get("requested_asin") or event.get("watch_id") or "unknown product"
        context = {key: comparison.get(key) for key in ("currency", "category_name", "keyword", "search_conditions") if comparison.get(key) is not None}
        row = {
            "event_id": str(event["event_id"]), "event_type": str(event["event_type"]),
            "severity": str(event.get("severity") or "info"), "watch_id": str(event.get("watch_id") or ""),
            "subject": asin, "before": _json_value(event.get("old_value_json") or event.get("old_value")),
            "after": _json_value(event.get("new_value_json") or event.get("new_value")),
            "delta": _json_value(delta), "comparison_context": _json_value(context),
            "observed_window": _json_value(window), "evidence_ids": list(event.get("evidence_ids") or []),
            "comparable": bool(comparison), "reason": str(event.get("reason") or ""),
        }
        rows.append(row)
        lines.append(
            f"- {row['subject']}: {row['event_type']} ({row['severity']}); "
            f"before={row['before']}; after={row['after']}; delta={row['delta']}; "
            f"observed window={row['observed_window']}; evidence={','.join(row['evidence_ids']) or 'none'}"
        )
    lines.append("Operational follow-up: verify the listed evidence and comparability before changing price, promotion, or advertising decisions.")
    return {"report_kind": report_kind, "timezone": timezone_name, "events": rows, "content_text": "\n".join(lines)}
