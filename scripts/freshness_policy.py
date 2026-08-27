"""Field-level freshness rules shared by schedulers and Agent clients."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

DEFAULT_TTLS_SECONDS = {
    "price": 4 * 60 * 60,
    "availability": 4 * 60 * 60,
    "offer": 4 * 60 * 60,
    "rating": 24 * 60 * 60,
    "reviews": 24 * 60 * 60,
    "content": 7 * 24 * 60 * 60,
    "media": 7 * 24 * 60 * 60,
    "identity": 30 * 24 * 60 * 60,
}


def _parse_timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class FreshnessPolicy:
    def __init__(self, ttls_seconds: Mapping[str, int] | None = None, default_ttl_seconds: int = 24 * 60 * 60):
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be > 0")
        self.default_ttl_seconds = int(default_ttl_seconds)
        self.ttls_seconds = dict(DEFAULT_TTLS_SECONDS)
        if ttls_seconds:
            for field_group, ttl in ttls_seconds.items():
                if int(ttl) <= 0:
                    raise ValueError(f"TTL must be > 0: {field_group}")
                self.ttls_seconds[str(field_group)] = int(ttl)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "FreshnessPolicy":
        default = int(values.get("default_ttl_seconds", 24 * 60 * 60))
        overrides = {key: int(value) for key, value in values.items() if key != "default_ttl_seconds"}
        return cls(overrides, default)

    def ttl(self, field_group: str) -> int:
        return self.ttls_seconds.get(field_group, self.default_ttl_seconds)

    def age_seconds(self, captured_at: Any, now: datetime | None = None) -> int | None:
        if not captured_at:
            return None
        try:
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            return max(0, int((current - _parse_timestamp(captured_at)).total_seconds()))
        except (TypeError, ValueError):
            return None

    def evaluate(self, captured_at: Any, field_groups: list[str], now: datetime | None = None) -> dict[str, Any]:
        age = self.age_seconds(captured_at, now)
        stale = age is None or any(age > self.ttl(group) for group in field_groups)
        return {
            "captured_at": captured_at,
            "age_seconds": age,
            "field_groups": field_groups,
            "stale": stale,
            "stale_groups": [group for group in field_groups if age is None or age > self.ttl(group)],
        }
