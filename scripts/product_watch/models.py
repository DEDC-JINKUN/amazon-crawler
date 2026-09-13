"""Stable, JSON-serializable objects for the product-watch-v1 contract."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any, Mapping


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def stable_id(prefix: str, value: Mapping[str, Any]) -> str:
    encoded = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"{prefix}_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def utc_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        raise ValueError("observed_at is required and must include a timezone")
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Observation:
    tenant_id: str
    marketplace: str
    subject_type: str
    requested_asin: str
    observed_asin: str
    kind: str
    observed_at: datetime
    source_evidence_ids: tuple[str, ...]
    value: dict[str, Any] | None = None
    raw_value: Any = None
    context: dict[str, Any] = field(default_factory=dict)
    quality: str = "valid"
    presence: str = "observed"
    coverage: dict[str, Any] = field(default_factory=dict)
    parent_asin: str | None = None
    parser_version: str | None = None
    normalizer_version: str = "product-watch-v1"
    observation_id: str | None = None

    def __post_init__(self) -> None:
        if not all((self.tenant_id, self.marketplace, self.subject_type, self.requested_asin, self.observed_asin, self.kind)):
            raise ValueError("observation identity and kind are required")
        if self.quality not in {"valid", "partial", "unknown", "invalid"}:
            raise ValueError("invalid observation quality")
        if self.presence not in {"observed", "confirmed_absent", "not_observed"}:
            raise ValueError("invalid observation presence")
        if not self.source_evidence_ids:
            raise ValueError("at least one stable source evidence id is required")
        object.__setattr__(self, "observed_at", utc_datetime(self.observed_at))
        object.__setattr__(self, "source_evidence_ids", tuple(sorted(str(item) for item in self.source_evidence_ids)))
        if self.observation_id is None:
            object.__setattr__(self, "observation_id", stable_id("obs", self.fact_identity()))

    def fact_identity(self) -> dict[str, Any]:
        comparable_context = {
            key: self.context.get(key)
            for key in ("currency", "seller", "offer", "delivery_region", "category_id", "category_name", "keyword", "search_conditions")
            if self.context.get(key) is not None
        }
        return {
            "tenant_id": self.tenant_id, "marketplace": self.marketplace, "subject_type": self.subject_type,
            "requested_asin": self.requested_asin, "observed_asin": self.observed_asin,
            "parent_asin": self.parent_asin, "kind": self.kind,
            "source_evidence_ids": self.source_evidence_ids, "context": comparable_context,
        }

    def comparison_identity(self) -> dict[str, Any]:
        value = self.fact_identity()
        value.pop("source_evidence_ids")
        return value

    def to_dict(self) -> dict[str, Any]:
        result = _canonical(asdict(self))
        result["source_evidence_ids"] = list(self.source_evidence_ids)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Observation":
        row = dict(value)
        row["source_evidence_ids"] = tuple(row.get("source_evidence_ids") or ())
        row["observed_at"] = utc_datetime(row["observed_at"])
        return cls(**row)


@dataclass(frozen=True)
class ChangeEvent:
    event_type: str
    watch_id: str
    before_observation_id: str | None
    after_observation_id: str
    old_value: dict[str, Any] | None
    new_value: dict[str, Any] | None
    delta: dict[str, Any]
    comparison_context: dict[str, Any]
    observed_window: dict[str, str]
    quality: str
    evidence_ids: tuple[str, ...]
    rule_version: str = "product-watch-v1"
    severity: str = "info"
    reason: str = ""
    event_id: str | None = None

    def __post_init__(self) -> None:
        if not self.watch_id or not self.after_observation_id or not self.event_type:
            raise ValueError("event type, watch id, and after observation are required")
        object.__setattr__(self, "evidence_ids", tuple(sorted(str(item) for item in self.evidence_ids)))
        if self.event_id is None:
            object.__setattr__(self, "event_id", stable_id("evt", {
                "event_type": self.event_type, "watch_id": self.watch_id,
                "before_observation_id": self.before_observation_id,
                "after_observation_id": self.after_observation_id,
                "rule_version": self.rule_version,
            }))

    def to_dict(self) -> dict[str, Any]:
        result = _canonical(asdict(self))
        result["evidence_ids"] = list(self.evidence_ids)
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChangeEvent":
        row = dict(value)
        row["evidence_ids"] = tuple(row.get("evidence_ids") or ())
        return cls(**row)
