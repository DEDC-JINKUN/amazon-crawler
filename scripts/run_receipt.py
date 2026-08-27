#!/usr/bin/env python3
"""Build one read-only, auditable receipt for an Amazon US collection run."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def compose_receipt(
    verification: dict[str, Any],
    verification_errors: list[str],
    collection: dict[str, Any],
    cost: dict[str, Any] | None,
) -> dict[str, Any]:
    state = verification.get("state", {})
    coverage = verification.get("coverage", {})
    blocked = verification.get("blocked", [])
    return {
        "schema_version": "amazon-us-run-receipt-v1",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "run_id": collection.get("run_id"),
        "verification": {
            "ok": not verification_errors and bool(verification.get("ok")),
            "phase": verification.get("phase", "unknown"),
            "collection_phase": verification.get("collection_phase", "unknown"),
            "manifest_count": verification.get("manifest", {}).get("count", 0),
            "state_count": state.get("count", 0),
            "status_counts": state.get("status_counts", {}),
            "missing_state_count": coverage.get("missing_state", 0),
            "extra_state_count": coverage.get("extra_state", 0),
            "blocked_count": len(blocked),
            "exhausted_failed_count": len(verification.get("exhausted_failed", [])),
            "error_count": len(verification_errors),
            "errors": verification_errors,
        },
        "action_items": {
            "blocked_asins": [item.get("asin") for item in verification.get("blocked", []) if item.get("asin")],
            "failed_asins": [item.get("asin") for item in verification.get("failed", []) if item.get("status") == "failed" and item.get("asin")],
            "exhausted_failed_asins": list(verification.get("exhausted_failed", [])),
            "failure_details": list(verification.get("failed", [])),
        },
        "collection_metrics": collection,
        "cost": cost,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "amazon_us_asin_manifest.csv")
    parser.add_argument("--state", type=Path, default=ROOT / "state" / "amazon_us.sqlite3")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "amazon_us")
    parser.add_argument("--raw-html-dir", type=Path, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--target-asins", type=int, default=5800)
    parser.add_argument("--budget-cny", type=float, default=200.0)
    parser.add_argument("--proxy-usage-before-bytes", type=int)
    parser.add_argument("--proxy-usage-after-bytes", type=int)
    parser.add_argument("--proxy-price-cny-per-gb", type=float)
    parser.add_argument("--proxy-charge-cny", type=float)
    parser.add_argument("--allocated-fixed-cost-cny", type=float, default=0.0)
    parser.add_argument("--business-units", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        verifier = _load("run_receipt_verifier", ROOT / "scripts" / "amazon_us_verify.py")
        metrics_module = _load("run_receipt_metrics", ROOT / "scripts" / "collection_metrics.py")
        cost_module = _load("run_receipt_cost", ROOT / "scripts" / "traffic_cost_report.py")
        verification, verification_errors = verifier.verify(args.manifest, args.state, args.output_dir, args.expected_count)
        collection = metrics_module.build_report(args.state, args.raw_html_dir, args.run_id)
        cost = cost_module.build_cost_report(
            collection,
            target_asins=args.target_asins,
            budget_cny=args.budget_cny,
            proxy_usage_before_bytes=args.proxy_usage_before_bytes,
            proxy_usage_after_bytes=args.proxy_usage_after_bytes,
            proxy_price_cny_per_gb=args.proxy_price_cny_per_gb,
            proxy_charge_cny=args.proxy_charge_cny,
            allocated_fixed_cost_cny=args.allocated_fixed_cost_cny,
            business_units=args.business_units,
        )
        receipt = compose_receipt(verification, verification_errors, collection, cost)
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as exc:
        print(f"run receipt failed: {exc}", file=sys.stderr)
        return 2
    encoded = json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        _write_atomic(args.output, encoded)
    print(encoded, end="")
    return 0 if receipt["verification"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
