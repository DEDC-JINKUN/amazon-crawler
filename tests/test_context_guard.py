from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ContextGuardTests(unittest.TestCase):
    def test_rejects_hkd_and_non_us_delivery_for_us_context(self):
        guard = load("context_guard")
        errors = guard.validate_context(
            {"price": "HKD235.11", "availability": "Deliver to Hong Kong", "buy_box": {}},
            {"expected_country": "US", "expected_currency": "USD"},
        )
        self.assertEqual(errors, ["currency_mismatch", "delivery_country_mismatch"])

    def test_context_mismatch_does_not_write_product_snapshot(self):
        worker = load("amazon_us_worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "state.sqlite3"
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            conn = worker.init_db(db)
            worker.initialize_manifest(conn, manifest, worker.DEFAULTS)
            row = conn.execute("SELECT * FROM item_state").fetchone()
            worker._set_status(conn, "US", row["asin"], "running", reason="test")
            data = {"asin": "B00RCPDCQU", "canonical_url": "https://www.amazon.com/dp/B00RCPDCQU", "price": "HKD235.11"}
            worker._write_product_action(conn, "context-test", conn.execute("SELECT * FROM item_state").fetchone(), data, "page", 200, None, error_code="context_mismatch:currency_mismatch", context={"postal_code": "90001", "expected_country": "US", "expected_currency": "USD"})
            self.assertIsNone(conn.execute("SELECT 1 FROM product_snapshot").fetchone())
            self.assertEqual(conn.execute("SELECT status FROM item_state").fetchone()[0], "failed")
            self.assertEqual(conn.execute("SELECT error_code FROM collection_evidence ORDER BY id DESC LIMIT 1").fetchone()[0], "context_mismatch:currency_mismatch")
            self.assertIn("90001", conn.execute("SELECT context_json FROM collection_evidence ORDER BY id DESC LIMIT 1").fetchone()[0])
            conn.close()


if __name__ == "__main__":
    unittest.main()
