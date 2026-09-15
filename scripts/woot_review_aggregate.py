#!/usr/bin/env python3
"""Aggregate immutable Woot batch outputs without modifying the source workbook."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from xlsx_asin_reader import read_asins


SUMMARY_SCHEMA = "woot-review-collection-summary-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_new_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _write_new_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")


def _load_progress(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid progress JSONL: {path}:{number}") from exc
            if not isinstance(row, dict) or not row.get("asin") or not row.get("status"):
                raise ValueError(f"invalid progress row: {path}:{number}")
            row["_progress_path"] = str(path)
            rows.append(row)
    return rows


def _time_key(value: Any) -> float:
    text = str(value or "1970-01-01T00:00:00Z").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _resolve_run_dir(value: Any, results_root: Path, project_root: Path) -> Path:
    candidate = Path(str(value or ""))
    if not candidate.is_absolute():
        candidate = project_root / candidate
    candidate = candidate.resolve()
    if not candidate.is_relative_to(results_root):
        raise ValueError(f"run_dir outside result root: {candidate}")
    if not candidate.is_dir():
        raise ValueError(f"run_dir missing: {candidate}")
    return candidate


def _latest_by_asin(rows: list[dict[str, Any]], status: str) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("status") != status:
            continue
        asin = str(row["asin"]).strip().upper()
        if asin not in selected or _time_key(row.get("completed_at")) >= _time_key(selected[asin].get("completed_at")):
            selected[asin] = row
    return selected


def aggregate_collection(
    workbook_path: str | Path,
    results_root: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    workbook = read_asins(workbook_path)
    root = Path(results_root).resolve()
    if not root.is_dir():
        raise ValueError(f"results_root is not a directory: {root}")
    project_root = Path.cwd().resolve()
    all_rows: list[dict[str, Any]] = []
    progress_files = sorted(root.rglob("progress.jsonl"))
    for progress_path in progress_files:
        all_rows.extend(_load_progress(progress_path))
    successes = _latest_by_asin(all_rows, "success")
    failures = _latest_by_asin(all_rows, "failed")
    workbook_set = set(workbook.asins)
    unexpected = (set(successes) | set(failures)) - workbook_set
    if unexpected:
        raise ValueError(f"result ASINs not present in workbook: {sorted(unexpected)[:5]}")

    review_rows: list[dict[str, Any]] = []
    status_rows: list[dict[str, Any]] = []
    coverage_counts: dict[str, int] = {}
    success_details: dict[str, dict[str, Any]] = {}
    for asin in workbook.asins:
        success = successes.get(asin)
        if success is None:
            failure = failures.get(asin)
            status_rows.append({
                "asin": asin,
                "status": "failed" if failure else "unattempted",
                "coverage_status": "blocked" if failure else None,
                "error_code": failure.get("error_code") if failure else None,
                "http_status": failure.get("http_status") if failure else None,
                "fetched_unique_count": 0,
            })
            continue
        run_dir = _resolve_run_dir(success.get("run_dir"), root, project_root)
        receipt_path = run_dir / "receipt.json"
        normalized_path = run_dir / "reviews.normalized.json"
        if not receipt_path.is_file() or not normalized_path.is_file():
            raise ValueError(f"successful run is missing receipt/output: {asin}")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        expected_hash = str(receipt.get("normalized_output_sha256") or "")
        actual_hash = _sha256(normalized_path)
        if not expected_hash or expected_hash != actual_hash:
            raise ValueError(f"normalized output hash mismatch: {asin}")
        normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
        if normalized.get("asin") != asin or not isinstance(normalized.get("reviews"), list):
            raise ValueError(f"normalized output identity/schema mismatch: {asin}")
        reviews = normalized["reviews"]
        ids = [str(item.get("review_id") or "") for item in reviews]
        if any(not value for value in ids) or len(ids) != len(set(ids)):
            raise ValueError(f"duplicate or blank review_id in normalized output: {asin}")
        coverage = normalized.get("coverage") or receipt.get("coverage") or {}
        coverage_status = str(coverage.get("status") or "unknown")
        coverage_counts[coverage_status] = coverage_counts.get(coverage_status, 0) + 1
        for item in reviews:
            if item.get("asin") != asin:
                raise ValueError(f"review ASIN mismatch: {asin}")
            review_rows.append(dict(item) | {
                "source_run_id": receipt.get("run_id"),
                "source_output_sha256": actual_hash,
                "coverage_status": coverage_status,
            })
        success_details[asin] = {
            "run_id": receipt.get("run_id"),
            "run_dir": str(run_dir),
            "normalized_output_sha256": actual_hash,
            "coverage_status": coverage_status,
            "fetched_unique_count": len(reviews),
        }
        status_rows.append({
            "asin": asin,
            "status": "success",
            "coverage_status": coverage_status,
            "error_code": None,
            "http_status": None,
            "fetched_unique_count": len(reviews),
        })

    pending = [item["asin"] for item in status_rows if item["status"] != "success"]
    failed = [item for item in status_rows if item["status"] == "failed"]
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=False)
    _write_new_jsonl(target / "reviews.jsonl", review_rows)
    _write_new_jsonl(target / "asin_status.jsonl", status_rows)
    _write_new_json(target / "pending_asins.json", {
        "source_filename": workbook.source_filename,
        "source_sha256": workbook.source_sha256,
        "asins": pending,
        "count": len(pending),
    })
    summary = {
        "schema_version": SUMMARY_SCHEMA,
        "source_filename": workbook.source_filename,
        "source_sha256": workbook.source_sha256,
        "source_sheet": workbook.sheet,
        "source_column": workbook.column,
        "source_unique_asins": len(workbook.asins),
        "successful_asins": len(success_details),
        "failed_asins": len(failed),
        "unattempted_asins": len(pending) - len(failed),
        "pending_asins": len(pending),
        "unique_reviews": len(review_rows),
        "coverage_status_counts": coverage_counts,
        "claim_100_percent_asins": 0,
        "progress_files": len(progress_files),
        "database_writes_performed": 0,
        "pdp_truth_written": False,
    }
    _write_new_json(target / "collection_summary.json", summary)
    files = []
    for name in ("asin_status.jsonl", "collection_summary.json", "pending_asins.json", "reviews.jsonl"):
        path = target / name
        files.append({"path": name, "bytes": path.stat().st_size, "sha256": _sha256(path)})
    _write_new_json(target / "manifest.json", {
        "schema_version": "woot-review-aggregate-manifest-v1",
        "files": files,
    })
    return summary | {"output_dir": str(target.resolve())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate Woot review batch outputs")
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--results-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = aggregate_collection(args.workbook, args.results_root, args.output_dir)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"woot review aggregation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
