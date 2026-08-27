#!/usr/bin/env python3
"""Validate an Amazon US ASIN manifest without network access."""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "amazon_us_asin_manifest.csv"
HEADERS = ["asin", "url", "marketplace", "source_site_label", "source_workbook"]
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def validate_manifest(path: Path = DEFAULT_MANIFEST, expected_count: int | None = None) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        return [f"manifest 不存在: {path}"]
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != HEADERS:
                errors.append(f"字段必须严格为 {HEADERS}，实际为 {reader.fieldnames}")
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        return [f"无法读取 manifest: {exc}"]

    seen: set[tuple[str, str]] = set()
    for line_number, row in enumerate(rows, start=2):
        missing = [field for field in HEADERS if not (row.get(field) or "").strip()]
        if missing:
            errors.append(f"第 {line_number} 行缺少字段: {', '.join(missing)}")
            continue
        asin = row["asin"].strip().upper()
        marketplace = row["marketplace"].strip()
        url = row["url"].strip()
        if marketplace != "US":
            errors.append(f"第 {line_number} 行 marketplace 不是 US: {marketplace}")
        if not ASIN_RE.fullmatch(asin):
            errors.append(f"第 {line_number} 行 ASIN 格式错误: {asin}")
        parts = urlsplit(url)
        if parts.scheme.lower() != "https" or parts.hostname not in {"amazon.com", "www.amazon.com"}:
            errors.append(f"第 {line_number} 行 URL 域名不是 amazon.com: {url}")
        expected_url = f"https://www.amazon.com/dp/{asin}"
        if url != expected_url:
            errors.append(f"第 {line_number} 行 URL 与 ASIN 不一致，应为 {expected_url}")
        key = (marketplace, asin)
        if key in seen:
            errors.append(f"第 {line_number} 行复合键重复: {marketplace}+{asin}")
        seen.add(key)

    if expected_count is not None and len(rows) != expected_count:
        errors.append(f"数量不符: 实际 {len(rows)}，预期 {expected_count}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--expected-count", type=int, default=1892)
    args = parser.parse_args(argv)
    errors = validate_manifest(args.manifest, args.expected_count)
    if errors:
        print(f"manifest 校验失败（{len(errors)} 项）:")
        for error in errors:
            print(f"- {error}")
        return 1
    with args.manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        count = sum(1 for _ in handle) - 1
    print(f"manifest 校验通过: {args.manifest}，{count} 条，字段/域名/ASIN/唯一性均有效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
