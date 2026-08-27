#!/usr/bin/env python3
"""Verify PostgreSQL repository reads after a SQLite replay."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from collection_storage import PostgresCollectionRepository
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from collection_storage import PostgresCollectionRepository


def schema_ok(contract: dict[str, list[str]]) -> bool:
    return contract.get("item_state") == ["next_retry_at"] and contract.get("collection_evidence") == ["context_json", "transfer_bytes"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--asin")
    args = parser.parse_args(argv)
    try:
        repository = PostgresCollectionRepository(args.dsn)
        contract = repository.load_schema_contract()
        contract_ok = schema_ok(contract)
        result = {"schema_version": "amazon-us-postgres-verification-v1", "schema_ok": contract_ok, "schema_contract": contract}
        if contract_ok:
            result["job_status"] = repository.load_job_status()
        else:
            result["job_status"] = None
        if args.asin and contract_ok:
            result["product"] = repository.load_product("US", args.asin.upper())
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["schema_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
