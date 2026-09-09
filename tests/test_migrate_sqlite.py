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


class MigrationTests(unittest.TestCase):
    def test_build_payload_maps_sqlite_rows_to_production_keys(self):
        worker = load("amazon_us_worker")
        migration = load("migrate_sqlite_to_postgres")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "state.sqlite3"
            conn = worker.init_db(db)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            worker.initialize_manifest(conn, manifest, worker.DEFAULTS)
            conn.close()
            payload = migration.build_payload(db, "tenant-a", "own")
            self.assertEqual(len(payload["asin_master"]), 1)
            self.assertEqual(payload["asin_master"][0]["subject_type"], "own")
            self.assertEqual(payload["item_state"][0]["tenant_id"], "tenant-a")
            self.assertEqual(payload["collection_evidence"], [])

    def test_child_table_mapping_keeps_marketplace_and_asin(self):
        worker = load("amazon_us_worker")
        migration = load("migrate_sqlite_to_postgres")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "state.sqlite3"
            conn = worker.init_db(db)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            worker.initialize_manifest(conn, manifest, worker.DEFAULTS)
            conn.execute("INSERT INTO media_asset(marketplace,asin,placement,unique_key) VALUES('US','B00RCPDCQU','gallery','key-1')")
            conn.commit()
            conn.close()
            payload = migration.build_payload(db, "tenant-a", "candidate")
            self.assertEqual(payload["media_asset"][0]["marketplace"], "US")
            self.assertEqual(payload["media_asset"][0]["asin"], "B00RCPDCQU")

    def test_evidence_mapping_keeps_collection_context(self):
        worker = load("amazon_us_worker")
        migration = load("migrate_sqlite_to_postgres")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "state.sqlite3"
            conn = worker.init_db(db)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            worker.initialize_manifest(conn, manifest, worker.DEFAULTS)
            worker._insert_evidence(conn, "run-1", "B00RCPDCQU", "https://www.amazon.com/dp/B00RCPDCQU", 200, "<html></html>", None, source_type="http_html", context={"postal_code": "90001", "expected_country": "US", "expected_currency": "USD"})
            conn.commit(); conn.close()
            payload = migration.build_payload(db, "tenant-a", "candidate")
            self.assertIn("90001", payload["collection_evidence"][0]["context_json"])

    def test_product_history_is_mapped_to_postgres_snapshots(self):
        worker = load("amazon_us_worker")
        migration = load("migrate_sqlite_to_postgres")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "state.sqlite3"
            conn = worker.init_db(db)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            worker.initialize_manifest(conn, manifest, worker.DEFAULTS)
            conn.execute("INSERT INTO product_snapshot_history(marketplace,asin,captured_at,source_type,raw_html_path,value_json) VALUES('US','B00RCPDCQU','2026-01-01T00:00:00+00:00','http_html','x.html',?)", ('{"price":"$1.00","bullets":[]}',))
            conn.commit()
            conn.close()
            payload = migration.build_payload(db, "tenant-a", "candidate")
            history_snapshots = [item for item in payload["product_snapshot"] if item["collected_at"] == "2026-01-01T00:00:00+00:00"]
            self.assertEqual(history_snapshots[0]["price"], "$1.00")

    def test_dry_run_does_not_require_postgres_dsn(self):
        migration = load("migrate_sqlite_to_postgres")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = load("amazon_us_worker")
            db = root / "state.sqlite3"
            conn = worker.init_db(db)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            worker.initialize_manifest(conn, manifest, worker.DEFAULTS)
            conn.close()
            self.assertEqual(migration.main(["--sqlite", str(db), "--dry-run"]), 0)

    def test_migrate_uses_transactional_db_api_connection(self):
        worker = load("amazon_us_worker")
        migration = load("migrate_sqlite_to_postgres")

        class Cursor:
            def __init__(self):
                self.executed = []
                self.batches = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql):
                self.executed.append(sql)

            def executemany(self, sql, rows):
                self.batches.append((sql, list(rows)))

        class Connection:
            def __init__(self):
                self.cursor_instance = Cursor()
                self.committed = False

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self):
                return self.cursor_instance

            def commit(self):
                self.committed = True

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "state.sqlite3"
            conn = worker.init_db(db)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            worker.initialize_manifest(conn, manifest, worker.DEFAULTS)
            conn.close()
            target = Connection()
            counts = migration.migrate(db, "postgresql://test", connect=lambda: target)
            self.assertEqual(counts["asin_master"], 1)
            self.assertTrue(target.committed)
            self.assertGreaterEqual(len(target.cursor_instance.batches), 2)

    def test_postgres_json_values_are_adapted(self):
        migration = load("migrate_sqlite_to_postgres")
        adapted = migration._adapt_postgres_value({"price": 12.3, "tags": ["a"]})
        self.assertIn("price", str(adapted))

    def test_missing_context_json_migrates_as_empty_object(self):
        migration = load("migrate_sqlite_to_postgres")
        adapted = migration._adapt_postgres_value(None, "context_json")
        self.assertIn("{}", str(adapted))

    def test_empty_nullable_integer_migrates_as_null(self):
        migration = load("migrate_sqlite_to_postgres")
        self.assertIsNone(migration._adapt_postgres_value("", "ordinal"))

    def test_sqlite_boolean_values_are_adapted(self):
        migration = load("migrate_sqlite_to_postgres")
        self.assertIs(migration._adapt_postgres_value(1, "is_primary"), True)
        self.assertIs(migration._adapt_postgres_value(0, "verified"), False)


if __name__ == "__main__":
    unittest.main()
