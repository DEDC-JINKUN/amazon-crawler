from __future__ import annotations

import csv
import os
from pathlib import Path
import sys
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
DSN = os.environ.get("AMAZON_TEST_POSTGRES_DSN")


def test_usd_price_parser_accepts_common_amazon_values_and_rejects_non_usd():
    from external_metrics import parse_usd_price

    assert str(parse_usd_price("$1,234.56")) == "1234.56"
    assert str(parse_usd_price("US$ 19.99")) == "19.99"
    assert parse_usd_price("HKD155.00") is None
    assert parse_usd_price(None) is None


@pytest.mark.skipif(not DSN, reason="isolated PostgreSQL required")
def test_external_sales_import_is_idempotent_and_applies_core_tiers_without_snapshot_writes(tmp_path):
    import psycopg
    from external_metrics import ExternalMetricBatchConflict, ExternalMetricsStore

    tenant = "metrics-" + uuid.uuid4().hex
    own_core = "B0OWN00001"
    own_normal = "B0OWN00002"
    competitor = "B0COMP0001"
    migration = ROOT / "schema" / "migrations" / "20260915_external_metrics.sql"
    with psycopg.connect(DSN) as conn:
        conn.execute((ROOT / "schema" / "postgres_schema.sql").read_text(encoding="utf-8"))
        conn.execute(migration.read_text(encoding="utf-8"))
        for asin, subject, policy in (
            (own_core, "own", "own_normal"),
            (own_normal, "own", "own_normal"),
            (competitor, "competitor", "competitor_normal"),
        ):
            conn.execute(
                "INSERT INTO amazon_us.asin_master(tenant_id,marketplace,asin,subject_type,source_type,priority,refresh_policy) "
                "VALUES(%s,'US',%s,%s,'fixture',50,%s)",
                (tenant, asin, subject, policy),
            )
        conn.execute(
            "INSERT INTO amazon_us.product_snapshot(tenant_id,marketplace,asin,subject_type,price,status) "
            "VALUES(%s,'US',%s,'own','$25.00','succeeded'),(%s,'US',%s,'own','$50.00','succeeded')",
            (tenant, own_core, tenant, own_normal),
        )

    source = tmp_path / "sales.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["asin", "sales_units", "observed_at", "competitor_asins"])
        writer.writeheader()
        writer.writerow({"asin": own_core, "sales_units": "100", "observed_at": "2026-09-15T00:00:00+00:00", "competitor_asins": competitor})
        writer.writerow({"asin": own_normal, "sales_units": "10", "observed_at": "2026-09-15T00:00:00+00:00", "competitor_asins": ""})

    store = ExternalMetricsStore(DSN, tenant_id=tenant)
    try:
        first = store.import_csv(source, batch_id="sales-20260915", source="mcp-sales")
        duplicate = store.import_csv(source, batch_id="sales-20260915", source="mcp-sales")
        applied = store.apply_core_tiers(revenue_threshold="2000")

        assert first["inserted_metrics"] == 2 and first["inserted_relationships"] == 1
        assert duplicate["duplicate_batch"] is True and duplicate["inserted_metrics"] == 0
        assert applied == {"core": 1, "normal": 1, "skipped": 0, "competitors_updated": 1}

        with psycopg.connect(DSN) as conn:
            policies = dict(conn.execute(
                "SELECT asin,refresh_policy FROM amazon_us.asin_master WHERE tenant_id=%s",
                (tenant,),
            ).fetchall())
            assert policies == {own_core: "own_core", own_normal: "own_normal", competitor: "competitor_core"}
            assert conn.execute("SELECT count(*) FROM amazon_us.external_metrics WHERE tenant_id=%s", (tenant,)).fetchone()[0] == 2
            assert conn.execute("SELECT count(*) FROM amazon_us.product_snapshot WHERE tenant_id=%s", (tenant,)).fetchone()[0] == 2

        changed = source.read_text(encoding="utf-8-sig").replace(",100,", ",101,")
        source.write_text(changed, encoding="utf-8-sig")
        with pytest.raises(ExternalMetricBatchConflict):
            store.import_csv(source, batch_id="sales-20260915", source="mcp-sales")
    finally:
        with psycopg.connect(DSN) as conn:
            conn.execute("DELETE FROM amazon_us.external_metric_relationship WHERE tenant_id=%s", (tenant,))
            conn.execute("DELETE FROM amazon_us.external_metrics WHERE tenant_id=%s", (tenant,))
            conn.execute("DELETE FROM amazon_us.external_metric_batch WHERE tenant_id=%s", (tenant,))
            conn.execute("DELETE FROM amazon_us.product_snapshot WHERE tenant_id=%s", (tenant,))
            conn.execute("DELETE FROM amazon_us.asin_master WHERE tenant_id=%s", (tenant,))
