#!/usr/bin/env python3
"""Consume independent PostgreSQL Woot review jobs without touching PDP state."""

from __future__ import annotations

import argparse
import json
import os
import socket
from pathlib import Path
from typing import Any, Callable, Mapping

from postgres_review_storage import PostgresReviewStorage
from woot_review_provider import DEFAULT_USER_AGENT, WootProviderError, build_plan, execute_collection


def consume_jobs(
    storage: PostgresReviewStorage,
    *,
    output_root: Path,
    limit: int,
    worker_id: str,
    lease_seconds: int = 3600,
    mode: str = "adaptive",
    max_requests: int = 200,
    request_delay_seconds: float = 1.0,
    user_agent: str = DEFAULT_USER_AGENT,
    plan_builder: Callable[..., Mapping[str, Any]] = build_plan,
    collector: Callable[..., Mapping[str, Any]] = execute_collection,
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be positive")
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "status": "success",
        "tenant_id": storage.tenant_id,
        "provider": storage.provider,
        "worker_id": worker_id,
        "claimed": 0,
        "succeeded": 0,
        "source_limited": 0,
        "retryable": 0,
        "failed": 0,
        "blocked": 0,
        "reviews": 0,
        "requests": 0,
        "response_body_bytes": 0,
        "stopped_on_provider_block": False,
        "pdp_truth_written": False,
        "database_writes_performed": True,
        "items": [],
    }
    for _ in range(limit):
        task = storage.claim_job(worker_id, lease_seconds=lease_seconds)
        if task is None:
            break
        summary["claimed"] += 1
        asin = str(task["asin"])
        try:
            plan = plan_builder(
                asin,
                mode=mode,
                max_requests=max_requests,
                request_delay_seconds=request_delay_seconds,
                user_agent=user_agent,
            )
            receipt = collector(plan, output_root=output_root)
        except WootProviderError as exc:
            result = storage.save_failure(
                task,
                error_code=exc.code,
                run_dir=getattr(exc, "run_dir", None),
            )
            summary[result["status"]] += 1
            summary["items"].append(result)
            if result["status"] == "blocked":
                summary["status"] = "blocked"
                summary["stopped_on_provider_block"] = True
                break
            continue
        except Exception as exc:
            result = storage.save_failure(
                task,
                error_code=getattr(exc, "code", type(exc).__name__),
                run_dir=getattr(exc, "run_dir", None),
            )
            summary[result["status"]] += 1
            summary["items"].append(result)
            continue
        # Database/lease errors are deliberately outside the provider catch.
        # They terminate the worker so lease recovery can safely retry later;
        # a successful provider receipt must never be relabelled as a fetch failure.
        result = storage.save_success(task, receipt["run_dir"])
        summary[result["status"]] += 1
        summary["reviews"] += int(result["review_count"])
        summary["requests"] += int(result["request_count"])
        summary["response_body_bytes"] += int(result["response_body_bytes"])
        summary["items"].append(result)
    if not summary["stopped_on_provider_block"] and (summary["failed"] or summary["retryable"]):
        summary["status"] = "partial_success" if summary["succeeded"] or summary["source_limited"] else "failed"
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the independent PostgreSQL Woot review worker")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--expected-database", default="postgres")
    parser.add_argument("--output-root", type=Path, default=Path("outputs") / "woot-review-worker")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--worker-id", default=f"review-{socket.gethostname()}-{os.getpid()}")
    parser.add_argument("--lease-seconds", type=int, default=3600)
    parser.add_argument("--mode", choices=("basic", "full", "max", "adaptive"), default="adaptive")
    parser.add_argument("--max-requests", type=int, default=200)
    parser.add_argument("--request-delay-seconds", type=float, default=1.0)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument(
        "--schedule-existing",
        action="store_true",
        help="mark all existing completed review jobs due before consuming them",
    )
    parser.add_argument(
        "--schedule-asins",
        help="comma-separated existing ASINs to mark due before consuming",
    )
    parser.add_argument(
        "--schedule-oldest",
        type=int,
        help="mark at most this many oldest eligible review jobs due before consuming",
    )
    parser.add_argument("--min-age-hours", type=int, default=24)
    parser.add_argument("--schedule-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        storage = PostgresReviewStorage.from_environment(
            args.tenant_id,
            dsn_env=args.dsn_env,
            expected_database=args.expected_database,
        )
        schedule_modes = sum(bool(value) for value in (
            args.schedule_existing, args.schedule_asins, args.schedule_oldest,
        ))
        if schedule_modes > 1:
            raise ValueError("choose one review scheduling mode")
        if args.schedule_oldest:
            scheduled = storage.schedule_oldest(
                limit=args.schedule_oldest,
                min_age_hours=args.min_age_hours,
            )
        elif args.schedule_asins:
            scheduled = storage.schedule_jobs(args.schedule_asins.split(","))
        else:
            scheduled = storage.schedule_all_existing() if args.schedule_existing else 0
        if args.schedule_only:
            payload = {
                "status": "scheduled",
                "tenant_id": args.tenant_id,
                "scheduled": scheduled,
                "pdp_truth_written": False,
            }
        else:
            payload = consume_jobs(
                storage,
                output_root=args.output_root,
                limit=args.limit,
                worker_id=args.worker_id,
                lease_seconds=args.lease_seconds,
                mode=args.mode,
                max_requests=args.max_requests,
            request_delay_seconds=args.request_delay_seconds,
            user_agent=args.user_agent,
            )
            payload["scheduled"] = scheduled
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if payload["status"] in {"success", "scheduled"} else 2
    except (ValueError, RuntimeError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
