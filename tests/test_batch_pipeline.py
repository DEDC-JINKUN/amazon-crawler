#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BatchCheckpointTests(unittest.TestCase):
    def _setup(self, root: Path):
        worker = load("amazon_us_worker")
        manifest = root / "manifest.csv"
        manifest.write_text("\ufeffasin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.xlsx\n", encoding="utf-8")
        conn = worker.init_db(root / "state.sqlite3")
        config = dict(worker.DEFAULTS)
        config.update({"max_actions_per_run": 1, "review_page_limit": 0, "output_dir": root / "out"})
        worker.initialize_manifest(conn, manifest, config)
        return worker, conn, config

    def test_product_anchor_only_succeeds_without_review_fetch_cursor(self):
        html = """
        <html><body>
          <input id="ASIN" value="B00RCPDCQU">
          <link rel="canonical" href="https://www.amazon.com/dp/B00RCPDCQU">
          <div id="acrPopover">4.0 out of 5 stars</div>
          <a id="acrCustomerReviewLink" href="#averageCustomerReviewsAnchor">(17)</a>
          <div id="averageCustomerReviewsAnchor">Reviews</div>
        </body></html>
        """
        with tempfile.TemporaryDirectory() as directory:
            worker, conn, config = self._setup(Path(directory))
            row = conn.execute("SELECT * FROM item_state").fetchone()
            worker._set_status(conn, "US", row["asin"], "running", reason="test")
            data = worker.parse_product_html(html, row["url"])
            worker._write_product_action(conn, "test-run", conn.execute("SELECT * FROM item_state").fetchone(), data, html, None, None)
            state = conn.execute("SELECT status,next_review_url,next_review_page,fetched_review_count FROM item_state").fetchone()
            summary = conn.execute("SELECT next_page,status,fetched_count FROM review_summary").fetchone()
            snapshot = conn.execute("SELECT review_link,review_section_anchor,reported_review_count FROM product_snapshot").fetchone()
            self.assertEqual(tuple(state), ("succeeded", None, None, 0))
            self.assertEqual(tuple(summary), (None, "section_only", 0))
            self.assertEqual(tuple(snapshot), ("", "#averageCustomerReviewsAnchor", 17))
            conn.close()

        worker_html = (FIXTURES / "product_unavailable_video_aplus.html").read_text()
        page1 = (FIXTURES / "reviews_page_1.html").read_text()
        page2 = (FIXTURES / "reviews_page_2.html").read_text()

        class Adapter:
            def __init__(self):
                self.calls = []

            def fetch(self, url):
                self.calls.append(url)
                if "pageNumber=2" in url:
                    return page2, None
                if "/product-reviews/" in url:
                    return page1, None
                return worker_html, None

        with tempfile.TemporaryDirectory() as directory:
            worker, conn, config = self._setup(Path(directory))
            adapter = Adapter()
            worker.run_actions(conn, adapter, config)
            worker.run_actions(conn, adapter, config)
            worker.run_actions(conn, adapter, config)
            row = conn.execute("SELECT status,next_review_url,next_review_page,fetched_review_count FROM item_state").fetchone()
            self.assertEqual(tuple(row), ("succeeded", None, None, 2))
            self.assertEqual(adapter.calls, [
                "https://www.amazon.com/dp/B00RCPDCQU",
                "https://www.amazon.com/product-reviews/B00RCPDCQU",
                "https://www.amazon.com/product-reviews/B00RCPDCQU?pageNumber=2",
            ])
            worker.materialize_csvs(conn, Path(directory) / "out")
            with (Path(directory) / "out" / "review_record.csv").open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            conn.close()

    def test_running_recovery_and_invalid_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, conn, config = self._setup(Path(directory))
            conn.execute("UPDATE item_state SET status='running',resume_status='reviews_pending',task_stage='reviews'")
            conn.commit()
            conn.close()
            conn = worker.init_db(Path(directory) / "state.sqlite3")
            self.assertEqual(conn.execute("SELECT status FROM item_state").fetchone()[0], "reviews_pending")
            with self.assertRaises(ValueError):
                worker._set_status(conn, "US", "B00RCPDCQU", "product_done")
            conn.close()

    def test_status_extraction_and_block_phrase_boundary(self):
        worker = load("amazon_us_worker")
        class Driver:
            def execute_script(self, script):
                return [{"responseStatus": 429}]
        self.assertEqual(worker.extract_response_status(Driver()), 429)
        self.assertEqual(worker.classify_block(None, "robot vacuum cleaner"), None)
        self.assertEqual(worker.classify_block(403, ""), "http_403")
        self.assertEqual(worker.classify_block(429, ""), "http_429")

    def test_default_config_user_agent_is_transparent(self):
        worker = load("amazon_us_worker")
        self.assertIn("Agent/amazon-us-worker", worker.DEFAULTS["user_agent"])
        loaded = worker.load_config(ROOT / "config" / "amazon_us.example.toml")
        self.assertIn("Agent/amazon-us-worker", loaded["user_agent"])

    def test_config_transparency(self):
        worker = load("amazon_us_worker")
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('[worker]\nagent_name="audit-agent"\nuser_agent="Agent/audit-agent test"\nmax_actions_per_run=2\n', encoding="utf-8")
            loaded = worker.load_config(config)
            self.assertEqual(loaded["agent_name"], "audit-agent")
            self.assertEqual(loaded["max_actions_per_run"], 2)
            bad = Path(directory) / "bad.toml"
            bad.write_text('[worker]\nagent_name="audit-agent"\nuser_agent="hidden"\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                worker.load_config(bad)

    def test_transient_review_failure_retries_same_page_and_resets_attempts(self):
        product = (FIXTURES / "product_unavailable_video_aplus.html").read_text()
        page1 = (FIXTURES / "reviews_page_1.html").read_text()
        page2 = (FIXTURES / "reviews_page_2.html").read_text()

        class Adapter:
            def __init__(self):
                self.calls = []
                self.page2_failures = 0

            def fetch(self, url):
                self.calls.append(url)
                if "pageNumber=2" in url:
                    if self.page2_failures == 0:
                        self.page2_failures += 1
                        raise worker.AdapterFetchError("temporary")
                    return page2, None
                if "/product-reviews/" in url:
                    return page1, None
                return product, None

        with tempfile.TemporaryDirectory() as directory:
            worker, conn, config = self._setup(Path(directory))
            adapter = Adapter()
            for _ in range(4):
                worker.run_actions(conn, adapter, config)
            row = conn.execute("SELECT status,attempts,next_review_url,next_review_page FROM item_state").fetchone()
            self.assertEqual(tuple(row), ("succeeded", 0, None, None))
            self.assertEqual(adapter.calls.count("https://www.amazon.com/product-reviews/B00RCPDCQU?pageNumber=2"), 2)
            conn.close()

    def test_empty_review_page_is_retryable_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, conn, config = self._setup(Path(directory))
            worker._set_status(conn, "US", "B00RCPDCQU", "running", reason="test")
            conn.execute(
                "update item_state set task_stage='reviews',resume_status='reviews_pending',next_review_url=?,next_review_page=1,reported_review_count=17 where asin='B00RCPDCQU'",
                ("https://www.amazon.com/product-reviews/B00RCPDCQU",),
            )
            conn.commit()
            task = conn.execute("select * from item_state").fetchone()
            worker._write_review_action(
                conn,
                "test-run",
                task,
                1,
                task["next_review_url"],
                [],
                None,
                "<html><body>No review records</body></html>",
                200,
                None,
                0,
            )
            row = conn.execute("select status,attempts,next_review_url,next_review_page from item_state").fetchone()
            self.assertEqual(tuple(row), ("failed", 1, task["next_review_url"], 1))
            page = conn.execute("select status,next_url from review_page_state").fetchone()
            self.assertEqual(tuple(page), ("failed", task["next_review_url"]))
            conn.close()

    def test_continuous_failures_stop_at_max_attempts(self):
        class Adapter:
            def __init__(self):
                self.calls = 0

            def fetch(self, url):
                self.calls += 1
                raise worker.AdapterFetchError("temporary")

        with tempfile.TemporaryDirectory() as directory:
            worker, conn, config = self._setup(Path(directory))
            config["max_actions_per_run"] = 1
            conn.execute("UPDATE item_state SET max_attempts=2")
            conn.commit()
            adapter = Adapter()
            worker.run_actions(conn, adapter, config)
            worker.run_actions(conn, adapter, config)
            worker.run_actions(conn, adapter, config)
            row = conn.execute("SELECT status,attempts FROM item_state").fetchone()
            self.assertEqual(tuple(row), ("failed", 2))
            self.assertEqual(adapter.calls, 2)
            conn.close()

    def test_429_defers_product_and_review_without_losing_cursor(self):
        product = (FIXTURES / "product_unavailable_video_aplus.html").read_text()
        page1 = (FIXTURES / "reviews_page_1.html").read_text()

        class Product429:
            def fetch(self, url):
                return "Too many requests", 429

        with tempfile.TemporaryDirectory() as directory:
            worker, conn, config = self._setup(Path(directory))
            self.assertEqual(worker.run_actions(conn, Product429(), config), -1)
            row = conn.execute("SELECT status,attempts,block_reason FROM item_state").fetchone()
            self.assertEqual(tuple(row), ("pending", 0, "http_429"))

            class Good:
                def fetch(self, url):
                    return product, None
            worker.run_actions(conn, Good(), config)
            row = conn.execute("SELECT status,next_review_url,attempts FROM item_state").fetchone()
            self.assertEqual(row["status"], "reviews_pending")
            cursor = row["next_review_url"]

            class Review429:
                def fetch(self, url):
                    return "Too many requests", 429
            self.assertEqual(worker.run_actions(conn, Review429(), config), -1)
            row = conn.execute("SELECT status,next_review_url,attempts FROM item_state").fetchone()
            self.assertEqual(tuple(row), ("reviews_pending", cursor, 0))
            page = conn.execute("SELECT status,url,next_url FROM review_page_state").fetchone()
            self.assertEqual(tuple(page), ("deferred", cursor, cursor))

            class ReviewGood:
                def fetch(self, url):
                    self.url = url
                    return page1, None
            retry = ReviewGood()
            worker.run_actions(conn, retry, config)
            self.assertEqual(retry.url, cursor)
            conn.close()


if __name__ == "__main__":
    unittest.main()
