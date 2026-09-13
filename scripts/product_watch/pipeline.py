"""Recoverable bridge from captured snapshots to B events and D's outbox."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
from typing import Any

from .changes import detect_changes
from .normalize import normalize_snapshot


class WatchPipeline:
    def __init__(self, *, watch_repository: Any, delivery_repository: Any, tenant_id: str, marketplace: str = "US", rules: Mapping[str, Any] | None = None, reporting: Mapping[str, Any] | None = None) -> None:
        self.watch_repository = watch_repository
        self.delivery_repository = delivery_repository
        self.tenant_id = tenant_id
        self.marketplace = marketplace.upper()
        self.rules = dict(rules or {})
        self.reporting = dict(reporting or {})

    def process_snapshot(self, *, snapshot: Mapping[str, Any], evidence: Mapping[str, Any], subject_type: str, requested_asin: str) -> dict[str, int]:
        observations = normalize_snapshot(snapshot, evidence, tenant_id=self.tenant_id, marketplace=self.marketplace, subject_type=subject_type, requested_asin=requested_asin)
        events = []
        for observation in observations:
            history = self.watch_repository.load_observations(tenant_id=observation.tenant_id, marketplace=observation.marketplace, subject_type=observation.subject_type, requested_asin=observation.requested_asin, observed_asin=observation.observed_asin, kind=observation.kind)
            events.extend(detect_changes([observation], history, rules=self.rules))
        inserted_observations = self.watch_repository.persist_observations(observations)
        inserted_events = self.watch_repository.persist_events(events)
        # A crash after B persistence is safe: sync_event_reports discovers the
        # unhandled durable event on the next scheduled run.
        reports = self.delivery_repository.sync_event_reports(tenant_id=self.tenant_id, marketplace=self.marketplace, config=self.reporting)
        return {"observations": inserted_observations, "events": inserted_events, "reports": reports}

    def reconcile(self) -> int:
        return self.delivery_repository.sync_event_reports(tenant_id=self.tenant_id, marketplace=self.marketplace, config=self.reporting)


class PostgresSnapshotIngestor:
    """Consumes committed crawler evidence without making another Amazon request."""
    def __init__(self, pipeline: WatchPipeline) -> None:
        self.pipeline = pipeline

    def _rows(self, query: str, params: list[Any]) -> list[dict[str, Any]]:
        with self.pipeline.watch_repository._connect_factory() as connection, connection.cursor() as cursor:
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def process_pending(self, *, limit: int = 50) -> dict[str, int]:
        """Ingest only durable, not-yet-normalized snapshots/failures.

        The source evidence identity is the crawler evidence ID when available;
        snapshot IDs are used only where historical schema has no direct link.
        This consumes the existing database and therefore never refreshes a
        product, decompresses raw HTML, or turns a failed request into no change.
        """
        cap = max(1, min(500, int(limit)))
        tenant, marketplace = self.pipeline.tenant_id, self.pipeline.marketplace
        snapshots = self._rows("""
            SELECT s.*, ('snapshot:' || s.snapshot_id::text) AS watch_source_id
            FROM amazon_us.product_snapshot s
            WHERE s.tenant_id=%s AND s.marketplace=%s AND NOT EXISTS (
              SELECT 1 FROM amazon_us.product_watch_observation o
              WHERE o.tenant_id=s.tenant_id AND o.source_evidence_ids @> jsonb_build_array('evidence:snapshot:' || s.snapshot_id::text)
            )
            ORDER BY s.snapshot_id LIMIT %s
        """, [tenant, marketplace, cap])
        result = {"observations": 0, "events": 0, "reports": 0, "snapshots": 0, "failed_attempts": 0}
        for row in snapshots:
            source_id = str(row["watch_source_id"])
            if self._already_ingested(source_id):
                continue
            processed = self.pipeline.process_snapshot(snapshot=row, evidence={"id": source_id, "retrieved_at": row["collected_at"], "parser_version": "postgres-snapshot"}, subject_type=str(row["subject_type"]), requested_asin=str(row["asin"]))
            result["snapshots"] += 1
            for name in ("observations", "events", "reports"): result[name] += processed[name]
        failures = self._rows("""
            SELECT id,asin,subject_type,retrieved_at,http_status,error_code,block_reason,parser_version
            FROM amazon_us.collection_evidence
            WHERE tenant_id=%s AND marketplace=%s AND (error_code IS NOT NULL OR block_reason IS NOT NULL OR COALESCE(http_status, 0) >= 400)
            ORDER BY id LIMIT %s
        """, [tenant, marketplace, cap])
        for row in failures:
            source_id = str(row["id"])
            if self._already_ingested(source_id):
                continue
            processed = self.pipeline.process_snapshot(snapshot={"asin": row["asin"]}, evidence=row, subject_type=str(row["subject_type"]), requested_asin=str(row["asin"]))
            result["failed_attempts"] += 1
            for name in ("observations", "events", "reports"): result[name] += processed[name]
        result["reports"] += self.pipeline.reconcile()
        return result

    def _already_ingested(self, source_id: str) -> bool:
        with self.pipeline.watch_repository._connect_factory() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM amazon_us.product_watch_observation WHERE tenant_id=%s AND source_evidence_ids @> %s::jsonb LIMIT 1", (self.pipeline.tenant_id, json.dumps([f"evidence:{source_id}"])))
            return cursor.fetchone() is not None


class SearchRankRecoveryIngestor:
    """Replays C's durable search pages into B/D after an interrupted handoff."""
    def __init__(self, pipeline: WatchPipeline, dsn: str) -> None:
        self.pipeline = pipeline
        self.dsn = dsn

    def process_pending(self, *, limit: int = 20) -> dict[str, int]:
        from search_rank_storage import SearchRankStorage
        from search_rank_worker import run_replay
        with self.pipeline.watch_repository._connect_factory() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT batch_id,run_id,keyword,search_conditions_json,requested_depth,target_asins_json
                FROM amazon_us.search_rank_batch WHERE tenant_id=%s AND marketplace=%s
                ORDER BY finished_at NULLS FIRST,batch_id LIMIT %s
            """, (self.pipeline.tenant_id, self.pipeline.marketplace, max(1, min(100, int(limit)))))
            batches = [dict(row) for row in cursor.fetchall()]
        result = {"search_batches": 0, "search_observations": 0, "search_events": 0, "reports": 0}
        storage = SearchRankStorage(self.dsn)
        for batch in batches:
            conditions = batch.get("search_conditions_json") or {}
            if isinstance(conditions, str): conditions = json.loads(conditions)
            targets = batch.get("target_asins_json") or []
            if isinstance(targets, str): targets = json.loads(targets)
            replay = run_replay(keyword=str(batch["keyword"]), target_asins=[str(value) for value in targets], page_html={}, requested_depth=int(batch["requested_depth"]), run_id=str(batch["run_id"]), config={"tenant_id": self.pipeline.tenant_id, "marketplace": self.pipeline.marketplace, "search_conditions": conditions, **self.pipeline.rules}, batch_id=str(batch["batch_id"]), storage=storage, watch_repository=self.pipeline.watch_repository)
            result["search_batches"] += 1
            result["search_observations"] += len(replay["observations"])
            result["search_events"] += len(replay["events"])
        result["reports"] = self.pipeline.reconcile()
        return result
