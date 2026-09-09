#!/usr/bin/env python3
"""从自家商品页 raw HTML 提取竞品关系（own_asin → competitor_asin）。

只扫描属于自家清单（asin_master, subject_type=own）的页面目录，
按主档《竞品关系》表结构输出：marketplace + own_asin + competitor_asin + relation_type。
"""
from __future__ import annotations

import argparse
import csv
import gzip
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

ASIN_RE = re.compile(r"/(?:dp|gp/product|product)/([A-Za-z0-9]{10})(?:[/?#]|$)", re.IGNORECASE)
HREF_RE = re.compile(r"(?:href|data-url)=[\"']([^\"']+)[\"']", re.IGNORECASE)
DATA_ASIN_RE = re.compile(r"data-asin=[\"']([A-Za-z0-9]{10})[\"']", re.IGNORECASE)

REL_HEADERS = [
    "own_asin", "competitor_asin", "marketplace", "relation_type", "status",
    "priority", "cadence_profile", "source_type", "source_query", "source_url",
    "discovered_at", "approved_by", "approved_at", "notes",
]


def load_own_asins(dsn: str, tenant_id: str) -> set[str]:
    import psycopg
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT asin FROM amazon_us.asin_master WHERE tenant_id=%s AND subject_type='own'",
                (tenant_id,),
            )
            return {row[0] for row in cur.fetchall()}


def read_html(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    return path.read_text(encoding="utf-8", errors="replace")


def extract_asins(html: str) -> set[str]:
    found = {m.upper() for m in DATA_ASIN_RE.findall(html)}
    for raw in HREF_RE.findall(html):
        absolute = urljoin("https://www.amazon.com/", raw)
        parts = urlsplit(absolute)
        if parts.hostname not in {"amazon.com", "www.amazon.com"}:
            continue
        match = ASIN_RE.search(parts.path)
        if match:
            found.add(match.group(1).upper())
    return found


def build_relations(raw_html_dir: Path, own_asins: set[str]) -> dict[str, set[str]]:
    relations: dict[str, set[str]] = defaultdict(set)
    for asin_dir in sorted(raw_html_dir.iterdir()):
        if not asin_dir.is_dir() or asin_dir.name.upper() not in own_asins:
            continue
        own_asin = asin_dir.name.upper()
        for html_path in sorted(asin_dir.rglob("*.html.gz")) + sorted(asin_dir.rglob("*.html")):
            for candidate in extract_asins(read_html(html_path)):
                if candidate not in own_asins and candidate != own_asin:
                    relations[candidate].add(own_asin)
    return relations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--tenant-id", default="amazon_us_main")
    parser.add_argument("--raw-html", type=Path, default=ROOT / "data" / "amazon_us" / "raw_html" / "US")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "amazon_us" / "competitor_relations.csv")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        print(f"错误: 需要环境变量 {args.dsn_env}", file=sys.stderr)
        return 2
    if args.output.exists() and not args.force:
        raise SystemExit(f"output exists; use --force: {args.output}")
    if not args.raw_html.is_dir():
        raise SystemExit(f"raw html dir not found: {args.raw_html}")

    own_asins = load_own_asins(dsn, args.tenant_id)
    relations = build_relations(args.raw_html, own_asins)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_name(f".{args.output.name}.tmp")
    pair_count = 0
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REL_HEADERS, lineterminator="\n")
        writer.writeheader()
        for competitor in sorted(relations):
            for own_asin in sorted(relations[competitor]):
                writer.writerow({
                    "own_asin": own_asin,
                    "competitor_asin": competitor,
                    "marketplace": "US",
                    "relation_type": "related_products",
                    "status": "candidate",
                    "priority": "",
                    "cadence_profile": "candidate",
                    "source_type": "related_products",
                    "source_query": "",
                    "source_url": f"https://www.amazon.com/dp/{own_asin}",
                    "discovered_at": now,
                    "approved_by": "",
                    "approved_at": "",
                    "notes": "",
                })
                pair_count += 1
    tmp.replace(args.output)
    pages = sum(1 for d in args.raw_html.iterdir() if d.is_dir() and d.name.upper() in own_asins)
    print(f"own asins: {len(own_asins)}, own pages scanned: {pages}, "
          f"unique competitors: {len(relations)}, relation pairs: {pair_count}")
    print(f"written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
