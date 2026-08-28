#!/usr/bin/env python3
"""Convert one collection run and proxy usage delta into comparable cost metrics."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    from collection_metrics import build_report as build_collection_report
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from collection_metrics import build_report as build_collection_report


DECIMAL_GB_BYTES = 1_000_000_000
MIB_BYTES = 1024 * 1024


def build_cost_report(
    collection: dict[str, Any],
    *,
    target_asins: int = 5800,
    budget_cny: float = 200.0,
    proxy_usage_before_bytes: int | None = None,
    proxy_usage_after_bytes: int | None = None,
    proxy_price_cny_per_gb: float | None = None,
    proxy_charge_cny: float | None = None,
    allocated_fixed_cost_cny: float = 0.0,
    business_units: int | None = None,
) -> dict[str, Any]:
    if target_asins <= 0:
        raise ValueError("target_asins must be positive")
    if budget_cny < 0:
        raise ValueError("budget_cny must not be negative")
    if (proxy_usage_before_bytes is None) != (proxy_usage_after_bytes is None):
        raise ValueError("proxy usage before and after must be supplied together")
    if proxy_price_cny_per_gb is not None and proxy_price_cny_per_gb < 0:
        raise ValueError("proxy_price_cny_per_gb must not be negative")
    if proxy_charge_cny is not None and proxy_charge_cny < 0:
        raise ValueError("proxy_charge_cny must not be negative")
    if allocated_fixed_cost_cny < 0:
        raise ValueError("allocated_fixed_cost_cny must not be negative")
    if business_units is not None and business_units < 0:
        raise ValueError("business_units must not be negative")

    successful_pages = int(collection.get("success_page_count") or 0)
    unique_asins = int(collection.get("unique_asin_count") or 0)
    successful_asins = int(collection["unique_successful_asin_count"]) if "unique_successful_asin_count" in collection else min(unique_asins, successful_pages)
    saved_body_bytes = int(collection.get("bytes_total") or 0)
    transfer_bytes = int(collection.get("transfer_bytes_total") or 0)
    denominator = successful_asins
    evidence_pages = int(collection.get("evidence_count") or 0)
    table_counts = {str(key): int(value or 0) for key, value in (collection.get("table_counts") or {}).items()}
    normalized_rows = sum(table_counts.values())

    def _scale(observed: int, definition: str) -> dict[str, Any]:
        return {
            "definition": definition,
            "observed": observed,
            "per_successful_asin": round(observed / denominator, 4) if denominator else None,
            "projected_for_target_asins": round(observed / denominator * target_asins, 2) if denominator else None,
        }

    result: dict[str, Any] = {
        "schema_version": "amazon-us-traffic-cost-v1",
        "run_id": collection.get("run_id"),
        "measurement_scope": {
            "successful_pages": successful_pages,
            "unique_asins": unique_asins,
            "unique_successful_asins": successful_asins,
            "saved_body_bytes": saved_body_bytes,
            "target_asins": target_asins,
            "budget_cny": budget_cny,
        },
        "local_body_estimate": {
            "saved_body_mib": round(saved_body_bytes / MIB_BYTES, 4),
            "mib_per_successful_page": round(saved_body_bytes / successful_pages / MIB_BYTES, 4) if successful_pages else None,
            "mib_per_unique_asin": round(saved_body_bytes / unique_asins / MIB_BYTES, 4) if unique_asins else None,
            "mib_per_successful_asin": round(saved_body_bytes / successful_asins / MIB_BYTES, 4) if successful_asins else None,
            "projected_gib_for_target_asins": round(saved_body_bytes / denominator * target_asins / (1024 ** 3), 4) if denominator else None,
            "known_transfer_bytes": transfer_bytes,
            "known_transfer_gib": round(transfer_bytes / (1024 ** 3), 4) if transfer_bytes else None,
            "projected_transfer_gib_for_target_asins": round(transfer_bytes / denominator * target_asins / (1024 ** 3), 4) if transfer_bytes and denominator else None,
            "transfer_bytes_missing_count": int(collection.get("transfer_bytes_missing_count") or 0),
            "is_proxy_bill": False,
        },
        "scale_estimates": {
            "pages": _scale(evidence_pages, "evidence/page actions in this run"),
            "asins": _scale(successful_asins, "ASINs with a validated product page"),
            "database_rows": _scale(normalized_rows, "current normalized rows for ASINs touched by this run; not a delta log"),
            "field_values": ({
                **_scale(int(business_units), "business-defined non-empty field values"),
                "business_definition_required": True,
            } if business_units is not None else {
                "definition": "business-defined non-empty field values",
                "observed": None,
                "per_successful_asin": None,
                "projected_for_target_asins": None,
                "business_definition_required": True,
            }),
        },
        "proxy_bill_measurement": None,
    }

    if proxy_usage_before_bytes is not None and proxy_usage_after_bytes is not None:
        billed_bytes = proxy_usage_after_bytes - proxy_usage_before_bytes
        if billed_bytes < 0:
            raise ValueError("proxy usage after must be greater than or equal to before")
        billed_gb = billed_bytes / DECIMAL_GB_BYTES
        proxy: dict[str, Any] = {
            "source_unit": "bytes",
            "usage_before_bytes": proxy_usage_before_bytes,
            "usage_after_bytes": proxy_usage_after_bytes,
            "billed_bytes": billed_bytes,
            "billed_gb": round(billed_gb, 6),
            "billed_mb_per_successful_page": round(billed_bytes / successful_pages / 1_000_000, 4) if successful_pages else None,
            "billed_mb_per_unique_asin": round(billed_bytes / unique_asins / 1_000_000, 4) if unique_asins else None,
            "billed_mb_per_successful_asin": round(billed_bytes / successful_asins / 1_000_000, 4) if successful_asins else None,
            "proxy_to_saved_body_ratio": round(billed_bytes / saved_body_bytes, 4) if saved_body_bytes else None,
            "projected_billed_gb_for_target_asins": round(billed_gb / denominator * target_asins, 4) if denominator else None,
        }
        if proxy_charge_cny is not None or proxy_price_cny_per_gb is not None or allocated_fixed_cost_cny:
            if proxy_charge_cny is not None:
                variable_cost = proxy_charge_cny
                cost_basis = "actual_charge_delta"
            else:
                variable_cost = billed_gb * (proxy_price_cny_per_gb or 0.0)
                cost_basis = "estimated_bandwidth_rate"
            observed_cost = variable_cost + allocated_fixed_cost_cny
            cost_per_asin = observed_cost / denominator if denominator else None
            proxy.update({
                "proxy_price_cny_per_gb": proxy_price_cny_per_gb,
                "proxy_charge_cny": proxy_charge_cny,
                "allocated_fixed_cost_cny": allocated_fixed_cost_cny,
                "cost_basis": cost_basis,
                "observed_total_cost_cny": round(observed_cost, 4),
                "cost_cny_per_successful_asin": round(observed_cost / successful_asins, 6) if successful_asins else None,
                "projected_cost_cny_for_target_asins": round(cost_per_asin * target_asins, 2) if cost_per_asin is not None else None,
                "max_asins_with_budget": int(budget_cny / cost_per_asin) if cost_per_asin and cost_per_asin > 0 else None,
            })
            if business_units is not None and business_units > 0:
                proxy["business_unit_definition_required"] = True
                proxy["observed_business_units"] = business_units
                proxy["cost_cny_per_million_business_units"] = round(observed_cost / business_units * 1_000_000, 4)
        result["proxy_bill_measurement"] = proxy

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("state/amazon_us.sqlite3"))
    parser.add_argument("--raw-html-dir", type=Path, required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--all-runs", action="store_true", help="Aggregate all resumed runs in the database")
    parser.add_argument("--target-asins", type=int, default=5800)
    parser.add_argument("--budget-cny", type=float, default=200.0)
    parser.add_argument("--proxy-usage-before-bytes", type=int)
    parser.add_argument("--proxy-usage-after-bytes", type=int)
    parser.add_argument("--proxy-price-cny-per-gb", type=float)
    parser.add_argument("--proxy-charge-cny", type=float, help="Actual charge delta for this isolated batch; preferred over a flat rate estimate")
    parser.add_argument("--allocated-fixed-cost-cny", type=float, default=0.0, help="Monthly fee or minimum-spend share allocated to this batch")
    parser.add_argument("--business-units", type=int, help="Count only after the business definition of one data unit is fixed")
    args = parser.parse_args(argv)
    try:
        collection = build_collection_report(args.db, args.raw_html_dir, args.run_id, args.all_runs)
        report = build_cost_report(
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
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
