#!/usr/bin/env python3
"""Idempotently enrich legacy asin_mismatch evidence with strict identity metadata."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

try:
    from raw_html_store import read_raw_html
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from raw_html_store import read_raw_html


ROOT = Path(__file__).resolve().parents[1]


def _load_product_parser():
    path = ROOT / "scripts" / "amazon_us_worker.py"
    spec = importlib.util.spec_from_file_location("identity_backfill_worker", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.parse_product_html, module._canonical_asin, module._valid_amazon_canonical


def _resolve_raw_path(value: str, raw_html_dir: Path) -> Path | None:
    root = raw_html_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("raw_html_dir must be a directory")
    candidate = Path(value)
    path = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def backfill_identity_evidence(
    connect: Callable[[], Any],
    *,
    tenant_id: str,
    raw_html_dir: Path,
    parser: Callable[[str, str], dict[str, Any]] | None = None,
    canonical_asin: Callable[[Any], str] | None = None,
    canonical_valid: Callable[[Any], bool] | None = None,
) -> dict[str, int]:
    tenant_id = str(tenant_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", tenant_id):
        raise ValueError("invalid tenant_id")
    raw_html_dir = raw_html_dir.resolve(strict=True)
    if not raw_html_dir.is_dir():
        raise ValueError("raw_html_dir must be a directory")
    if parser is None or canonical_asin is None or canonical_valid is None:
        parser, canonical_asin, canonical_valid = _load_product_parser()
    scanned = updated = missing_raw = hash_mismatch = parse_failed = 0
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id,tenant_id,asin,url,raw_html_path,content_hash
            FROM amazon_us.collection_evidence
            WHERE tenant_id=%s AND error_code='asin_mismatch'
              AND (NOT (context_json ? 'identity')
                   OR NOT (context_json->'identity' ? 'canonical_valid_amazon'))
            ORDER BY id
            """,
            (tenant_id,),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        for row in rows:
            scanned += 1
            raw_path = _resolve_raw_path(str(row.get("raw_html_path") or ""), raw_html_dir)
            if raw_path is None:
                missing_raw += 1
                continue
            try:
                raw_body = read_raw_html(raw_path)
                if hashlib.sha256(raw_body.encode("utf-8")).hexdigest() != str(row.get("content_hash") or ""):
                    hash_mismatch += 1
                    continue
                data = parser(raw_body, str(row.get("url") or ""))
                identity = {
                    "requested_asin": str(row["asin"]).upper(),
                    "observed_asin": str(data.get("asin") or "").upper(),
                    "canonical_asin": str(canonical_asin(data.get("canonical_url")) or "").upper(),
                    "canonical_valid_amazon": bool(canonical_valid(data.get("canonical_url"))),
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
                WHERE tenant_id=%s AND id=%s AND error_code='asin_mismatch'
                  AND (NOT (context_json ? 'identity')
                       OR NOT (context_json->'identity' ? 'canonical_valid_amazon'))
                """,
                (json.dumps(identity, ensure_ascii=False), tenant_id, row["id"]),
            )
            updated += 1
        connection.commit()
    return {
        "scanned": scanned, "updated": updated, "missing_raw": missing_raw,
        "hash_mismatch": hash_mismatch, "parse_failed": parse_failed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--raw-html-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        dsn = os.environ.get(args.dsn_env, "").strip()
        if not dsn:
            raise ValueError(f"PostgreSQL DSN environment variable is required: {args.dsn_env}")
        import psycopg
        from psycopg.rows import dict_row
        result = backfill_identity_evidence(
            lambda: psycopg.connect(dsn, row_factory=dict_row),
            tenant_id=args.tenant_id,
            raw_html_dir=args.raw_html_dir,
        )
        print(json.dumps(result, ensure_ascii=False))
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        print(f"identity evidence backfill failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
