#!/usr/bin/env python3
"""Import source-separated sales metrics and apply revenue-based refresh tiers."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import os
from pathlib import Path
import re
from typing import Any, Callable


ASIN_RE = re.compile(r"[A-Z0-9]{10}")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,160}")


class ExternalMetricBatchConflict(ValueError):
    pass


def parse_usd_price(value: Any) -> Decimal | None:
    text = str(value or "").strip()
    if not text or not re.search(r"(?:US\$|\$)", text, re.IGNORECASE):
        return None
    match = re.search(r"(?:US\$|\$)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text, re.IGNORECASE)
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def _observed_at(value: str) -> str:
    observed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return observed.isoformat()


def _sales_units(value: str) -> Decimal:
    try:
        parsed = Decimal(str(value or "").strip())
    except InvalidOperation as exc:
        raise ValueError("sales_units must be numeric") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("sales_units must be a non-negative finite number")
    return parsed


class ExternalMetricsStore:
    def __init__(
        self, dsn: str, *, tenant_id: str,
        connect: Callable[[], Any] | None = None,
    ) -> None:
        if not str(dsn or "").strip() or not str(tenant_id or "").strip():
            raise ValueError("dsn and tenant_id are required")
        self.dsn = dsn
        self.tenant_id = tenant_id
        if connect is None:
            import psycopg
            from psycopg.rows import dict_row
            connect = lambda: psycopg.connect(self.dsn, row_factory=dict_row)
        self._connect = connect

    def import_csv(self, path: Path, *, batch_id: str, source: str) -> dict[str, Any]:
        path = Path(path)
        if not SAFE_ID_RE.fullmatch(str(batch_id or "")) or not SAFE_ID_RE.fullmatch(str(source or "")):
            raise ValueError("invalid external metric batch or source")
        raw = path.read_bytes()
        content_hash = hashlib.sha256(raw).hexdigest()
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        required = {"asin", "sales_units", "observed_at"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("external metric CSV requires asin,sales_units,observed_at")
        metrics: list[tuple[str, Decimal, str]] = []
        relationships: set[tuple[str, str]] = set()
        seen: set[str] = set()
        for row in reader:
            asin = str(row.get("asin") or "").strip().upper()
            if not ASIN_RE.fullmatch(asin) or asin in seen:
                raise ValueError("external metric CSV has invalid or duplicate ASIN")
            seen.add(asin)
            metrics.append((asin, _sales_units(row.get("sales_units", "")), _observed_at(row.get("observed_at", ""))))
            for competitor in re.split(r"[;,\s]+", str(row.get("competitor_asins") or "").strip().upper()):
                if not competitor:
                    continue
                if not ASIN_RE.fullmatch(competitor) or competitor == asin:
                    raise ValueError("external metric CSV has invalid competitor ASIN")
                relationships.add((asin, competitor))
        if not metrics:
            raise ValueError("external metric CSV is empty")

        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT source,content_hash,row_count FROM amazon_us.external_metric_batch "
                    "WHERE tenant_id=%s AND batch_id=%s FOR UPDATE",
                    (self.tenant_id, batch_id),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    value = dict(existing)
                    if value["source"] != source or value["content_hash"] != content_hash or int(value["row_count"]) != len(metrics):
                        raise ExternalMetricBatchConflict("external metric batch identity conflict")
                    conn.commit()
                    return {
                        "batch_id": batch_id, "source": source, "duplicate_batch": True,
                        "inserted_metrics": 0, "inserted_relationships": 0,
                    }
                cursor.execute(
                    "INSERT INTO amazon_us.external_metric_batch(tenant_id,batch_id,source,content_hash,row_count) "
                    "VALUES(%s,%s,%s,%s,%s)",
                    (self.tenant_id, batch_id, source, content_hash, len(metrics)),
                )
                cursor.executemany(
                    "INSERT INTO amazon_us.external_metrics(tenant_id,marketplace,asin,sales_units,observed_at,source,batch_id) "
                    "VALUES(%s,'US',%s,%s,%s,%s,%s)",
                    [(self.tenant_id, asin, units, observed, source, batch_id) for asin, units, observed in metrics],
                )
                if relationships:
                    cursor.executemany(
                        "INSERT INTO amazon_us.external_metric_relationship(tenant_id,marketplace,own_asin,competitor_asin,source,batch_id) "
                        "VALUES(%s,'US',%s,%s,%s,%s) ON CONFLICT(tenant_id,own_asin,competitor_asin) DO UPDATE SET "
                        "source=EXCLUDED.source,batch_id=EXCLUDED.batch_id,updated_at=CURRENT_TIMESTAMP",
                        [(self.tenant_id, own, competitor, source, batch_id) for own, competitor in sorted(relationships)],
                    )
            conn.commit()
        return {
            "batch_id": batch_id, "source": source, "duplicate_batch": False,
            "inserted_metrics": len(metrics), "inserted_relationships": len(relationships),
        }

    def apply_core_tiers(self, *, revenue_threshold: str | Decimal = "2000") -> dict[str, int]:
        try:
            threshold = Decimal(str(revenue_threshold))
        except InvalidOperation as exc:
            raise ValueError("invalid revenue threshold") from exc
        if not threshold.is_finite() or threshold <= 0:
            raise ValueError("revenue threshold must be positive")
        counts = {"core": 0, "normal": 0, "skipped": 0, "competitors_updated": 0}
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """SELECT master.asin,metric.sales_units,latest.price
                    FROM amazon_us.asin_master master
                    LEFT JOIN LATERAL (
                      SELECT sales_units FROM amazon_us.external_metrics value
                      WHERE value.tenant_id=master.tenant_id AND value.marketplace=master.marketplace AND value.asin=master.asin
                      ORDER BY value.observed_at DESC,value.imported_at DESC LIMIT 1
                    ) metric ON true
                    LEFT JOIN LATERAL (
                      SELECT price FROM amazon_us.product_snapshot snapshot
                      WHERE snapshot.tenant_id=master.tenant_id AND snapshot.marketplace=master.marketplace
                        AND snapshot.asin=master.asin AND snapshot.subject_type=master.subject_type
                      ORDER BY snapshot.collected_at DESC,snapshot.snapshot_id DESC LIMIT 1
                    ) latest ON true
                    WHERE master.tenant_id=%s AND master.marketplace='US' AND master.subject_type='own'
                      AND master.active_status='active' ORDER BY master.asin""",
                    (self.tenant_id,),
                )
                for raw in cursor.fetchall():
                    row = dict(raw)
                    price = parse_usd_price(row.get("price"))
                    if row.get("sales_units") is None or price is None:
                        counts["skipped"] += 1
                        continue
                    core = Decimal(str(row["sales_units"])) * price >= threshold
                    policy, priority = ("own_core", 100) if core else ("own_normal", 50)
                    cursor.execute(
                        "UPDATE amazon_us.asin_master SET refresh_policy=%s,priority=%s,updated_at=CURRENT_TIMESTAMP "
                        "WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type='own'",
                        (policy, priority, self.tenant_id, row["asin"]),
                    )
                    counts["core" if core else "normal"] += 1
                cursor.execute(
                    """UPDATE amazon_us.asin_master competitor SET
                      refresh_policy=CASE WHEN owner.refresh_policy='own_core' THEN 'competitor_core' ELSE 'competitor_normal' END,
                      priority=CASE WHEN owner.refresh_policy='own_core' THEN 100 ELSE 50 END,
                      updated_at=CURRENT_TIMESTAMP
                    FROM amazon_us.external_metric_relationship relation
                    JOIN amazon_us.asin_master owner ON owner.tenant_id=relation.tenant_id
                      AND owner.marketplace=relation.marketplace AND owner.asin=relation.own_asin AND owner.subject_type='own'
                    WHERE competitor.tenant_id=relation.tenant_id AND competitor.marketplace=relation.marketplace
                      AND competitor.asin=relation.competitor_asin AND competitor.subject_type='competitor'
                      AND competitor.tenant_id=%s""",
                    (self.tenant_id,),
                )
                counts["competitors_updated"] = max(0, int(cursor.rowcount))
            conn.commit()
        return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--tenant-id", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    import_parser = subparsers.add_parser("import-csv")
    import_parser.add_argument("--file", type=Path, required=True)
    import_parser.add_argument("--batch-id", required=True)
    import_parser.add_argument("--source", required=True)
    apply_parser = subparsers.add_parser("apply-tiers")
    apply_parser.add_argument("--revenue-threshold", default="2000")
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        print(json.dumps({"ok": False, "error": "database_credentials_missing"}))
        return 2
    try:
        store = ExternalMetricsStore(dsn, tenant_id=args.tenant_id)
        if args.command == "import-csv":
            result = store.import_csv(args.file, batch_id=args.batch_id, source=args.source)
        else:
            result = store.apply_core_tiers(revenue_threshold=args.revenue_threshold)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}))
        return 1
    print(json.dumps({"ok": True, **result}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
