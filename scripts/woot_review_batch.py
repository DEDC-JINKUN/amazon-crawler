#!/usr/bin/env python3
"""Resumable, globally rate-limited Woot review batch runner for XLSX ASIN lists."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from woot_review_provider import (
    WootProviderError,
    _default_transport,
    build_plan as build_asin_plan,
    execute_collection,
    query_matrix,
)
from xlsx_asin_reader import read_asins


BATCH_PLAN_SCHEMA = "woot-review-batch-plan-v1"
BATCH_SUMMARY_SCHEMA = "woot-review-batch-summary-v1"
STOP_CODES = {"provider_blocked", "provider_rate_limited"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_new_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _replace_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(dict(value), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def build_batch_plan(
    workbook_path: str | Path,
    *,
    mode: str = "max",
    concurrency: int = 3,
    min_request_interval_seconds: float = 0.25,
    per_asin_max_requests: int = 200,
    start_index: int = 0,
    limit: int | None = None,
    skip_success_root: str | Path | None = None,
) -> dict[str, Any]:
    source = Path(workbook_path).resolve()
    workbook = read_asins(source)
    if concurrency < 1 or concurrency > 8:
        raise ValueError("concurrency must be between 1 and 8")
    if min_request_interval_seconds < 0.1:
        raise ValueError("min_request_interval_seconds must be at least 0.1")
    minimum_requests = len(query_matrix(mode))
    if per_asin_max_requests < minimum_requests:
        raise ValueError(
            f"per_asin_max_requests must be at least {minimum_requests} for {mode} mode"
        )
    if start_index < 0 or start_index >= len(workbook.asins):
        raise ValueError("start_index is outside the ASIN list")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    selected = workbook.asins[start_index:]
    if limit is not None:
        selected = selected[:limit]
    skip_root = Path(skip_success_root).resolve() if skip_success_root is not None else None
    prior_successes = _successful_asins(skip_root) if skip_root is not None else set()
    prior_successes.intersection_update(workbook.asins)
    selected = [asin for asin in selected if asin not in prior_successes]
    approval = {
        "schema_version": BATCH_PLAN_SCHEMA,
        "provider": "woot_reviews",
        "marketplace": "US",
        "source_path": str(source),
        "source_filename": workbook.source_filename,
        "source_sha256": workbook.source_sha256,
        "source_sheet": workbook.sheet,
        "source_column": workbook.column,
        "source_unique_asins": len(workbook.asins),
        "start_index": start_index,
        "limit": limit,
        "skip_success_root": str(skip_root) if skip_root is not None else None,
        "prior_success_asins": len(prior_successes),
        "asins": selected,
        "selected_asins": len(selected),
        "mode": mode,
        "concurrency": concurrency,
        "min_request_interval_seconds": min_request_interval_seconds,
        "per_asin_max_requests": per_asin_max_requests,
        "stop_after_consecutive_failures": 3,
        "stop_immediately_on": sorted(STOP_CODES),
        "database_writes_performed": 0,
        "pdp_truth_written": False,
    }
    return approval | {"plan_hash": hashlib.sha256(_canonical(approval)).hexdigest()}


class GlobalRateLimitedTransport:
    def __init__(
        self,
        interval_seconds: float,
        stop_event: threading.Event,
        transport: Callable = _default_transport,
    ):
        self.interval_seconds = interval_seconds
        self.stop_event = stop_event
        self.transport = transport
        self._lock = threading.Lock()
        self._last_request_started = 0.0

    def __call__(self, request, timeout):
        if self.stop_event.is_set():
            raise WootProviderError("batch_stopped", "batch stop gate is active")
        with self._lock:
            if self.stop_event.is_set():
                raise WootProviderError("batch_stopped", "batch stop gate is active")
            remaining = self.interval_seconds - (time.monotonic() - self._last_request_started)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request_started = time.monotonic()
        return self.transport(request, timeout)


def _canonical_batch_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    return build_batch_plan(
        str(plan.get("source_path") or ""),
        mode=str(plan.get("mode") or "max"),
        concurrency=int(plan.get("concurrency") or 0),
        min_request_interval_seconds=float(plan.get("min_request_interval_seconds") or 0),
        per_asin_max_requests=int(plan.get("per_asin_max_requests") or 0),
        start_index=int(plan.get("start_index") or 0),
        limit=plan.get("limit"),
        skip_success_root=plan.get("skip_success_root"),
    )


def _load_outcomes(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    outcomes = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WootProviderError("invalid_progress_ledger", f"invalid JSONL line {number}") from exc
            if not isinstance(value, dict) or not value.get("asin"):
                raise WootProviderError("invalid_progress_ledger", f"invalid outcome line {number}")
            outcomes.append(value)
    return outcomes


def _successful_asins(root: Path) -> set[str]:
    if not root.is_dir():
        raise ValueError(f"skip_success_root is not a directory: {root}")
    successful: set[str] = set()
    for progress_path in sorted(root.rglob("progress.jsonl")):
        for outcome in _load_outcomes(progress_path):
            if outcome.get("status") == "success":
                successful.add(str(outcome["asin"]).strip().upper())
    return successful


def _outcome_from_error(asin: str, root: Path, exc: Exception) -> dict[str, Any]:
    failed_receipts = sorted(root.rglob("receipt.failed.json"), key=lambda path: path.stat().st_mtime_ns)
    run_dir = str(failed_receipts[-1].parent) if failed_receipts else None
    return {
        "asin": asin,
        "status": "failed",
        "error_code": getattr(exc, "code", type(exc).__name__),
        "http_status": getattr(exc, "http_status", None),
        "error": str(exc),
        "run_dir": run_dir,
        "coverage_status": "blocked",
        "fetched_unique_count": 0,
        "evidence_files": None,
        "completed_at": _iso(_utc_now()),
    }


def _run_one(
    asin: str,
    plan: Mapping[str, Any],
    batch_dir: Path,
    transport: Callable,
    collector: Callable = execute_collection,
) -> dict[str, Any]:
    asin_plan = build_asin_plan(
        asin,
        mode=str(plan["mode"]),
        expected_written_reviews=None,
        expected_count_source=None,
        max_requests=int(plan["per_asin_max_requests"]),
        request_delay_seconds=0,
    )
    asin_root = batch_dir / "asins" / asin
    try:
        receipt = collector(asin_plan, output_root=asin_root, transport=transport)
    except Exception as exc:
        return _outcome_from_error(asin, asin_root, exc)
    coverage = receipt.get("coverage") or {}
    return {
        "asin": asin,
        "status": "success",
        "error_code": None,
        "http_status": None,
        "error": None,
        "run_dir": receipt.get("run_dir"),
        "coverage_status": coverage.get("status"),
        "fetched_unique_count": coverage.get("fetched_unique_count", 0),
        "evidence_files": receipt.get("evidence_files"),
        "completed_at": _iso(_utc_now()),
    }


def _summary(
    plan: Mapping[str, Any],
    outcomes: list[dict[str, Any]],
    *,
    batch_id: str,
    started_at: datetime,
    stop_reason: str | None,
) -> dict[str, Any]:
    selected = len(plan["asins"])
    succeeded = sum(item.get("status") == "success" for item in outcomes)
    failed = sum(item.get("status") == "failed" for item in outcomes)
    fetched = sum(int(item.get("fetched_unique_count") or 0) for item in outcomes)
    coverage_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    for item in outcomes:
        coverage = str(item.get("coverage_status") or "unknown")
        coverage_counts[coverage] = coverage_counts.get(coverage, 0) + 1
        if item.get("error_code"):
            code = str(item["error_code"])
            error_counts[code] = error_counts.get(code, 0) + 1
    status = "aborted" if stop_reason else "success" if succeeded == selected else "partial_success"
    return {
        "schema_version": BATCH_SUMMARY_SCHEMA,
        "batch_id": batch_id,
        "plan_hash": plan["plan_hash"],
        "status": status,
        "started_at": _iso(started_at),
        "updated_at": _iso(_utc_now()),
        "selected_asins": selected,
        "processed_asins": len(outcomes),
        "remaining_asins": selected - len(outcomes),
        "succeeded_asins": succeeded,
        "failed_asins": failed,
        "fetched_unique_reviews": fetched,
        "coverage_status_counts": coverage_counts,
        "error_counts": error_counts,
        "stop_reason": stop_reason,
        "database_writes_performed": 0,
        "pdp_truth_written": False,
    }


def execute_batch(
    plan: Mapping[str, Any],
    *,
    output_root: Path,
    resume_dir: Path | None = None,
    transport_factory: Callable[[threading.Event], Callable] | None = None,
    collector: Callable = execute_collection,
    now: datetime | None = None,
) -> dict[str, Any]:
    canonical = _canonical_batch_plan(plan)
    if dict(plan) != canonical:
        raise WootProviderError("plan_mismatch", "batch plan no longer matches the workbook or arguments")
    started_at = now or _utc_now()
    if resume_dir is None:
        batch_id = f"woot-batch-{started_at.strftime('%Y%m%dT%H%M%S%fZ')}-{plan['plan_hash'][:8]}-{uuid.uuid4().hex[:6]}"
        batch_dir = Path(output_root) / batch_id
        batch_dir.mkdir(parents=True, exist_ok=False)
        _write_new_json(batch_dir / "plan.json", dict(plan))
    else:
        batch_dir = Path(resume_dir)
        saved = json.loads((batch_dir / "plan.json").read_text(encoding="utf-8"))
        if saved != dict(plan):
            raise WootProviderError("plan_mismatch", "resume directory plan does not match")
        batch_id = batch_dir.name
    progress_path = batch_dir / "progress.jsonl"
    outcomes = _load_outcomes(progress_path)
    seen = {str(item["asin"]) for item in outcomes}
    remaining = [asin for asin in plan["asins"] if asin not in seen]
    stop_event = threading.Event()
    if transport_factory is None:
        transport = GlobalRateLimitedTransport(
            float(plan["min_request_interval_seconds"]), stop_event
        )
    else:
        transport = transport_factory(stop_event)
    stop_reason = None
    consecutive_failures = 0

    with ThreadPoolExecutor(max_workers=int(plan["concurrency"])) as pool:
        pending: dict[Future, str] = {}
        iterator = iter(remaining)
        for _ in range(int(plan["concurrency"])):
            asin = next(iterator, None)
            if asin is None:
                break
            pending[pool.submit(_run_one, asin, plan, batch_dir, transport, collector)] = asin
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                asin = pending.pop(future)
                try:
                    outcome = future.result()
                except Exception as exc:
                    outcome = _outcome_from_error(asin, batch_dir / "asins" / asin, exc)
                outcomes.append(outcome)
                _append_jsonl(progress_path, outcome)
                if outcome["status"] == "success":
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    code = str(outcome.get("error_code") or "unknown")
                    if code in STOP_CODES:
                        stop_reason = code
                    elif consecutive_failures >= int(plan["stop_after_consecutive_failures"]):
                        stop_reason = f"consecutive_failures:{code}"
                if stop_reason:
                    stop_event.set()
                    continue
                next_asin = next(iterator, None)
                if next_asin is not None:
                    pending[pool.submit(_run_one, next_asin, plan, batch_dir, transport, collector)] = next_asin
            checkpoint = _summary(
                plan, outcomes, batch_id=batch_id, started_at=started_at, stop_reason=stop_reason
            )
            _replace_json(batch_dir / "batch_summary.json", checkpoint)
    result = _summary(plan, outcomes, batch_id=batch_id, started_at=started_at, stop_reason=stop_reason)
    _replace_json(batch_dir / "batch_summary.json", result)
    return result | {"batch_dir": str(batch_dir)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan or execute Woot reviews for an XLSX ASIN list")
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--mode", choices=("basic", "full", "max", "adaptive"), default="max")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--min-request-interval-seconds", type=float, default=0.25)
    parser.add_argument("--per-asin-max-requests", type=int, default=200)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--skip-success-root", type=Path)
    parser.add_argument("--output-root", type=Path, default=Path("outputs") / "woot-review-batches")
    parser.add_argument("--resume-dir", type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = build_batch_plan(
            args.workbook,
            mode=args.mode,
            concurrency=args.concurrency,
            min_request_interval_seconds=args.min_request_interval_seconds,
            per_asin_max_requests=args.per_asin_max_requests,
            start_index=args.start_index,
            limit=args.limit,
            skip_success_root=args.skip_success_root,
        )
        if not args.execute:
            print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        result = execute_batch(
            plan,
            output_root=args.output_root,
            resume_dir=args.resume_dir,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["status"] == "success" else 5
    except (ValueError, WootProviderError, OSError, json.JSONDecodeError) as exc:
        print(f"woot review batch failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
