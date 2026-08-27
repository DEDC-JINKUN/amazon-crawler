#!/usr/bin/env python3
"""Generate an auditable collection coverage report from SQLite."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

FIELD_GROUPS = {
    "identity": ("canonical_url", "title", "brand"),
    "commercial": ("price", "availability"),
    "content": ("bullets_json", "product_description", "specs_json", "buy_box_json"),
    "social": ("rating", "reported_review_count", "review_count"),
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _present(value: Any) -> bool:
    return value is not None and str(value).strip() not in {"", "[]", "{}"}


def _counts(rows: list[sqlite3.Row], fields: tuple[str, ...]) -> dict[str, dict[str, float | int]]:
    total = len(rows)
    result: dict[str, dict[str, float | int]] = {}
    for field in fields:
        observed = sum(1 for row in rows if _present(row[field]))
        result[field] = {"observed": observed, "total": total, "rate": round(observed / total, 4) if total else 0.0}
    return result


class _FieldContainerParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.active: str | None = None
        self.depth = 0
        self.text: list[str] = []
        self.list_items = 0
        self.found: dict[str, tuple[str, int]] = {}

    @staticmethod
    def _field(attrs: list[tuple[str, str | None]]) -> str | None:
        values = " ".join(str(value or "") for name, value in attrs if name in {"id", "class"}).lower()
        if "feature-bullets" in values or "product-bullets" in values:
            return "bullets"
        if any(token in values for token in ("productdescription", "product-description", "bookdescription")):
            return "description"
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.active is not None:
            self.depth += 1
            if tag.lower() == "li":
                self.list_items += 1
            return
        field = self._field(attrs)
        if field is not None and field not in self.found:
            self.active, self.depth, self.text, self.list_items = field, 1, [], 0

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.active is not None:
            if tag.lower() == "li":
                self.list_items += 1
            return
        field = self._field(attrs)
        if field is not None and field not in self.found:
            self.found[field] = ("", 0)

    def handle_endtag(self, tag: str) -> None:
        if self.active is None:
            return
        self.depth -= 1
        if self.depth <= 0:
            self.found[self.active] = (" ".join(self.text), self.list_items)
            self.active = None

    def handle_data(self, data: str) -> None:
        if self.active is not None:
            self.text.append(data)


def _html_field_state(path: Path) -> dict[str, str]:
    """Classify whether the page exposed content for bullets/description."""
    if path is None or not path.exists():
        return {"bullets": "uninspectable", "description": "uninspectable"}
    try:
        html = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"bullets": "uninspectable", "description": "uninspectable"}
    result: dict[str, str] = {}
    markers = {
        "bullets": (r"id=[\"']feature-bullets[\"']", r"(?:id|class)=[\"'][^\"']*(?:feature-bullets|product-bullets)[^\"']*[\"']"),
        "description": (r"id=[\"']productDescription[\"']", r"(?:id|class)=[\"'][^\"']*(?:productDescription|product-description|bookDescription)[^\"']*[\"']"),
    }
    for field, marker_options in markers.items():
        match = next((re.search(marker, html, flags=re.IGNORECASE) for marker in marker_options if re.search(marker, html, flags=re.IGNORECASE)), None)
        if not match:
            result[field] = "not_present"
            continue
        content_start = html.find(">", match.end()) + 1
        if content_start <= 0:
            result[field] = "uninspectable"
            continue
        closing = re.search(r"</div\s*>", html[content_start:], flags=re.IGNORECASE)
        window = html[content_start : content_start + (closing.start() if closing else 12000)]
        window = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", window, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", window)
        text = re.sub(r"\s+", " ", text).strip()
        result[field] = "present" if len(text) > 20 or (field == "bullets" and "<li" in window.lower()) else "empty"
    return result


def build_report(db_path: Path, raw_html_dir: Path | None = None) -> dict[str, Any]:
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        product_rows = list(conn.execute("SELECT * FROM product_snapshot"))
        status_rows = conn.execute("SELECT status, COUNT(*) AS count FROM item_state GROUP BY status ORDER BY status").fetchall()
        source_rows = conn.execute("SELECT COALESCE(source_type, 'unknown') AS key, COUNT(*) AS count FROM collection_evidence GROUP BY source_type ORDER BY source_type").fetchall()
        block_rows = conn.execute("SELECT COALESCE(block_reason, 'none') AS key, COUNT(*) AS count FROM collection_evidence GROUP BY block_reason ORDER BY block_reason").fetchall()
        html_states = {"bullets": {}, "description": {}}
        if raw_html_dir is not None:
            for row in product_rows:
                evidence = conn.execute(
                    "SELECT raw_html_path FROM collection_evidence WHERE marketplace=? AND asin=? ORDER BY id DESC LIMIT 1",
                    (row["marketplace"], row["asin"]),
                ).fetchone()
                path = raw_html_dir / str(evidence["raw_html_path"]) if evidence and evidence["raw_html_path"] else None
                states = _html_field_state(path)
                for field, state in states.items():
                    html_states[field][state] = html_states[field].get(state, 0) + 1
        table_counts = {}
        for table in ("product_snapshot", "media_asset", "content_module", "review_summary", "review_record", "collection_evidence"):
            table_counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return {
            "schema_version": "amazon-us-coverage-v1",
            "generated_at": _now(),
            "database": str(db_path),
            "task_status": {row["status"]: row["count"] for row in status_rows},
            "product_count": len(product_rows),
            "field_coverage": {group: _counts(product_rows, fields) for group, fields in FIELD_GROUPS.items()},
            "table_counts": table_counts,
            "evidence_source": {row["key"]: row["count"] for row in source_rows},
            "evidence_block_reason": {row["key"]: row["count"] for row in block_rows},
            "html_field_state": html_states if raw_html_dir is not None else None,
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("state/amazon_us.sqlite3"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--raw-html-dir", type=Path, help="Directory containing evidence raw_html_path files")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.db, args.raw_html_dir)
    except (OSError, sqlite3.Error) as exc:
        print(f"error: {exc}")
        return 1
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
