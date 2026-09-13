#!/usr/bin/env python3
"""Run a bounded product-watch reconciliation and optional offline snapshot replay.

Live collection remains owned by the existing bounded workers. This entrypoint
only turns their already-captured data (or an explicit offline replay file) into
watch observations, events, reports and outbox rows.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from product_watch import PostgresSnapshotIngestor, ProductWatchRepository, SearchRankRecoveryIngestor, WatchDeliveryRepository, WatchPipeline
from product_watch.delivery import LocalReceiptChannel, UnconfiguredChannel


def load_watch_config(path: Path) -> dict[str, Any]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    required = ("tenant_id",)
    missing = [name for name in required if not str(data.get(name) or "").strip()]
    if missing:
        raise ValueError(f"missing required configuration: {', '.join(missing)}")
    return data


def _run_configured_keyword_jobs(config: dict[str, Any], *, dsn: str) -> int:
    """Start only explicitly approved, bounded C search jobs.

    Each job keeps C's live/permit-live/capacity gate. Empty configuration is
    a no-op, so installing D never consumes search traffic by itself.
    """
    schedule = dict(config.get("keywords") or {})
    jobs = list(schedule.get("jobs") or [])
    if not jobs:
        return 0
    if not bool(schedule.get("live_collection_enabled")):
        raise ValueError("keyword jobs require live_collection_enabled=true")
    from search_rank_worker import _load_config, run_live
    count = 0
    for job in jobs:
        if not isinstance(job, dict): raise ValueError("keyword job must be an object")
        keyword = str(job.get("keyword") or "").strip()
        targets = [str(value).upper() for value in (job.get("target_asins") or [])]
        depth = int(job.get("depth") or 0)
        source_path = Path(str(job.get("search_config_path") or ""))
        if not keyword or not targets or depth < 1 or not source_path.is_file():
            raise ValueError("keyword job requires keyword, target_asins, positive depth, and search_config_path")
        source = _load_config(source_path)
        if not bool(source.get("live_collection_enabled")):
            raise ValueError("search config live_collection_enabled is required")
        run_live(keyword=keyword, target_asins=targets, requested_depth=depth, run_id=f"watch-keyword-{int(time.time())}-{count}", config={**source, "tenant_id": config["tenant_id"]}, config_path=source_path, dsn=dsn)
        count += 1
    return count


def enqueue_due_products(*, dsn: str, tenant_id: str, config: dict[str, Any], storage_factory=None) -> int:
    if not config.get("enabled"):
        return 0
    approved_asins = [str(value).strip().upper() for value in (config.get("asins") or []) if str(value).strip()]
    if not 1 <= len(approved_asins) <= 5 or len(set(approved_asins)) != len(approved_asins):
        raise ValueError("enabled product scheduling requires 1 to 5 unique approved ASINs")
    if storage_factory is None:
        from postgres_worker_storage import PostgresWorkerStorage
        storage_factory = PostgresWorkerStorage
    storage = storage_factory(dsn, tenant_id=tenant_id, subject_type=str(config.get("subject_type") or "own"))
    return int(storage.enqueue_due_refreshes(
        min_age_hours=int(config.get("max_age_hours", 24)), limit=min(int(config.get("limit", 5)), len(approved_asins)), asins=approved_asins
    ))


def run_once(config: dict[str, Any], *, dsn: str, replay_path: Path | None = None, test_receipt_dir: Path | None = None) -> dict[str, int]:
    tenant_id = str(config["tenant_id"])
    marketplace = str(config.get("marketplace") or "US")
    delivery = WatchDeliveryRepository(dsn)
    pipeline = WatchPipeline(
        watch_repository=ProductWatchRepository(dsn), delivery_repository=delivery,
        tenant_id=tenant_id, marketplace=marketplace, rules=config.get("rules") or {}, reporting=config.get("reporting") or {},
    )
    result = {"observations": 0, "events": 0, "reports": pipeline.reconcile(), "claimed": 0, "sent": 0, "failed": 0, "unknown": 0, "queued_products": 0, "keyword_jobs": 0, "search_batches": 0, "search_observations": 0, "search_events": 0}
    product_schedule = dict(config.get("products") or {})
    result["queued_products"] = enqueue_due_products(dsn=dsn, tenant_id=tenant_id, config=product_schedule)
    result["keyword_jobs"] = _run_configured_keyword_jobs(config, dsn=dsn)
    ingested = PostgresSnapshotIngestor(pipeline).process_pending(limit=int((config.get("scheduling") or {}).get("snapshot_batch_limit", 50)))
    for name in ("observations", "events", "reports"):
        result[name] += int(ingested[name])
    result["snapshots"] = int(ingested["snapshots"])
    result["failed_attempts"] = int(ingested["failed_attempts"])
    recovered_search = SearchRankRecoveryIngestor(pipeline, dsn).process_pending(limit=int((config.get("scheduling") or {}).get("search_batch_limit", 20)))
    for name in ("search_batches", "search_observations", "search_events", "reports"):
        result[name] += int(recovered_search[name])
    if replay_path:
        rows = json.loads(replay_path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError("replay JSON must be an array")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("replay rows must be objects")
            processed = pipeline.process_snapshot(
                snapshot=dict(row.get("snapshot") or {}), evidence=dict(row.get("evidence") or {}),
                subject_type=str(row.get("subject_type") or "own"), requested_asin=str(row.get("requested_asin") or ""),
            )
            for name, value in processed.items(): result[name] = result.get(name, 0) + int(value)
    channel = LocalReceiptChannel(test_receipt_dir) if test_receipt_dir else UnconfiguredChannel()
    result.update(delivery.process_pending(tenant_id=tenant_id, channel=channel, limit=int((config.get("delivery") or {}).get("batch_limit", 10))))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--replay-json", type=Path, help="explicit offline fixture/replay input; makes no network requests")
    parser.add_argument("--test-receipt-dir", type=Path, help="explicit local test outlet; never an external recipient")
    parser.add_argument("--iterations", type=int, default=1, help="finite runs; 0 is rejected")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)
    try:
        if args.iterations < 1 or args.interval_seconds <= 0:
            raise ValueError("iterations and interval-seconds must be positive")
        dsn = os.environ.get(args.dsn_env, "").strip()
        if not dsn:
            raise ValueError(f"DSN is unavailable: {args.dsn_env}")
        config = load_watch_config(args.config)
        for iteration in range(args.iterations):
            print(json.dumps(run_once(config, dsn=dsn, replay_path=args.replay_json, test_receipt_dir=args.test_receipt_dir), ensure_ascii=False, sort_keys=True), flush=True)
            if iteration + 1 < args.iterations:
                time.sleep(args.interval_seconds)
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
