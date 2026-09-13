"""Additive PostgreSQL repository for product-watch-v1; it never alters crawler tables."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
from typing import Any, Callable

from .models import ChangeEvent, Observation


class ProductWatchRepository:
    def __init__(self, dsn: str, *, connect: Callable[[], Any] | None = None) -> None:
        if not dsn and connect is None:
            raise ValueError("dsn is required")
        self.dsn = dsn
        self._connect_factory = connect or self._connect

    def _connect(self) -> Any:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("ProductWatchRepository requires psycopg for PostgreSQL") from exc
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _json(value: Any) -> Any:
        try:
            from psycopg.types.json import Jsonb
            return Jsonb(value)
        except ImportError:
            return json.dumps(value, sort_keys=True, default=str)

    @staticmethod
    def _row(value: Any) -> dict[str, Any]:
        return dict(value) if not isinstance(value, dict) else value

    @classmethod
    def _observation(cls, row: Mapping[str, Any]) -> Observation:
        value = cls._row(row)
        for name in ("source_evidence_ids", "value_json", "raw_value_json", "context_json", "coverage_json"):
            if isinstance(value.get(name), str):
                value[name] = json.loads(value[name])
        return Observation.from_dict({
            "observation_id": value["observation_id"], "tenant_id": value["tenant_id"], "marketplace": value["marketplace"],
            "subject_type": value["subject_type"], "requested_asin": value["requested_asin"], "observed_asin": value["observed_asin"],
            "parent_asin": value.get("parent_asin"), "kind": value["kind"], "observed_at": value["observed_at"],
            "source_evidence_ids": value["source_evidence_ids"], "value": value.get("value_json"), "raw_value": value.get("raw_value_json"),
            "context": value.get("context_json") or {}, "quality": value["quality"], "presence": value["presence"],
            "coverage": value.get("coverage_json") or {}, "parser_version": value.get("parser_version"), "normalizer_version": value["normalizer_version"],
        })

    def persist_observations(self, observations: Iterable[Observation]) -> int:
        inserted = 0
        with self._connect_factory() as connection:
            try:
                with connection.cursor() as cursor:
                    for item in observations:
                        serialized = item.to_dict()
                        cursor.execute("""
                            INSERT INTO amazon_us.product_watch_observation
                            (observation_id,tenant_id,marketplace,subject_type,requested_asin,observed_asin,parent_asin,kind,observed_at,source_evidence_ids,value_json,raw_value_json,context_json,quality,presence,coverage_json,parser_version,normalizer_version)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (observation_id) DO NOTHING
                            RETURNING observation_id
                        """, (item.observation_id, item.tenant_id, item.marketplace, item.subject_type, item.requested_asin, item.observed_asin, item.parent_asin, item.kind, item.observed_at, self._json(serialized["source_evidence_ids"]), self._json(serialized["value"]), self._json(serialized["raw_value"]), self._json(serialized["context"]), item.quality, item.presence, self._json(serialized["coverage"]), item.parser_version, item.normalizer_version))
                        if cursor.fetchone() is not None:
                            inserted += 1
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return inserted

    def load_observations(self, *, tenant_id: str, marketplace: str, subject_type: str, requested_asin: str, observed_asin: str | None = None, kind: str | None = None, limit: int = 100) -> list[Observation]:
        clauses = ["tenant_id=%s", "marketplace=%s", "subject_type=%s", "requested_asin=%s"]
        params: list[Any] = [tenant_id, marketplace, subject_type, requested_asin]
        if observed_asin:
            clauses.append("observed_asin=%s"); params.append(observed_asin)
        if kind:
            clauses.append("kind=%s"); params.append(kind)
        params.append(limit)
        with self._connect_factory() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT * FROM amazon_us.product_watch_observation WHERE {' AND '.join(clauses)} ORDER BY observed_at DESC, observation_id DESC LIMIT %s", params)
            return [self._observation(row) for row in cursor.fetchall()]

    def _load_latest(self, *, include_non_effective: bool, identity: Mapping[str, str], kind: str, context: Mapping[str, Any] | None = None, rank_field: str | None = None) -> Observation | None:
        if rank_field not in {None, "organic", "ad"}:
            raise ValueError("rank_field must be organic, ad, or None")
        clauses = ["tenant_id=%s", "marketplace=%s", "subject_type=%s", "requested_asin=%s", "observed_asin=%s", "kind=%s"]
        params: list[Any] = [identity["tenant_id"], identity["marketplace"], identity["subject_type"], identity["requested_asin"], identity["observed_asin"], kind]
        if not include_non_effective:
            clauses.extend(["quality='valid'", "presence IN ('observed','confirmed_absent')"])
            if kind == "keyword_rank":
                field = f"{rank_field or 'organic'}_position"
                clauses.append("(presence='confirmed_absent' OR (value_json ? %s AND value_json->>%s IS NOT NULL))")
                params.extend([field, field])
        for key, value in sorted((context or {}).items()):
            clauses.append("context_json @> %s::jsonb"); params.append(json.dumps({key: value}, sort_keys=True))
        with self._connect_factory() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT * FROM amazon_us.product_watch_observation WHERE {' AND '.join(clauses)} ORDER BY observed_at DESC, observation_id DESC LIMIT 1", params)
            row = cursor.fetchone()
            return self._observation(row) if row is not None else None

    def load_latest_effective(self, *, identity: Mapping[str, str], kind: str, context: Mapping[str, Any] | None = None, rank_field: str | None = None) -> Observation | None:
        """Load an effective business fact; keyword ranks default to organic position."""
        return self._load_latest(include_non_effective=False, identity=identity, kind=kind, context=context, rank_field=rank_field)

    def load_latest_attempt(self, *, identity: Mapping[str, str], kind: str, context: Mapping[str, Any] | None = None) -> Observation | None:
        return self._load_latest(include_non_effective=True, identity=identity, kind=kind, context=context)

    def load_latest_state(self, *, identity: Mapping[str, str], kind: str, context: Mapping[str, Any] | None = None, rank_field: str | None = None) -> dict[str, Any]:
        """Return latest valid business value and latest attempt without hiding staleness."""
        effective = self.load_latest_effective(identity=identity, kind=kind, context=context, rank_field=rank_field)
        attempt = self.load_latest_attempt(identity=identity, kind=kind, context=context)
        required_key = f"{rank_field or 'organic'}_position" if kind == "keyword_rank" else None
        attempt_supplies_value = not required_key or attempt is None or attempt.presence == "confirmed_absent" or (attempt.value or {}).get(required_key) not in (None, "")
        stale = bool(effective and attempt and attempt.observed_at > effective.observed_at and (not (attempt.quality == "valid" and attempt.presence == "observed") or not attempt_supplies_value))
        return {
            "effective": effective,
            "latest_attempt": attempt,
            "stale": stale,
            "stale_since": effective.observed_at.isoformat() if stale and effective else None,
        }

    def persist_events(self, events: Iterable[ChangeEvent]) -> int:
        inserted = 0
        with self._connect_factory() as connection:
            try:
                with connection.cursor() as cursor:
                    for event in events:
                        serialized = event.to_dict()
                        cursor.execute("""
                            INSERT INTO amazon_us.product_watch_event
                            (event_id,event_type,watch_id,before_observation_id,after_observation_id,old_value_json,new_value_json,delta_json,comparison_context_json,observed_window_json,quality,evidence_ids,rule_version,severity,reason)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (event_id) DO NOTHING RETURNING event_id
                        """, (event.event_id, event.event_type, event.watch_id, event.before_observation_id, event.after_observation_id, self._json(serialized["old_value"]), self._json(serialized["new_value"]), self._json(serialized["delta"]), self._json(serialized["comparison_context"]), self._json(serialized["observed_window"]), event.quality, self._json(serialized["evidence_ids"]), event.rule_version, event.severity, event.reason))
                        if cursor.fetchone() is not None:
                            inserted += 1
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return inserted

    def load_event_summary(self, *, tenant_id: str, marketplace: str, since: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = [tenant_id, marketplace]
        since_clause = ""
        if since:
            since_clause = " AND e.created_at >= %s"; params.append(since)
        with self._connect_factory() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT e.event_type,e.severity,COUNT(*) AS count,MAX(e.created_at) AS latest_at
                FROM amazon_us.product_watch_event e
                JOIN amazon_us.product_watch_observation o ON o.observation_id=e.after_observation_id
                WHERE o.tenant_id=%s AND o.marketplace=%s
            """ + since_clause + " GROUP BY e.event_type,e.severity ORDER BY latest_at DESC", params)
            return [self._row(row) for row in cursor.fetchall()]
