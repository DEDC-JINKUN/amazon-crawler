#!/usr/bin/env python3
"""按模板结构生成填充版《Amazon商品监控主档》。

数据源：DB asin_master（自有清单）+ product_snapshot（商品名）+ competitor_relations.csv（竞品关系）。
模板的 使用说明/采集策略 原样保留，关键词 保留表头，变更说明 追加导入记录。
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from copy import copy
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

ROOT = Path(__file__).resolve().parents[1]

ZEBRA_1 = PatternFill(fill_type="solid", fgColor="FFFFFF")
ZEBRA_2 = PatternFill(fill_type="solid", fgColor="F7F9FC")
THIN = Side(style="thin", color="D9DEE7")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
FONT = Font(name="Arial", size=10)
HEADER_FONT = Font(name="Arial", size=10, bold=True)
HEADER_FILL = PatternFill(fill_type="solid", fgColor="232F3E")
HEADER_FONT_WHITE = Font(name="Arial", size=10, bold=True, color="FFFFFF")


def load_own(dsn: str, tenant_id: str) -> list[dict[str, str]]:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.asin, COALESCE(p.title, '') AS title
                FROM amazon_us.asin_master m
                LEFT JOIN amazon_us.product_latest p
                  ON p.tenant_id = m.tenant_id AND p.asin = m.asin
                WHERE m.tenant_id = %s AND m.subject_type = 'own'
                ORDER BY m.asin
                """,
                (tenant_id,),
            )
            return [dict(r) for r in cur.fetchall()]


def style_data_region(ws, header_row: int, max_col: int) -> None:
    for cell in ws[header_row]:
        if cell.value is not None:
            cell.font = HEADER_FONT_WHITE
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = BORDER
    for row_idx in range(header_row + 1, ws.max_row + 1):
        fill = ZEBRA_1 if (row_idx - header_row) % 2 == 1 else ZEBRA_2
        for col_idx in range(1, max_col + 1):
            cell = ws.cell(row_idx, column=col_idx)
            cell.fill = fill
            cell.font = FONT
            cell.border = BORDER
            cell.alignment = Alignment(vertical="top")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--relations", type=Path, default=ROOT / "data" / "amazon_us" / "competitor_relations.csv")
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--tenant-id", default="amazon_us_main")
    parser.add_argument("--source-ref", default="Asin清单.xlsx")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        print(f"错误: 需要环境变量 {args.dsn_env}", file=sys.stderr)
        return 2
    if args.output.exists() and not args.force:
        raise SystemExit(f"output exists; use --force: {args.output}")

    own_rows = load_own(dsn, args.tenant_id)
    with args.relations.open("r", encoding="utf-8-sig", newline="") as handle:
        relations = list(csv.DictReader(handle))
    competitors = sorted({r["competitor_asin"] for r in relations})
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    wb = load_workbook(args.template)

    ws = wb["自有ASIN"]
    headers = [c.value for c in ws[1] if c.value is not None]
    for row in own_rows:
        ws.append([
            row["asin"], "US", row["title"] or "", "normal", "own_normal", "active",
            "manifest", args.source_ref, "", "", "",
        ])
    style_data_region(ws, 1, len(headers))

    ws = wb["竞品关系"]
    headers = [c.value for c in ws[1] if c.value is not None]
    for r in relations:
        ws.append([
            r["own_asin"], r["competitor_asin"], r["marketplace"], r["relation_type"],
            r["status"], "", r["cadence_profile"], r["source_type"], r["source_query"],
            r["source_url"], r["discovered_at"], "", "", "",
        ])
    style_data_region(ws, 1, len(headers))

    ws = wb["变更说明"]
    headers = [c.value for c in ws[1] if c.value is not None]
    ws.append(["initial_import", "自有ASIN", f"{len(own_rows)} rows",
               "从业务清单导入自有 ASIN（tenant=amazon_us_main，全部 own_normal，核心商品待业务确认）",
               "supervisor-agent", now, args.source_ref])
    ws.append(["initial_import", "竞品关系", f"{len(relations)} pairs / {len(competitors)} unique",
               "从 128+ 个自有商品页 raw HTML 提取 related_products 关联（link_competitor_relations.py），"
               "全部为 candidate 状态待审核",
               "supervisor-agent", now, "competitor_relations.csv"])
    style_data_region(ws, 1, len(headers))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    wb.save(args.output)
    print(f"own: {len(own_rows)}, relation pairs: {len(relations)}, unique competitors: {len(competitors)}")
    print(f"written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
