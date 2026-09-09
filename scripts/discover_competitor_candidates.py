#!/usr/bin/env python3
"""Extract reviewable competitor ASIN candidates from saved Amazon HTML."""
from __future__ import annotations

import argparse
import csv
import gzip
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

HEADERS = ["asin", "marketplace", "source_type", "source_query", "source_url", "discovered_at", "status"]
ASIN_RE = re.compile(r"/(?:dp|gp/product|product)/([A-Za-z0-9]{10})(?:[/?#]|$)", re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_html(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    return path.read_text(encoding="utf-8", errors="replace")


def discover(input_paths: list[Path], *, source_type: str, source_query: str, source_url: str, output: Path, force: bool = False) -> int:
    if not input_paths:
        raise ValueError("at least one --input HTML file is required")
    if output.exists() and not force:
        raise FileExistsError(f"output exists; use --force to overwrite: {output}")
    # 目录输入：递归收集 *.html / *.html.gz（worker 落盘的是 gzip 格式）
    files: list[Path] = []
    for path in input_paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("*.html.gz")))
            files.extend(sorted(path.rglob("*.html")))
        else:
            files.append(path)
    if not files:
        raise ValueError(f"no HTML files found under: {', '.join(str(p) for p in input_paths)}")
    records: dict[str, dict[str, str]] = {}
    for path in files:
        html = _read_html(path)
        for raw_asin in re.findall(r"data-asin=[\"']([A-Za-z0-9]{10})[\"']", html, flags=re.IGNORECASE):
            asin = raw_asin.upper()
            records.setdefault(asin, {"asin": asin, "marketplace": "US", "source_type": source_type, "source_query": source_query, "source_url": source_url, "discovered_at": _now(), "status": "candidate"})
        for raw in re.findall(r"(?:href|data-url)=[\"']([^\"']+)[\"']", html, flags=re.IGNORECASE):
            absolute = urljoin(source_url or "https://www.amazon.com/", raw)
            parts = urlsplit(absolute)
            if parts.hostname not in {"amazon.com", "www.amazon.com"}:
                continue
            match = ASIN_RE.search(parts.path)
            if not match:
                continue
            asin = match.group(1).upper()
            records.setdefault(asin, {"asin": asin, "marketplace": "US", "source_type": source_type, "source_query": source_query, "source_url": source_url, "discovered_at": _now(), "status": "candidate"})
    rows = [records[key] for key in sorted(records)]
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADERS, lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)
    temporary.replace(output)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--source-type", default="keyword_search")
    parser.add_argument("--source-query", default="")
    parser.add_argument("--source-url", default="https://www.amazon.com/")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        count = discover(args.input, source_type=args.source_type, source_query=args.source_query, source_url=args.source_url, output=args.output, force=args.force)
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"discovered {count} candidate ASINs: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
