#!/usr/bin/env python3
"""Audit raw HTML evidence integrity without modifying SQLite or files."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def audit(db_path: Path, raw_html_dir: Path | None = None) -> dict[str, Any]:
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    missing: list[str] = []
    hash_mismatch: list[str] = []
    context_missing = 0
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT asin,raw_html_path,content_hash,context_json FROM collection_evidence ORDER BY id").fetchall()
    except sqlite3.OperationalError as exc:
        if "context_json" not in str(exc):
            conn.close()
            raise
        rows = [dict(row) | {"context_json": None} for row in conn.execute("SELECT asin,raw_html_path,content_hash FROM collection_evidence ORDER BY id").fetchall()]
    try:
        for row in rows:
            context = row.get("context_json") if isinstance(row, dict) else row["context_json"]
            if not context or context == "{}":
                context_missing += 1
            relative = row.get("raw_html_path") if isinstance(row, dict) else row["raw_html_path"]
            if not raw_html_dir or not relative:
                continue
            path = raw_html_dir / str(relative)
            if not path.exists():
                missing.append(str(relative))
                continue
            expected = row.get("content_hash") if isinstance(row, dict) else row["content_hash"]
            if expected:
                # Worker hashes the UTF-8 text body; normalize through the
                # same text path so Windows newline translation is not a
                # false mismatch.
                actual = hashlib.sha256(path.read_text(encoding="utf-8", errors="replace").encode("utf-8")).hexdigest()
                if actual != expected:
                    hash_mismatch.append(str(relative))
        return {
            "schema_version": "amazon-us-evidence-health-v1",
            "evidence_count": len(rows),
            "raw_html_checked": bool(raw_html_dir),
            "raw_html_missing_count": len(missing),
            "hash_mismatch_count": len(hash_mismatch),
            "context_missing_count": context_missing,
            "missing_examples": missing[:20],
            "hash_mismatch_examples": hash_mismatch[:20],
            "ok": not missing and not hash_mismatch,
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("state/amazon_us.sqlite3"))
    parser.add_argument("--raw-html-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        result = audit(args.db, args.raw_html_dir)
    except (OSError, sqlite3.Error) as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
