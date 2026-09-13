#!/usr/bin/env python3
"""Bounded search-rank acquisition, replay, persistence and product-watch handoff.

By default this CLI only replays explicitly supplied ``--input`` HTML.  Live
collection requires an explicit two-part opt-in and is capacity-gated.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import tomllib
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlencode
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from product_watch import ProductWatchRepository, detect_changes, normalize_keyword_rank
from raw_html_store import LocalRawHtmlStore
from search_rank_parser import project_target_positions, parse_search_cards, rank_cards_across_pages
from search_rank_storage import SearchRankStorage


PARSER_VERSION = "search-rank-v1"


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def search_url(keyword: str, page: int) -> str:
    if page < 1:
        raise ValueError("page must be positive")
    return "https://www.amazon.com/s?" + urlencode({"k": keyword, "page": page})


def batch_id_for(*, run_id: str, keyword: str, target_asins: Iterable[str], tenant_id: str = "default", marketplace: str = "US", search_conditions: Mapping[str, Any] | None = None) -> str:
    # A caller may explicitly retain this id to resume.  It has no hidden
    # dependence on current time, so replays do not duplicate completed pages.
    seed = json.dumps({"run_id": run_id, "keyword": keyword, "targets": sorted(str(value).upper() for value in target_asins), "tenant_id": tenant_id, "marketplace": marketplace, "search_conditions": dict(search_conditions or {})}, separators=(",", ":"), sort_keys=True)
    return "search-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _conditions(config: Mapping[str, Any]) -> dict[str, Any]:
    values = {
        "language": config.get("language"),
        "device": config.get("device"),
        "delivery_region": config.get("delivery_region"),
    }
    return {key: value for key, value in values.items() if value not in (None, "")}


def payloads_from_pages(*, keyword: str, target_asins: Iterable[str], pages: Mapping[int, list[dict[str, Any]]], requested_depth: int, search_conditions: Mapping[str, Any], evidence_ids: Iterable[str], observed_at: datetime) -> list[dict[str, Any]]:
    """Produce one product-watch payload per target from an ordered page set."""
    expected = set(range(1, requested_depth + 1))
    completed = set(pages)
    coverage_complete = completed == expected
    flattened = rank_cards_across_pages(dict(pages))
    projected = project_target_positions(flattened, target_asins)
    result: list[dict[str, Any]] = []
    for asin in sorted(projected):
        item = projected[asin]
        result.append({
            "requested_asin": asin,
            "observed_asin": item["observed_asin"],
            "keyword": keyword,
            "organic_position": item["organic_position"],
            "ad_position": item["ad_position"],
            "page": item["page"],
            "card_index": item["card_index"],
            "searched_depth": requested_depth,
            "coverage_complete": coverage_complete,
            "status": "ok" if coverage_complete else "partial",
            "search_conditions": dict(search_conditions),
            "observed_at": observed_at.isoformat(),
            "source_evidence_ids": sorted(evidence_ids),
            "matches": item["matches"],
        })
    return result


def run_replay(*, keyword: str, target_asins: list[str], page_html: Mapping[int, str], requested_depth: int, run_id: str, config: Mapping[str, Any], batch_id: str | None = None, storage: SearchRankStorage | Any | None = None, raw_store: LocalRawHtmlStore | None = None, watch_repository: ProductWatchRepository | Any | None = None, page_metadata: Mapping[int, Mapping[str, Any]] | None = None, failed_pages: Mapping[int, Mapping[str, Any]] | None = None, observed_at: datetime | None = None) -> dict[str, Any]:
    """Replay supplied HTML and optionally persist evidence and observations.

    Missing pages are an incomplete observation, not a claim that a target ranks
    outside the requested depth.  Existing completed pages are skipped when a
    storage instance provides the resume checkpoint.
    """
    if not keyword.strip() or not target_asins:
        raise ValueError("keyword and at least one target ASIN are required")
    if requested_depth < 1:
        raise ValueError("requested_depth must be positive")
    now = observed_at or utc_now()
    conditions = _conditions(config)
    batch_id = batch_id or batch_id_for(run_id=run_id, keyword=keyword, target_asins=target_asins, tenant_id=str(config.get("tenant_id") or "default"), marketplace=str(config.get("marketplace") or "US"), search_conditions=conditions)
    normalized_targets = sorted({value.upper() for value in target_asins})
    if storage:
        storage.start_batch(batch_id=batch_id, run_id=run_id, tenant_id=str(config.get("tenant_id") or "default"), marketplace=str(config.get("marketplace") or "US"), keyword=keyword, search_conditions=conditions, requested_depth=requested_depth, target_asins=normalized_targets)
        stored_pages = storage.load_pages(batch_id) if hasattr(storage, "load_pages") else {}
        completed_before = {page for page, value in stored_pages.items() if value.get("status") == "completed"}
    else:
        stored_pages = {}
        completed_before = set()
    parsed: dict[int, list[dict[str, Any]]] = {}
    evidence_ids: list[str] = []
    failures: list[int] = []
    charged_pages: set[int] = set()
    metadata = page_metadata or {}
    failures_metadata = failed_pages or {}
    for page in range(1, requested_depth + 1):
        existing = stored_pages.get(page)
        if existing:
            source = existing.get("source_evidence_id")
            if source:
                evidence_ids.append(str(source))
            if existing.get("status") == "completed":
                parsed[page] = list(existing.get("cards_json") or [])
            else:
                failures.append(page)
            continue
        if page in completed_before:
            # The checkpoint is evidence that this page was paid for already;
            # it must never be fetched again by a resume invocation.  Supplied
            # saved HTML may still be re-parsed locally to reconstruct the
            # batch result without any extra request or storage write.
            body = page_html.get(page)
            if body is not None:
                parsed[page] = parse_search_cards(body, page=page)
            elif hasattr(storage, "load_completed_cards"):
                parsed[page] = storage.load_completed_cards(batch_id, page)
            continue
        body = page_html.get(page)
        url = search_url(keyword, page)
        if body is None:
            failures.append(page)
            if storage:
                failure = dict(failures_metadata.get(page) or {})
                evidence_ids.append(storage.record_page(batch_id=batch_id, page=page, status="failed", url=url, retrieved_at=now, http_status=failure.get("http_status"), transfer_bytes=failure.get("transfer_bytes"), error_code=str(failure.get("error_code") or "page_input_missing"), evidence_context={"kind": "search_rank", "page": page, "elapsed_ms": failure.get("elapsed_ms"), "source_type": failure.get("source_type", "offline_replay")}))
            continue
        cards = parse_search_cards(body, page=page)
        parsed[page] = cards
        charged_pages.add(page)
        page_metric = dict(metadata.get(page) or {})
        transfer_bytes = page_metric.get("transfer_bytes", len(body.encode("utf-8")))
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        relative_path = None
        if raw_store is not None:
            relative_path = raw_store.put(run_id, "search-" + hashlib.sha256(keyword.encode("utf-8")).hexdigest()[:10], body)
        if storage:
            evidence_ids.append(storage.record_page(batch_id=batch_id, page=page, status="completed", url=url, retrieved_at=now, transfer_bytes=transfer_bytes, raw_html_path=str(relative_path) if relative_path else f"inline://{digest}", content_hash=digest, cards=cards, evidence_context={"kind": "search_rank", "page": page, "card_count": len(cards), "parser_version": PARSER_VERSION, "elapsed_ms": page_metric.get("elapsed_ms"), "source_type": page_metric.get("source_type", "offline_replay")}))
        else:
            evidence_ids.append(f"search-page:{batch_id}:{page}:{digest}")
    # A resumed run still has a useful result only after its caller supplies or
    # loads all page cards.  Do not construct artificial ranks from the subset.
    payloads = payloads_from_pages(keyword=keyword, target_asins=normalized_targets, pages=parsed, requested_depth=requested_depth, search_conditions=conditions, evidence_ids=evidence_ids or [f"search-batch:{batch_id}"], observed_at=now)
    observations = []
    events = []
    if watch_repository is not None:
        for payload in payloads:
            evidence = {"evidence_id": "|".join(payload["source_evidence_ids"]), "retrieved_at": now.isoformat(), "parser_version": PARSER_VERSION}
            observation = normalize_keyword_rank(payload, evidence, tenant_id=str(config.get("tenant_id") or "default"), marketplace=str(config.get("marketplace") or "US"), subject_type=str(config.get("subject_type") or "own"), requested_asin=payload["requested_asin"], observed_at=now)
            history = watch_repository.load_observations(tenant_id=observation.tenant_id, marketplace=observation.marketplace, subject_type=observation.subject_type, requested_asin=observation.requested_asin, observed_asin=observation.observed_asin, kind="keyword_rank")
            observations.append(observation)
            events.extend(detect_changes([observation], history, rules=dict(config.get("rules") or {})))
        watch_repository.persist_observations(observations)
        watch_repository.persist_events(events)
    complete = not failures and set(parsed) == set(range(1, requested_depth + 1))
    known_page_bytes = [metadata.get(page, {}).get("transfer_bytes", len(page_html[page].encode("utf-8"))) for page in charged_pages]
    metrics = {"requested_depth": requested_depth, "completed_pages": len(parsed), "newly_acquired_pages": len(charged_pages), "failed_or_unavailable_pages": sorted(set(failures)), "search_page_bytes": sum(int(value) for value in known_page_bytes if value is not None), "unknown_transfer_pages": sum(value is None for value in known_page_bytes), "product_observation_count": len(observations), "event_count": len(events)}
    if storage:
        storage.finish_batch(batch_id, status="completed" if complete else "partial", metrics=metrics)
    return {"batch_id": batch_id, "coverage_complete": complete, "payloads": payloads, "observations": observations, "events": events, "metrics": metrics}


def run_live(*, keyword: str, target_asins: list[str], requested_depth: int, run_id: str, config: Mapping[str, Any], config_path: Path, dsn: str, batch_id: str | None = None, raw_store: LocalRawHtmlStore | None = None) -> dict[str, Any]:
    """Fetch only unfinished pages behind the existing shared capacity gate.

    This function is never selected by default.  Product collection counters are
    not touched: search page byte totals are written only to ``search_rank_*``.
    """
    if not dsn:
        raise ValueError("live search requires an approved PostgreSQL DSN for capacity and evidence")
    from amazon_us_worker import AdapterFetchError, DEFAULTS, FallbackReason, _build_http_adapter, classify_block
    from postgres_worker_storage import PostgresWorkerStorage
    from proxy_capacity_gate import reserve_capacity

    worker_config = {**DEFAULTS, **dict(config.get("worker") or {})}
    tenant_id = str(config.get("tenant_id") or "default")
    worker_config["tenant_id"] = tenant_id
    storage = SearchRankStorage(dsn)
    watch = ProductWatchRepository(dsn)
    conditions = _conditions(config)
    resolved_batch_id = batch_id or batch_id_for(run_id=run_id, keyword=keyword, target_asins=target_asins, tenant_id=tenant_id, marketplace=str(config.get("marketplace") or "US"), search_conditions=conditions)
    storage.start_batch(batch_id=resolved_batch_id, run_id=run_id, tenant_id=tenant_id, marketplace=str(config.get("marketplace") or "US"), keyword=keyword, search_conditions=_conditions(config), requested_depth=requested_depth, target_asins=sorted({value.upper() for value in target_asins}))
    existing = storage.load_pages(resolved_batch_id)
    pending = [page for page in range(1, requested_depth + 1) if page not in existing]
    if not pending:
        return run_replay(keyword=keyword, target_asins=target_asins, page_html={}, requested_depth=requested_depth, run_id=run_id, config=config, batch_id=resolved_batch_id, storage=storage, raw_store=raw_store, watch_repository=watch)
    capacity_storage = PostgresWorkerStorage(dsn, tenant_id=tenant_id, subject_type=str(config.get("subject_type") or "own"))
    reservation = None
    adapter = None
    try:
        adapter = _build_http_adapter(worker_config)
        reservation = reserve_capacity(config_path, storage=capacity_storage, requested_actions=len(pending) or 1, owner_id=f"search-rank:{resolved_batch_id}", lease_seconds=600, reservation_id=f"search-{resolved_batch_id}")
        configure = getattr(adapter, "configure_capacity_reservation", None)
        if not callable(configure):
            raise RuntimeError("search live collection requires ProxySessionPool capacity binding")
        owner = f"search-rank:{resolved_batch_id}"
        def validate() -> dict[str, Any]:
            current = capacity_storage.validate_proxy_capacity_reservation(str(reservation["reservation_id"]), owner, max_age_seconds=int(worker_config.get("proxy_canary_max_age_seconds") or 3600), lease_seconds=600)
            if current.get("status") != "active":
                raise RuntimeError("search_capacity_reservation_inactive")
            return current
        configure(list(reservation.get("slot_ids") or []), validate)
        adapter.begin_run(run_id, tenant_id, f"search-rank:{resolved_batch_id}")
        for page in pending:
            adapter.begin_action()
            url = search_url(keyword, page)
            started = time.perf_counter()
            try:
                body, status = adapter.fetch(url)
            except AdapterFetchError:
                try:
                    body, status = adapter.fetch_browser(url, fallback_reason=FallbackReason.HTTP_TRANSPORT_ERROR, run_id=run_id, asin=f"S{page:09d}")
                except AdapterFetchError:
                    storage.record_page(batch_id=resolved_batch_id, page=page, status="failed", url=url, retrieved_at=utc_now(), transfer_bytes=adapter.last_transfer_bytes, error_code="fetch_error", evidence_context={"kind": "search_rank", "page": page, "elapsed_ms": int((time.perf_counter() - started) * 1000), "source_type": adapter.source_type})
                    continue
            block_reason = classify_block(status, body)
            if block_reason:
                try:
                    body, status = adapter.fetch_browser(url, fallback_reason=FallbackReason.ACCESS_CONTROL_VERIFICATION, run_id=run_id, asin=f"S{page:09d}")
                    block_reason = classify_block(status, body)
                except AdapterFetchError:
                    block_reason = block_reason or "browser_verification_failed"
            if status is None or status >= 400 or block_reason:
                storage.record_page(batch_id=resolved_batch_id, page=page, status="failed", url=url, retrieved_at=utc_now(), http_status=status, transfer_bytes=adapter.last_transfer_bytes, error_code=f"access_block:{block_reason}" if block_reason else "http_error", evidence_context={"kind": "search_rank", "page": page, "elapsed_ms": int((time.perf_counter() - started) * 1000), "source_type": adapter.source_type})
                continue
            cards = parse_search_cards(body, page=page)
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            try:
                relative_path = raw_store.put(run_id, "search-" + hashlib.sha256(keyword.encode("utf-8")).hexdigest()[:10], body) if raw_store is not None else f"inline://{digest}"
            except OSError:
                # The request has already happened.  Checkpoint a terminal
                # failure before continuing so resume cannot silently pay for
                # it again without an explicit new batch.
                storage.record_page(batch_id=resolved_batch_id, page=page, status="failed", url=url, retrieved_at=utc_now(), http_status=status, transfer_bytes=adapter.last_transfer_bytes, error_code="raw_evidence_store_failed", evidence_context={"kind": "search_rank", "page": page, "card_count": len(cards), "parser_version": PARSER_VERSION, "elapsed_ms": int((time.perf_counter() - started) * 1000), "source_type": adapter.source_type})
                continue
            storage.record_page(batch_id=resolved_batch_id, page=page, status="completed", url=url, retrieved_at=utc_now(), http_status=status, transfer_bytes=adapter.last_transfer_bytes, raw_html_path=str(relative_path), content_hash=digest, cards=cards, evidence_context={"kind": "search_rank", "page": page, "card_count": len(cards), "parser_version": PARSER_VERSION, "elapsed_ms": int((time.perf_counter() - started) * 1000), "source_type": adapter.source_type})
        # Every request has been checkpointed above.  This final local-only pass
        # reloads durable cards/evidence and emits product-watch observations.
        return run_replay(keyword=keyword, target_asins=target_asins, page_html={}, requested_depth=requested_depth, run_id=run_id, config=config, batch_id=resolved_batch_id, storage=storage, raw_store=raw_store, watch_repository=watch)
    except Exception:
        storage.finish_batch(resolved_batch_id, status="failed", metrics={"requested_depth": requested_depth, "failure": "live_setup_or_fetch_failed"})
        raise
    finally:
        try:
            if adapter is not None:
                release_adapter = getattr(adapter, "release_capacity_reservation", None)
                if callable(release_adapter):
                    release_adapter()
                else:
                    adapter.close()
        finally:
            if reservation is not None:
                capacity_storage.release_proxy_capacity(str(reservation["reservation_id"]), f"search-rank:{resolved_batch_id}")


def _parse_input(values: list[str]) -> dict[int, str]:
    pages: dict[int, str] = {}
    for value in values:
        page_text, separator, path_text = value.partition(":")
        if not separator or not page_text.isdigit():
            raise ValueError("--input must be PAGE:PATH")
        page = int(page_text)
        if page in pages:
            raise ValueError(f"duplicate input for page {page}")
        pages[page] = Path(path_text).read_text(encoding="utf-8", errors="replace")
    return pages


def _load_config(path: Path) -> dict[str, Any]:
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    return {**dict(document.get("search") or {}), "worker": dict(document.get("worker") or {})}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--target-asin", action="append", required=True)
    parser.add_argument("--depth", type=int)
    parser.add_argument("--run-id")
    parser.add_argument("--batch-id")
    parser.add_argument("--input", action="append", default=[], metavar="PAGE:PATH")
    parser.add_argument("--live", action="store_true", help="run bounded network collection after explicit config approval")
    parser.add_argument("--permit-live", action="store_true", help="second explicit acknowledgement required with --live")
    parser.add_argument("--dsn-env", default="AMAZON_TEST_POSTGRES_DSN")
    parser.add_argument("--raw-html-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _load_config(args.config)
        depth = args.depth or int(config.get("search_depth") or 0)
        if depth < 1:
            raise ValueError("--depth or search_depth must be positive")
        dsn = __import__("os").environ.get(args.dsn_env, "").strip()
        raw_root = args.raw_html_dir or Path(str(config.get("raw_html_dir") or "outputs/search-rank-raw"))
        run_id = args.run_id or "search-" + uuid.uuid4().hex
        if args.live:
            if args.input or not args.permit_live or not bool(config.get("live_collection_enabled")):
                raise ValueError("--live requires --permit-live, live_collection_enabled=true, and no --input")
            result = run_live(keyword=args.keyword, target_asins=args.target_asin, requested_depth=depth, run_id=run_id, config=config, config_path=args.config, dsn=dsn, batch_id=args.batch_id, raw_store=LocalRawHtmlStore(raw_root))
        else:
            if not args.input:
                raise ValueError("this entrypoint is offline by default; provide --input PAGE:PATH")
            pages = _parse_input(args.input)
            storage = SearchRankStorage(dsn) if dsn else None
            watch = ProductWatchRepository(dsn) if dsn else None
            result = run_replay(keyword=args.keyword, target_asins=args.target_asin, page_html=pages, requested_depth=depth, run_id=run_id, config=config, batch_id=args.batch_id, storage=storage, raw_store=LocalRawHtmlStore(raw_root), watch_repository=watch)
    except (OSError, ValueError, RuntimeError, tomllib.TOMLDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"batch_id": result["batch_id"], "coverage_complete": result["coverage_complete"], "metrics": result["metrics"]}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
