#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sqlite3
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


class CheckpointTests(unittest.TestCase):
    def test_idempotent_manifest_and_review_resume(self):
        worker = load("amazon_us_worker")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.csv"
            manifest.write_text("\ufeffasin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.xlsx\n", encoding="utf-8")
            conn = worker.init_db(root / "state.sqlite3")
            self.assertEqual(worker.initialize_manifest(conn, manifest), 1)
            self.assertEqual(worker.initialize_manifest(conn, manifest), 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM item_state").fetchone()[0], 1)
            paginator = worker.ReviewPaginator(conn, "B00RCPDCQU", "https://www.amazon.com/reviews?page=1")
            records = [{"review_id": "R1", "rating": "5", "title": "T", "body": "B", "review_url": "", "review_date": "", "verified": "", "page": 1}]
            paginator.record_page(1, "https://www.amazon.com/reviews?page=1", "https://www.amazon.com/reviews?page=2", records)
            paginator.record_page(1, "https://www.amazon.com/reviews?page=1", "https://www.amazon.com/reviews?page=2", records)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_record").fetchone()[0], 1)
            edited = {**records[0], "body": "edited body", "page": 1}
            paginator.record_page(1, "https://www.amazon.com/reviews?page=1", "https://www.amazon.com/reviews?page=2", [edited])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_record").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT body FROM review_record WHERE review_id='R1'").fetchone()[0], "edited body")
            self.assertEqual(paginator.resume(), (2, "https://www.amazon.com/reviews?page=2"))
            conn.close()

    def test_block_stop_reasons(self):
        worker = load("amazon_us_worker")
        self.assertEqual(worker.classify_block(403, ""), "http_403")
        self.assertEqual(worker.classify_block(429, ""), "http_429")
        self.assertEqual(worker.classify_block(None, "Please complete the CAPTCHA"), "captcha")
        self.assertEqual(worker.classify_block(None, "robot check"), "robot")


if __name__ == "__main__":
    unittest.main()
