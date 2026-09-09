#!/usr/bin/env python3
"""Promote only operator-approved competitor candidates into the crawler manifest."""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

HEADERS = ["asin", "url", "marketplace", "source_site_label", "source_workbook"]
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def promote(candidates: Path, approved: Path, output: Path, force: bool = False) -> int:
    if output.exists() and not force:
        raise FileExistsError(f"output exists; use --force to overwrite: {output}")
    with candidates.open("r", encoding="utf-8-sig", newline="") as handle:
        candidate_rows = list(csv.DictReader(handle))
    approved_asins = {line.strip().upper() for line in approved.read_text(encoding="utf-8-sig").splitlines() if line.strip()}
    invalid = sorted(asin for asin in approved_asins if not ASIN_RE.fullmatch(asin))
    if invalid:
        raise ValueError(f"invalid approved ASIN: {invalid[0]}")
    rows: dict[str, dict[str, str]] = {}
    for row in candidate_rows:
        asin = str(row.get("asin") or "").strip().upper()
        if asin in approved_asins and ASIN_RE.fullmatch(asin):
            rows.setdefault(asin, {"asin": asin, "url": f"https://www.amazon.com/dp/{asin}", "marketplace": "US", "source_site_label": "competitor_approved", "source_workbook": candidates.name})
    missing = sorted(approved_asins - set(rows))
    if missing:
        raise ValueError(f"approved ASIN not found in candidates: {missing[0]}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADERS, lineterminator="\n")
        writer.writeheader(); writer.writerows(rows[asin] for asin in sorted(rows))
    temporary.replace(output)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--approved-asins", type=Path, required=True, help="one approved ASIN per line")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        count = promote(args.candidates, args.approved_asins, args.output, args.force)
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"promoted {count} approved competitor ASINs: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
