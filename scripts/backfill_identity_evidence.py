#!/usr/bin/env python3
"""Idempotently enrich legacy asin_mismatch evidence with strict identity metadata."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]


def _load_product_parser():
    path = ROOT / "scripts" / "amazon_us_worker.py"
    spec = importlib.util.spec_from_file_location("identity_backfill_worker", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.parse_product_html, module._canonical_asin


def _resolve_raw_path(value: str) -> Path | None:
    candidate = Path(value)
    candidates = [candidate] if candidate.is_absolute() else [ROOT / candidate, ROOT / "data" / candidate]
    for path in candidates:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and resolved.is_relative_to(ROOT):
            return resolved
    return None


def backfill_identity_evidence(
    connect: Callable[[], Any],
    *,
    parser: Callable[[str, str], dict[str, Any]] | None = None,
    canonical_asin: Callable[[Any], str] | None = None,
) -> dict[str, int]:
    if parser is None or canonical_asin is None:
        parser, canonical_asin = _load_product_parser()
    scanned = updated = missing_raw = parse_failed = 0
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id,asin,url,raw_html_path
            FROM amazon_us.collection_evidence
            WHERE error_code='asin_mismatch' AND NOT (context_json ? 'identity')
            ORDER BY id
            """
        )
        rows = [dict(row) for row in cursor.fetchall()]
        for row in rows:
            scanned += 1
            raw_path = _resolve_raw_path(str(row.get("raw_html_path") or ""))
            if raw_path is None:
                missing_raw += 1
                continue
            try:
                data = parser(raw_path.read_text(encoding="utf-8", errors="replace"), str(row.get("url") or ""))
                identity = {
                    "requested_asin": str(row["asin"]).upper(),
                    "observed_asin": str(data.get("asin") or "").upper(),
                    "canonical_asin": str(canonical_asin(data.get("canonical_url")) or "").upper(),
                    "parent_asin": str(data.get("parent_asin") or "").upper(),
                    "child_asins": sorted({str(value).upper() for value in data.get("identity_child_asins") or [] if value}),
                }
            except (OSError, TypeError, ValueError):
                parse_failed += 1
                continue
            cursor.execute(
                """
                UPDATE amazon_us.collection_evidence
                SET context_json=jsonb_set(context_json,'{identity}',%s::jsonb,true)
                WHERE id=%s AND error_code='asin_mismatch' AND NOT (context_json ? 'identity')
                """,
                (json.dumps(identity, ensure_ascii=False), row["id"]),
            )
            updated += 1
        connection.commit()
    return {"scanned": scanned, "updated": updated, "missing_raw": missing_raw, "parse_failed": parse_failed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    args = parser.parse_args(argv)
    try:
        dsn = os.environ.get(args.dsn_env, "").strip()
        if not dsn:
            raise ValueError(f"PostgreSQL DSN environment variable is required: {args.dsn_env}")
        import psycopg
        from psycopg.rows import dict_row
        result = backfill_identity_evidence(lambda: psycopg.connect(dsn, row_factory=dict_row))
        print(json.dumps(result, ensure_ascii=False))
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        print(f"identity evidence backfill failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
