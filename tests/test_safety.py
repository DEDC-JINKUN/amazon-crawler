#!/usr/bin/env python3
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


class SafetyTests(unittest.TestCase):
    def test_asin_mismatch_fails_without_wrong_snapshot(self):
        worker = load("amazon_us_worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.csv"
            manifest.write_text("\ufeffasin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,x,x\n", encoding="utf-8")
            conn = worker.init_db(root / "state.sqlite3")
            worker.initialize_manifest(conn, manifest)
            row = conn.execute("SELECT * FROM item_state").fetchone()
            worker._set_status(conn, "US", row["asin"], "running", reason="test")
            worker._write_product_action(conn, "test-run", conn.execute("SELECT * FROM item_state").fetchone(), {"asin": "B00RCPDI50", "canonical_url": "https://www.amazon.com/dp/B00RCPDI50"}, "fixture", None, None)
            state = conn.execute("SELECT status,last_error FROM item_state").fetchone()
            self.assertEqual(tuple(state), ("failed", "asin_mismatch"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM product_snapshot").fetchone()[0], 0)
            evidence = conn.execute("SELECT error_code FROM collection_evidence ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(evidence[0], "asin_mismatch")
            conn.close()

    def test_same_asin_clp_canonical_is_accepted(self):
        worker = load("amazon_us_worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.csv"
            manifest.write_text("\ufeffasin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,x,x\n", encoding="utf-8")
            conn = worker.init_db(root / "state.sqlite3")
            worker.initialize_manifest(conn, manifest)
            worker._set_status(conn, "US", "B00RCPDCQU", "running", reason="test")
            worker._write_product_action(
                conn,
                "test-run",
                conn.execute("SELECT * FROM item_state").fetchone(),
                {"asin": "B00RCPDCQU", "canonical_url": "https://www.amazon.com/clp/B00RCPDCQU", "title": "Example"},
                "fixture",
                200,
                None,
            )
            self.assertEqual(conn.execute("SELECT status FROM item_state").fetchone()[0], "succeeded")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM product_snapshot").fetchone()[0], 1)
            conn.close()

    def test_block_text_ignores_script_style_noscript(self):
        worker = load("amazon_us_worker")
        html = '<html><head><script>var message = "captcha";</script><style>.captcha{}</style><noscript>captcha</noscript><title>Normal product</title></head><body><input id="ASIN" value="B00RCPDCQU"><link rel="canonical" href="https://www.amazon.com/dp/B00RCPDCQU"><h1 id="productTitle">Normal product</h1></body></html>'
        result = worker.parse_product_html(html, "https://www.amazon.com/dp/B00RCPDCQU")
        self.assertIsNone(result["block_reason"])

    def test_aws_waf_challenge_is_blocked_before_visible_text_filtering(self):
        worker = load("amazon_us_worker")
        html = """
        <html><head>
          <script>window.awsWafCookieDomainList = []; AwsWafIntegration.getToken();</script>
          <script src="https://example.token.awswaf.com/challenge.js"></script>
        </head><body><div id="challenge-container"></div></body></html>
        """

        self.assertEqual(worker.classify_block(202, html), "waf_challenge")
        self.assertEqual(worker.parse_product_html(html, "https://www.amazon.com/dp/B07KSYGZPD")["block_reason"], "waf_challenge")

    def test_canonical_must_be_amazon_us(self):
        worker = load("amazon_us_worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.csv"
            manifest.write_text("\ufeffasin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,x,x\n", encoding="utf-8")
            conn = worker.init_db(root / "state.sqlite3")
            worker.initialize_manifest(conn, manifest)
            worker._set_status(conn, "US", "B00RCPDCQU", "running", reason="test")
            worker._write_product_action(
                conn,
                "test-run",
                conn.execute("SELECT * FROM item_state").fetchone(),
                {"asin": "B00RCPDCQU", "canonical_url": "https://evil.example/dp/B00RCPDCQU"},
                "fixture",
                200,
                None,
            )
            self.assertEqual(conn.execute("SELECT status FROM item_state").fetchone()[0], "failed")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM product_snapshot").fetchone()[0], 0)
            conn.close()

    def test_blocking_page_stops_and_does_not_retry(self):
        worker = load("amazon_us_worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.csv"
            manifest.write_text("\ufeffasin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,x,x\n", encoding="utf-8")
            conn = worker.init_db(root / "state.sqlite3")
            worker.initialize_manifest(conn, manifest)
            row = conn.execute("SELECT * FROM item_state").fetchone()
            worker._set_status(conn, "US", row["asin"], "running", reason="test")
            worker._write_product_action(conn, "test-run", conn.execute("SELECT * FROM item_state").fetchone(), {}, "robot check", 403, "http_403")
            state = conn.execute("SELECT status,block_reason,attempts FROM item_state").fetchone()
            self.assertEqual(tuple(state), ("blocked", "http_403", 0))
            self.assertEqual(len(worker._select_actions(conn, 10)), 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
