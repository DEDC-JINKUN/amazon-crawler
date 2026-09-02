from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from datetime import datetime, timezone
import tempfile
import threading
import urllib.error
import urllib.request
import unittest
from unittest.mock import patch
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
                worker._write_product_action(conn, "api-test", conn.execute("SELECT * FROM item_state").fetchone(), data, body, 200, None, raw_html_dir=root / "raw", context={"postal_code": "90001", "expected_country": "US", "expected_currency": "USD"}, transfer_bytes=12345)
                conn.close()
                payload = api.load_product(db, "US", "B00RCPDCQU")
                self.assertEqual(payload["asin"], "B00RCPDCQU")
                self.assertEqual(payload["source"], "selenium_dom")
                self.assertIsNotNone(payload["freshness"]["age_seconds"])
                self.assertTrue(payload["evidence"]["raw_html_path"])
                self.assertIn("90001", payload["evidence"]["context_json"])
                self.assertEqual(payload["evidence"]["transfer_bytes"], 12345)
                self.assertEqual(len(api.load_history(db, "US", "B00RCPDCQU")), 1)
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
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/readyz", timeout=2) as response:
                    self.assertEqual(json.loads(response.read())["ok"], True)
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/v1/asin/US/B00RCPDCQU", timeout=2)
                self.assertEqual(raised.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_sqlite_repository_reads_legacy_evidence_without_context_column(self):
        storage = load("collection_storage")
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "legacy.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE collection_evidence (id INTEGER PRIMARY KEY,run_id TEXT,url TEXT,http_status INTEGER,retrieved_at TEXT,source_type TEXT,content_hash TEXT,raw_html_path TEXT,block_reason TEXT,parser_version TEXT,error_code TEXT,marketplace TEXT,asin TEXT)")
            conn.execute("INSERT INTO collection_evidence VALUES(1,'r1','https://example.test',200,'2026-01-01T00:00:00+00:00','http_html','hash','US/A1/x.html',NULL,'v1',NULL,'US','A1')")
            conn.commit(); conn.close()
            rows = storage.SQLiteCollectionRepository(db).load_evidence("US", "A1")
            self.assertEqual(rows[0]["context_json"], None)

    def test_optional_api_key_protects_non_health_routes(self):
        api = load("collection_api")

        class Repository:
            def load_product(self, marketplace, asin):
                return {"asin": asin, "marketplace": marketplace}

            def load_job_status(self):
                return {"counts": {}}

            def load_evidence(self, marketplace, asin, limit=20):
                return []

            def load_history(self, marketplace, asin, limit=20):
                return [{"snapshot_id": 1, "captured_at": "2026-01-01T00:00:00+00:00"}] if asin == "B00RCPDCQU" else []

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

    def test_agent_identity_derives_requested_by_and_enforces_refresh_scope(self):
        api = load("collection_api")

        class Repository:
            def __init__(self):
                self.refreshes = []
                self.audits = []

            def load_product(self, marketplace, asin): return {"asin": asin, "marketplace": marketplace}
            def load_job_status(self): return {"counts": {}}
            def load_evidence(self, marketplace, asin, limit=20): return []
            def load_history(self, marketplace, asin, limit=20): return []
            def load_refresh_request(self, job_id): return {"job_id": job_id, "status": "queued"}
            def request_refresh(self, marketplace, asin, requested_by, reason):
                self.refreshes.append((marketplace, asin, requested_by, reason))
                return {"job_id": "refresh-1", "status": "queued", "requested_by": requested_by}
            def record_api_audit(self, **event): self.audits.append(event)

        repository = Repository()
        server = api.CollectionServer(("127.0.0.1", 0), repository=repository, api_key="master-key", agent_rate_limit=100)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            read_headers = api.agent_headers("master-key", "read-agent")
            refresh_headers = api.agent_headers("master-key", "refresh-agent")
            denied = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/asin/US/B00RCPDCQU/refresh",
                data=b'{"requested_by":"forged","reason":"stale_price"}',
                headers={"Content-Type": "application/json", **read_headers}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(denied, timeout=2)
            self.assertEqual(raised.exception.code, 403)

            allowed = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/asin/US/B00RCPDCQU/refresh",
                data=b'{"requested_by":"forged","reason":"stale_price"}',
                headers={"Content-Type": "application/json", **refresh_headers}, method="POST")
            with urllib.request.urlopen(allowed, timeout=2) as response:
                self.assertEqual(response.status, 202)
            self.assertEqual(repository.refreshes, [("US", "B00RCPDCQU", "refresh-agent", "stale_price")])
            self.assertEqual(repository.audits[-1]["agent_id"], "refresh-agent")
            self.assertEqual(repository.audits[-1]["outcome"], "accepted")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_sqlite_refresh_audit_is_durable_and_excludes_request_body_identity(self):
        worker = load("amazon_us_worker")
        api = load("collection_api")
        storage = load("collection_storage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "state.sqlite3"
            conn = worker.init_db(db)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            worker.initialize_manifest(conn, manifest, worker.DEFAULTS)
            conn.close()
            server = api.CollectionServer(("127.0.0.1", 0), db, api_key="master-key")
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/v1/asin/US/B00RCPDCQU/refresh",
                    data=b'{"requested_by":"forged","reason":"stale_price"}',
                    headers={"Content-Type": "application/json", **api.agent_headers("master-key", "refresh-agent")}, method="POST")
                with urllib.request.urlopen(request, timeout=2): pass
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
            conn = sqlite3.connect(db)
            row = conn.execute("SELECT agent_id,action,resource,outcome FROM collection_api_audit").fetchone()
            conn.close()
            self.assertEqual(row, ("refresh-agent", "request_refresh", "US/B00RCPDCQU", "accepted"))

    def test_agent_client_uses_only_scoped_credentials_for_read_and_refresh(self):
        api = load("collection_api")
        client_module = load("amazon_collection_client")

        class Repository:
            def load_product(self, marketplace, asin): return {"asin": asin, "marketplace": marketplace, "found": True}
            def load_job_status(self): return {"counts": {}}
            def load_evidence(self, marketplace, asin, limit=20): return []
            def load_history(self, marketplace, asin, limit=20): return []
            def load_refresh_request(self, job_id): return {"job_id": job_id, "status": "queued"}
            def request_refresh(self, marketplace, asin, requested_by, reason): return {"job_id": "refresh-1", "status": "queued", "requested_by": requested_by}

        server = api.CollectionServer(("127.0.0.1", 0), repository=Repository(), api_key="master-key")
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            read_client = client_module.AmazonCollectionClient(base_url, "read-agent", api.derive_agent_key("master-key", "read-agent"))
            self.assertEqual(read_client.get_product("B00RCPDCQU")["asin"], "B00RCPDCQU")
            refresh_client = client_module.AmazonCollectionClient(base_url, "refresh-agent", api.derive_agent_key("master-key", "refresh-agent"))
            self.assertEqual(refresh_client.request_refresh("B00RCPDCQU")["job"]["requested_by"], "refresh-agent")
            self.assertEqual(refresh_client.get_job("refresh-1")["job"]["status"], "queued")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_readyz_converts_unexpected_repository_error_to_503(self):
        api = load("collection_api")

        class BrokenRepository:
            def load_job_status(self):
                raise Exception("driver detail")

        server = api.CollectionServer(("127.0.0.1", 0), repository=BrokenRepository())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/readyz", timeout=2)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 503)
            self.assertEqual(json.loads(exc.read())["error"], "database_unavailable")
        else:
            self.fail(f"expected 503, got {response.status}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_readyz_reports_postgres_tenant_scope(self):
        api = load("collection_api")

        class ReadyRepository:
            tenant_id = "tenant-a"

            def load_schema_contract(self):
                return {"item_state": ["lease_expires_at", "lease_owner", "lease_token", "next_retry_at"], "collection_evidence": ["context_json", "transfer_bytes"]}

            def load_job_status(self):
                return {"counts": {}}

        server = api.CollectionServer(("127.0.0.1", 0), repository=ReadyRepository())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/readyz", timeout=2) as response:
                payload = json.loads(response.read())
            self.assertEqual(payload["tenant_id"], "tenant-a")
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

    def test_collection_api_postgres_repository_keeps_tenant_scope(self):
        api = load("collection_api")
        repository = api.create_repository("postgres", Path("unused.sqlite3"), "postgresql://example", "tenant-a")
        self.assertEqual(repository.tenant_id, "tenant-a")

    def test_required_api_key_refuses_missing_environment_value(self):
        api = load("collection_api")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "required API key"):
                api.resolve_api_key("MISSING_KEY", True)

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

            def load_history(self, marketplace, asin, limit=20):
                return [{"snapshot_id": 1, "captured_at": "2026-01-01T00:00:00+00:00"}] if asin == "B00RCPDCQU" else []

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
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/v1/asin/US/B00RCPDCQU/history", timeout=2) as response:
                self.assertEqual(len(json.loads(response.read())["items"]), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
