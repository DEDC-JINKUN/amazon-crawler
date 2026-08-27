from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
import tempfile
import threading
import urllib.error
import urllib.request
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


class CollectionApiTests(unittest.TestCase):
    def test_json_default_serializes_postgres_datetime(self):
        api = load("collection_api")
        self.assertEqual(api._json_default(datetime(2026, 1, 1, tzinfo=timezone.utc)), "2026-01-01T00:00:00+00:00")

    def test_local_api_returns_snapshot_and_status(self):
        worker = load("amazon_us_worker")
        api = load("collection_api")
        with self.subTest("database"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                db = root / "state.sqlite3"
                conn = worker.init_db(db)
                manifest = root / "manifest.csv"
                manifest.write_text(
                    "asin,url,marketplace,source_site_label,source_workbook\n"
                    "B00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n",
                    encoding="utf-8",
                )
                worker.initialize_manifest(conn, manifest, worker.DEFAULTS)
                row = conn.execute("SELECT * FROM item_state").fetchone()
                worker._set_status(conn, "US", row["asin"], "running", reason="test")
                body = (FIXTURES / "product_unavailable_video_aplus.html").read_text(encoding="utf-8")
                data = worker.parse_product_html(body, row["url"])
                worker._write_product_action(conn, "api-test", conn.execute("SELECT * FROM item_state").fetchone(), data, body, 200, None, raw_html_dir=root / "raw")
                conn.close()
                payload = api.load_product(db, "US", "B00RCPDCQU")
                self.assertEqual(payload["asin"], "B00RCPDCQU")
                self.assertEqual(payload["source"], "selenium_dom")
                self.assertIsNotNone(payload["freshness"]["age_seconds"])
                self.assertTrue(payload["evidence"]["raw_html_path"])
                self.assertEqual(len(api.load_evidence(db, "US", "B00RCPDCQU")), 1)
                self.assertEqual(api.load_job_status(db)["counts"]["reviews_pending"], 1)
                self.assertEqual(api.load_job_status(db)["refresh_requests"], {})

    def test_server_is_loopback_only_and_has_json_routes(self):
        api = load("collection_api")
        with self.assertRaises(ValueError):
            api.CollectionServer(("0.0.0.0", 0), Path("missing.sqlite3"))

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite3"
            worker = load("amazon_us_worker")
            conn = worker.init_db(db)
            conn.close()
            server = api.CollectionServer(("127.0.0.1", 0), db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/healthz", timeout=2) as response:
                    self.assertEqual(json.loads(response.read())["ok"], True)
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/v1/asin/US/B00RCPDCQU", timeout=2)
                self.assertEqual(raised.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_optional_api_key_protects_non_health_routes(self):
        api = load("collection_api")

        class Repository:
            def load_product(self, marketplace, asin):
                return {"asin": asin, "marketplace": marketplace}

            def load_job_status(self):
                return {"counts": {}}

            def load_evidence(self, marketplace, asin, limit=20):
                return []

            def request_refresh(self, marketplace, asin, requested_by, reason):
                return {"job_id": "test", "status": "queued"}

            def load_refresh_request(self, job_id):
                return None

        server = api.CollectionServer(("127.0.0.1", 0), repository=Repository(), api_key="secret")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/healthz", timeout=2) as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/v1/jobs/status", timeout=2)
            self.assertEqual(raised.exception.code, 401)
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/jobs/status", headers={"X-Collection-API-Key": "secret"})
            with urllib.request.urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_postgres_repository_uses_same_response_contract(self):
        storage = load("collection_storage")

        class Cursor:
            def __init__(self):
                self.rows = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=()):
                if "product_latest" in sql:
                    self.rows = [{"asin": "B00RCPDCQU", "subject_type": "own", "collected_at": "2026-01-01T00:00:00+00:00"}]
                elif "item_state" in sql and "GROUP BY" not in sql:
                    self.rows = [{"status": "succeeded", "subject_type": "own"}]
                elif "collection_evidence" in sql:
                    self.rows = [{"run_id": "run-1", "source_type": "http_html", "retrieved_at": "2026-01-01T00:00:00+00:00"}]
                elif "media_asset" in sql:
                    self.rows = [{"count": 2}]
                elif "content_module" in sql:
                    self.rows = [{"count": 3}]
                else:
                    self.rows = [{"status": "succeeded", "count": 1}]

            def fetchone(self):
                return self.rows[0] if self.rows else None

            def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self):
                self.cursor_instance = Cursor()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self):
                return self.cursor_instance

        repository = storage.PostgresCollectionRepository("postgresql://example", connect=Connection)
        payload = repository.load_product("US", "B00RCPDCQU")
        self.assertEqual(payload["asin"], "B00RCPDCQU")
        self.assertEqual(payload["source"], "http_html")
        self.assertIsNotNone(payload["freshness"]["age_seconds"])
        self.assertEqual(payload["counts"], {"media": 2, "content_modules": 3})
        self.assertEqual(repository.load_job_status()["counts"], {"succeeded": 1})

    def test_refresh_endpoint_only_enqueues_a_request(self):
        worker = load("amazon_us_worker")
        api = load("collection_api")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "state.sqlite3"
            conn = worker.init_db(db)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            worker.initialize_manifest(conn, manifest, worker.DEFAULTS)
            conn.close()
            server = api.CollectionServer(("127.0.0.1", 0), db)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/v1/asin/US/B00RCPDCQU/refresh",
                    data=b'{"requested_by":"test-agent","reason":"stale_price"}',
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    payload = json.loads(response.read())
                    self.assertEqual(response.status, 202)
                self.assertEqual(payload["job"]["status"], "queued")
                job_id = payload["job"]["job_id"]
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/v1/jobs/{job_id}", timeout=2) as response:
                    self.assertEqual(json.loads(response.read())["job"]["status"], "queued")
                conn = worker.init_db(db)
                count = conn.execute("SELECT COUNT(*) FROM refresh_request").fetchone()[0]
                conn.close()
                self.assertEqual(count, 1)

                class Adapter:
                    def fetch(self, url):
                        return (FIXTURES / "product_unavailable_video_aplus.html").read_text(encoding="utf-8"), 200

                conn = worker.init_db(db)
                config = dict(worker.DEFAULTS)
                config.update({"max_actions_per_run": 1, "output_dir": root / "out", "raw_html_dir": root / "raw"})
                worker.run_actions(conn, Adapter(), config, limit=1)
                self.assertEqual(conn.execute("SELECT status FROM refresh_request").fetchone()[0], "completed")
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_batch_endpoint_deduplicates_and_marks_missing_asins(self):
        api = load("collection_api")

        class Repository:
            def load_product(self, marketplace, asin):
                return {"asin": asin, "marketplace": marketplace, "found": True} if asin == "B00RCPDCQU" else None

            def load_job_status(self):
                return {"counts": {}}

            def load_evidence(self, marketplace, asin, limit=20):
                return []

            def request_refresh(self, marketplace, asin, requested_by, reason):
                return {"job_id": "test", "status": "queued"}

        server = api.CollectionServer(("127.0.0.1", 0), repository=Repository())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/asin/batch",
                data=b'{"marketplace":"US","asins":["B00RCPDCQU","B00RCPDCQU","B000000001"]}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
            self.assertEqual([item["asin"] for item in payload["items"]], ["B00RCPDCQU", "B000000001"])
            self.assertFalse(payload["items"][1]["found"])
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/v1/asin/US/B00RCPDCQU?fields=price,content", timeout=2) as response:
                freshness = json.loads(response.read())["freshness"]
            self.assertTrue(freshness["stale"])
            self.assertEqual(freshness["stale_groups"], ["price", "content"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
