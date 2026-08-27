from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CoverageReportTests(unittest.TestCase):
    def test_empty_initialized_database_has_zero_coverage(self):
        worker = load("amazon_us_worker")
        report = load("coverage_report")
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite3"
            conn = worker.init_db(db)
            conn.close()
            result = report.build_report(db)
            self.assertEqual(result["product_count"], 0)
            self.assertEqual(result["table_counts"]["collection_evidence"], 0)
            self.assertEqual(result["field_coverage"]["identity"]["title"]["rate"], 0.0)

    def test_report_counts_fixture_product_and_evidence(self):
        worker = load("amazon_us_worker")
        report = load("coverage_report")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "state.sqlite3"
            manifest = root / "manifest.csv"
            manifest.write_text(
                "asin,url,marketplace,source_site_label,source_workbook\n"
                "B00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n",
                encoding="utf-8",
            )
            conn = worker.init_db(db)
            worker.initialize_manifest(conn, manifest, worker.DEFAULTS)
            row = conn.execute("SELECT * FROM item_state").fetchone()
            worker._set_status(conn, "US", row["asin"], "running", reason="test")
            body = (ROOT / "tests" / "fixtures" / "product_unavailable_video_aplus.html").read_text(encoding="utf-8")
            data = worker.parse_product_html(body, row["url"])
            worker._write_product_action(conn, "coverage-test", conn.execute("SELECT * FROM item_state").fetchone(), data, body, 200, None, raw_html_dir=root / "raw")
            conn.close()
            result = report.build_report(db)
            self.assertEqual(result["product_count"], 1)
            self.assertEqual(result["evidence_source"]["selenium_dom"], 1)
            self.assertEqual(result["field_coverage"]["identity"]["title"]["observed"], 1)

    def test_report_distinguishes_empty_html_containers(self):
        report = load("coverage_report")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            html = root / "empty.html"
            html.write_text('<html><div id="feature-bullets"></div><div id="productDescription"></div></html>', encoding="utf-8")
            result = report._html_field_state(html)
            self.assertEqual(result, {"bullets": "empty", "description": "empty"})


if __name__ == "__main__":
    unittest.main()
