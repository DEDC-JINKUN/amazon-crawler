from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "traffic_cost_report.py"


def load_module():
    spec = importlib.util.spec_from_file_location("traffic_cost_report_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_build_cost_report_uses_proxy_dashboard_delta() -> None:
    report = load_module().build_cost_report(
        {"run_id": "run-1", "success_page_count": 90, "unique_asin_count": 100, "unique_successful_asin_count": 90, "bytes_total": 150_000_000},
        target_asins=5800,
        budget_cny=200,
        proxy_usage_before_bytes=10_000_000_000,
        proxy_usage_after_bytes=10_200_000_000,
        proxy_price_cny_per_gb=5.0,
        business_units=20_000,
    )

    proxy = report["proxy_bill_measurement"]
    assert proxy["billed_bytes"] == 200_000_000
    assert proxy["billed_gb"] == 0.2
    assert proxy["observed_total_cost_cny"] == 1.0
    assert proxy["projected_cost_cny_for_target_asins"] == 64.44
    assert proxy["max_asins_with_budget"] == 18_000
    assert proxy["cost_cny_per_million_business_units"] == 50.0
    assert proxy["proxy_to_saved_body_ratio"] == pytest.approx(1.3333)


def test_build_cost_report_requires_complete_usage_pair() -> None:
    module = load_module()
    with pytest.raises(ValueError, match="before and after"):
        module.build_cost_report(
            {"success_page_count": 1, "unique_asin_count": 1, "bytes_total": 100},
            proxy_usage_before_bytes=1,
        )


def test_local_body_projection_is_not_labeled_as_proxy_bill() -> None:
    report = load_module().build_cost_report(
        {"run_id": "run-2", "success_page_count": 10, "unique_asin_count": 10, "bytes_total": 10 * 1024 * 1024},
        target_asins=5800,
    )
    assert report["local_body_estimate"]["mib_per_unique_asin"] == 1.0
    assert report["local_body_estimate"]["is_proxy_bill"] is False
    assert report["proxy_bill_measurement"] is None


def test_actual_charge_and_fixed_cost_override_flat_rate_estimate() -> None:
    report = load_module().build_cost_report(
        {"success_page_count": 10, "unique_asin_count": 10, "unique_successful_asin_count": 10, "bytes_total": 1000},
        proxy_usage_before_bytes=1000,
        proxy_usage_after_bytes=1_000_001_000,
        proxy_price_cny_per_gb=1.0,
        proxy_charge_cny=8.0,
        allocated_fixed_cost_cny=2.0,
    )
    proxy = report["proxy_bill_measurement"]
    assert proxy["cost_basis"] == "actual_charge_delta"
    assert proxy["observed_total_cost_cny"] == 10.0
    assert proxy["cost_cny_per_successful_asin"] == 1.0


def test_explicit_zero_successful_asins_is_not_replaced_by_page_count() -> None:
    report = load_module().build_cost_report(
        {"success_page_count": 5, "unique_asin_count": 5, "unique_successful_asin_count": 0, "bytes_total": 1000},
        proxy_usage_before_bytes=0,
        proxy_usage_after_bytes=1000,
        proxy_charge_cny=1.0,
    )
    assert report["measurement_scope"]["unique_successful_asins"] == 0
    assert report["proxy_bill_measurement"]["cost_cny_per_successful_asin"] is None
    assert report["proxy_bill_measurement"]["projected_cost_cny_for_target_asins"] is None
