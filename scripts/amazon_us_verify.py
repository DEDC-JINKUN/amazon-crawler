#!/usr/bin/env python3
"""Read-only verification for Amazon US SQLite state and materialized outputs."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "amazon_us_asin_manifest.csv"
DEFAULT_STATE = ROOT / "state" / "amazon_us.sqlite3"
DEFAULT_OUTPUT = ROOT / "data" / "amazon_us"
DEFAULT_VERIFICATION = DEFAULT_OUTPUT / "verification" / "latest_verification.json"
STATUSES = {"pending", "running", "product_done", "reviews_pending", "succeeded", "blocked", "failed"}
ALLOWED_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"pending"},
    "pending": {"pending", "running", "failed"},
    "running": {"pending", "running", "product_done", "reviews_pending", "succeeded", "blocked", "failed"},
    "product_done": {"product_done", "reviews_pending", "succeeded", "failed"},
    "reviews_pending": {"reviews_pending", "running", "succeeded", "blocked", "failed"},
    "succeeded": {"succeeded", "running"},
    "blocked": {"blocked"},
    "failed": {"failed", "running"},
}
HEADERS = {
    "product_snapshot.csv": [
        "asin", "marketplace", "canonical_url", "availability", "title", "brand", "rating",
        "reported_rating_count", "reported_review_count", "review_count", "review_count_source",
        "price", "bullets_json", "product_description", "specs_json", "buy_box_json",
        "top_reviews_json", "review_link", "review_section_anchor", "aplus_present", "collected_at", "status",
    ],
    "media_asset.csv": [
        "asin", "marketplace", "placement", "entry_type", "thumbnail_url", "display_url", "asset_url",
        "poster_url", "ordinal", "is_primary", "width", "height", "alt_text", "variant_asin",
        "load_status", "failure_reason", "unique_key",
    ],
    "content_module.csv": ["asin", "marketplace", "module_type", "position", "order_index", "text", "image_url", "link_url", "status", "unique_key"],
    "review_summary.csv": [
        "asin", "marketplace", "reported_rating_count", "reported_review_count", "reported_count_source",
        "fetched_count", "pages_fetched", "next_page", "status", "updated_at",
    ],
    "review_record.csv": [
        "asin", "marketplace", "review_id", "rating", "title", "body", "review_url", "review_date",
        "locale", "verified", "body_truncated", "review_images_json", "page", "unique_key",
    ],
    "collection_evidence.csv": [
        "run_id", "asin", "marketplace", "url", "http_status", "transfer_bytes", "retrieved_at", "source_type",
        "content_hash", "raw_html_path", "block_reason", "parser_version", "error_code", "context_json",
    ],
}


def _read_manifest(path: Path, errors: list[str]) -> set[tuple[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return {(row.get("marketplace", ""), row.get("asin", "")) for row in csv.DictReader(handle)}
    except (OSError, UnicodeError, csv.Error) as exc:
        errors.append(f"manifest 无法读取: {exc}")
        return set()


def _read_csv(path: Path, errors: list[str]) -> list[dict[str, str]]:
    if not path.exists():
        errors.append(f"缺少输出: {path.name}")
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            expected = HEADERS[path.name]
            if reader.fieldnames != expected:
                errors.append(f"{path.name} headers 不匹配")
            return list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        errors.append(f"{path.name} 无法读取: {exc}")
        return []


def _duplicates(rows: list[dict[str, str]], key_fn) -> list[str]:
    seen: set[Any] = set()
    duplicate: list[str] = []
    for row in rows:
        key = key_fn(row)
        if key in seen:
            duplicate.append(str(key))
        seen.add(key)
    return duplicate


def _verify_history(conn: sqlite3.Connection, errors: list[str]) -> None:
    try:
        rows = list(conn.execute("SELECT * FROM state_history ORDER BY id"))
    except sqlite3.Error as exc:
        errors.append(f"state_history 不可读取: {exc}")
        return
    last: dict[tuple[str, str], str | None] = {}
    for row in rows:
        key = (row["marketplace"], row["asin"])
        expected_from = last.get(key)
        if row["from_status"] != expected_from:
            errors.append(f"state/history 不一致: {row['asin']} history from={row['from_status']} expected={expected_from}")
        transition_allowed = row["to_status"] in ALLOWED_TRANSITIONS.get(row["from_status"], set())
        migration_allowed = (
            (
                row["from_status"] == "reviews_pending"
                and row["to_status"] == "pending"
                and row["reason"] in {"fragment_cursor_migration", "parser_regression_requeue"}
            )
            or (
                row["from_status"] == "succeeded"
                and row["to_status"] == "reviews_pending"
                and row["reason"] == "review_parser_regression_requeue"
            )
        )
        if not transition_allowed and not migration_allowed:
            errors.append(f"非法状态转移: {row['asin']} {row['from_status']}->{row['to_status']}")
        last[key] = row["to_status"]
    for key, status in last.items():
        current = conn.execute("SELECT status FROM item_state WHERE marketplace=? AND asin=?", key).fetchone()
        if current is None or current[0] != status:
            errors.append(f"state/history 最终状态不一致: {key[1]} {status}")


def _verify_pages(conn: sqlite3.Connection, item_rows: list[sqlite3.Row], errors: list[str]) -> None:
    for item in item_rows:
        pages = list(conn.execute("SELECT * FROM review_page_state WHERE marketplace=? AND asin=? ORDER BY page", (item["marketplace"], item["asin"])))
        expected_page = 1
        for page in pages:
            if page["page"] != expected_page:
                errors.append(f"分页不连续: {item['asin']} expected={expected_page} actual={page['page']}")
            page_status_allowed = page["status"] in {"fetched", "deferred"}
            terminal_page_allowed = (
                page["status"] == "blocked" and item["status"] == "blocked"
            ) or (
                page["status"] == "failed" and item["status"] == "failed"
            )
            if not page_status_allowed and not terminal_page_allowed:
                errors.append(f"页面状态异常: {item['asin']} page={page['page']} status={page['status']} item={item['status']}")
            if page["next_url"] and page["page"] < (pages[-1]["page"] if pages else 0):
                next_row = next((candidate for candidate in pages if candidate["page"] == page["page"] + 1), None)
                if next_row and next_row["url"] != page["next_url"]:
                    errors.append(f"分页 URL 断裂: {item['asin']} page={page['page']}")
            expected_page += 1
        if item["status"] == "succeeded" and item["next_review_url"]:
            errors.append(f"succeeded 仍有 next_review_url: {item['asin']}")
        if item["status"] == "succeeded" and pages and pages[-1]["next_url"]:
            errors.append(f"succeeded 末页仍有下一页: {item['asin']}")
        if item["status"] == "reviews_pending" and not item["next_review_url"]:
            errors.append(f"reviews_pending 缺少 next_review_url: {item['asin']}")


def verify(manifest: Path = DEFAULT_MANIFEST, state: Path = DEFAULT_STATE, output_dir: Path = DEFAULT_OUTPUT, expected_count: int | None = 1892) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    manifest_keys = _read_manifest(manifest, errors) if manifest.exists() else set()
    if not manifest.exists():
        errors.append(f"manifest 不存在: {manifest}")
    if expected_count is not None and len(manifest_keys) != expected_count:
        errors.append(f"manifest 数量为 {len(manifest_keys)}，预期 {expected_count}")
    item_rows: list[sqlite3.Row] = []
    state_counts: dict[str, int] = {}
    if not state.exists():
        errors.append(f"SQLite state 不存在: {state}")
        conn = None
    else:
        conn = sqlite3.connect(state)
        conn.row_factory = sqlite3.Row
        try:
            item_rows = list(conn.execute("SELECT * FROM item_state"))
            _verify_history(conn, errors)
            _verify_pages(conn, item_rows, errors)
        except sqlite3.Error as exc:
            errors.append(f"SQLite state 无法读取: {exc}")
    item_keys = {(row["marketplace"], row["asin"]) for row in item_rows}
    missing = sorted(manifest_keys - item_keys)
    extra = sorted(item_keys - manifest_keys)
    if missing:
        errors.append(f"state 未覆盖 {len(missing)} 个 manifest key")
    if extra:
        errors.append(f"state 多出 {len(extra)} 个 key")
    for row in item_rows:
        status = row["status"]
        state_counts[status] = state_counts.get(status, 0) + 1
        if status not in STATUSES:
            errors.append(f"未知状态: {row['asin']} {status}")
        if status == "running":
            errors.append(f"悬挂 running: {row['asin']}")
        if status == "blocked" and not row["block_reason"]:
            errors.append(f"blocked 缺少原因: {row['asin']}")
        if status == "failed" and row["attempts"] >= row["max_attempts"]:
            pass
        if row["fetched_review_count"] > 0 and row["reported_review_count"] is not None and row["fetched_review_count"] > row["reported_review_count"]:
            errors.append(f"fetched_count 超过 reported_review_count: {row['asin']}")

    csv_rows = {name: _read_csv(output_dir / name, errors) for name in HEADERS}
    key_functions = {
        "product_snapshot.csv": lambda r: (r.get("marketplace"), r.get("asin")),
        "media_asset.csv": lambda r: r.get("unique_key"),
        "content_module.csv": lambda r: r.get("unique_key"),
        "review_summary.csv": lambda r: (r.get("marketplace"), r.get("asin")),
        "review_record.csv": lambda r: r.get("unique_key"),
        "collection_evidence.csv": lambda r: (r.get("run_id"), r.get("marketplace"), r.get("asin"), r.get("url"), r.get("retrieved_at"), r.get("content_hash"), r.get("error_code")),
    }
    duplicates: dict[str, int] = {}
    for name, rows in csv_rows.items():
        duplicate = _duplicates(rows, key_functions[name])
        duplicates[name] = len(duplicate)
        if duplicate:
            errors.append(f"{name} 存在重复唯一键 {len(duplicate)} 项")
    product_by_key = {(r.get("marketplace"), r.get("asin")): r for r in csv_rows["product_snapshot.csv"]}
    summary_by_key = {(r.get("marketplace"), r.get("asin")): r for r in csv_rows["review_summary.csv"]}
    evidence_rows = csv_rows["collection_evidence.csv"]
    for row in evidence_rows:
        if row.get("http_status") in {"403", "429"} and not row.get("block_reason"):
            errors.append(f"HTTP {row['http_status']} evidence 缺少 block_reason: {row.get('asin')}")
    if conn is not None:
        for row in item_rows:
            key = (row["marketplace"], row["asin"])
            summary = summary_by_key.get(key)
            if summary and int(summary.get("fetched_count") or 0) != int(row["fetched_review_count"] or 0):
                errors.append(f"summary 与 state 数量不一致: {row['asin']}")
            if row["status"] in {"product_done", "reviews_pending", "succeeded"} and key not in product_by_key:
                errors.append(f"采集状态缺少 product snapshot: {row['asin']}")
            if row["status"] == "succeeded":
                product = product_by_key.get(key, {})
                if product.get("review_link") and (not summary or summary.get("status") != "exhausted" or summary.get("next_page")):
                    errors.append(f"succeeded 非末页终态: {row['asin']}")
        conn.close()
    exhausted_failed = [row["asin"] for row in item_rows if row["status"] == "failed" and row["attempts"] >= row["max_attempts"]]
    failed_items = [
        {"asin": row["asin"], "status": row["status"], "attempts": row["attempts"], "last_error": row["last_error"], "block_reason": row["block_reason"]}
        for row in item_rows if row["status"] in {"failed", "blocked"}
    ]
    collected_count = sum(state_counts.get(status, 0) for status in ("product_done", "reviews_pending", "succeeded", "blocked", "failed"))
    initialized = bool(item_rows) and state_counts.get("pending", 0) == len(item_rows) and not evidence_rows and not product_by_key
    terminal_count = sum(state_counts.get(status, 0) for status in ("succeeded", "blocked"))
    all_terminal = bool(item_rows) and terminal_count + state_counts.get("failed", 0) == len(item_rows)
    phase = "initialized" if initialized else ("collected" if all_terminal else ("not_collected" if not collected_count else "collecting"))
    result = {
        "verification_version": "amazon-us-verification-v2",
        "verified_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "phase": phase,
        "collection_phase": "not_collected" if initialized else ("complete" if state_counts.get("succeeded", 0) + state_counts.get("blocked", 0) + state_counts.get("failed", 0) == len(item_rows) and item_rows else "collecting"),
        "manifest": {"path": str(manifest), "count": len(manifest_keys)},
        "state": {"path": str(state), "count": len(item_rows), "status_counts": state_counts},
        "outputs": {name: len(rows) for name, rows in csv_rows.items()},
        "duplicate_counts": duplicates,
        "coverage": {"missing_state": len(missing), "extra_state": len(extra)},
        "blocked": [{"asin": row["asin"], "reason": row["block_reason"]} for row in item_rows if row["status"] == "blocked"],
        "failed": failed_items,
        "exhausted_failed": exhausted_failed,
        "errors": errors,
        "ok": not errors,
    }
    return result, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--expected-count", type=int, default=1892)
    parser.add_argument("--verification", type=Path, default=DEFAULT_VERIFICATION)
    parser.add_argument("--once", action="store_true", help="兼容 worker 调度参数，无额外效果")
    args = parser.parse_args(argv)
    result, errors = verify(args.manifest, args.state, args.output_dir, args.expected_count)
    args.verification.parent.mkdir(parents=True, exist_ok=True)
    with args.verification.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    if errors:
        print(f"验收校验失败（{len(errors)} 项）: {args.verification}")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"验收校验通过: {args.verification} phase={result['phase']} collection_phase={result['collection_phase']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
