from pathlib import Path
import importlib.util
import sqlite3
import tempfile


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("collection_metrics", ROOT / "scripts" / "collection_metrics.py")
metrics = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(metrics)


def test_build_report_summarizes_one_run_read_only():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db = root / "state.sqlite3"
        raw = root / "raw"
        raw_path = raw / "US" / "B000000001" / "page.html"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_text("x" * 12, encoding="utf-8")
        conn = sqlite3.connect(db)
        conn.executescript("""
        CREATE TABLE collection_evidence (id INTEGER PRIMARY KEY,run_id TEXT,asin TEXT,marketplace TEXT,url TEXT,http_status INTEGER,retrieved_at TEXT,source_type TEXT,error_code TEXT,block_reason TEXT,raw_html_path TEXT);
        CREATE TABLE item_state (asin TEXT,marketplace TEXT,status TEXT);
        CREATE TABLE product_snapshot (asin TEXT,marketplace TEXT);
        CREATE TABLE media_asset (asin TEXT,marketplace TEXT);
        CREATE TABLE content_module (asin TEXT,marketplace TEXT);
        CREATE TABLE review_summary (asin TEXT,marketplace TEXT);
        CREATE TABLE review_record (asin TEXT,marketplace TEXT);
        """)
        conn.execute("INSERT INTO collection_evidence VALUES(1,'run-1','B000000001','US','https://www.amazon.com/dp/B000000001',200,'2026-01-01T00:00:00+00:00','http_html',NULL,NULL,'US/B000000001/page.html')")
        conn.execute("INSERT INTO item_state VALUES('B000000001','US','reviews_pending')")
        conn.execute("INSERT INTO product_snapshot VALUES('B000000001','US')")
        conn.commit(); conn.close()
        result = metrics.build_report(db, raw, "run-1")
        assert result["success_page_count"] == 1
        assert result["unique_successful_asin_count"] == 1
        assert result["task_status_counts"] == {"reviews_pending": 1}
        assert result["bytes_total"] == 12
        assert result["table_counts"]["product_snapshot"] == 1


def test_successful_asins_require_clean_product_page_and_dedupe_raw_path():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db = root / "state.sqlite3"
        raw = root / "raw"
        for asin, name in (("B000000001", "ok.html"), ("B000000002", "review.html"), ("B000000003", "mismatch.html")):
            path = raw / "US" / asin / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x" * 10, encoding="utf-8")
        conn = sqlite3.connect(db)
        conn.executescript("""
        CREATE TABLE collection_evidence (id INTEGER PRIMARY KEY,run_id TEXT,asin TEXT,marketplace TEXT,url TEXT,http_status INTEGER,retrieved_at TEXT,source_type TEXT,error_code TEXT,block_reason TEXT,raw_html_path TEXT);
        CREATE TABLE item_state (asin TEXT,marketplace TEXT,status TEXT);
        CREATE TABLE product_snapshot (asin TEXT,marketplace TEXT);
        CREATE TABLE media_asset (asin TEXT,marketplace TEXT);
        CREATE TABLE content_module (asin TEXT,marketplace TEXT);
        CREATE TABLE review_summary (asin TEXT,marketplace TEXT);
        CREATE TABLE review_record (asin TEXT,marketplace TEXT);
        INSERT INTO collection_evidence VALUES(1,'run-1','B000000001','US','https://www.amazon.com/dp/B000000001',200,'2026-01-01T00:00:00+00:00','http_html',NULL,NULL,'US/B000000001/ok.html');
        INSERT INTO collection_evidence VALUES(2,'run-1','B000000002','US','https://www.amazon.com/product-reviews/B000000002',200,'2026-01-01T00:00:01+00:00','http_html',NULL,NULL,'US/B000000002/review.html');
        INSERT INTO collection_evidence VALUES(3,'run-1','B000000003','US','https://www.amazon.com/dp/B000000003',200,'2026-01-01T00:00:02+00:00','http_html',NULL,NULL,'US/B000000003/mismatch.html');
        INSERT INTO collection_evidence VALUES(4,'run-1','B000000003','US','https://www.amazon.com/dp/B000000003',200,'2026-01-01T00:00:03+00:00','http_html','asin_mismatch',NULL,'US/B000000003/mismatch.html');
        INSERT INTO item_state VALUES('B000000001','US','succeeded');
        INSERT INTO item_state VALUES('B000000002','US','reviews_pending');
        INSERT INTO item_state VALUES('B000000003','US','failed');
        """)
        conn.commit()
        conn.close()

        result = metrics.build_report(db, raw, "run-1")
        assert result["unique_asin_count"] == 3
        assert result["unique_successful_asin_count"] == 1
        assert result["bytes_total"] == 30
        assert result["raw_files_read"] == 3


def test_transfer_bytes_are_reported_without_raw_html_directory():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "state.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript("""
        CREATE TABLE collection_evidence (id INTEGER PRIMARY KEY,run_id TEXT,asin TEXT,marketplace TEXT,url TEXT,http_status INTEGER,transfer_bytes INTEGER,retrieved_at TEXT,source_type TEXT,error_code TEXT,block_reason TEXT,raw_html_path TEXT);
        CREATE TABLE item_state (asin TEXT,marketplace TEXT,status TEXT);
        CREATE TABLE product_snapshot (asin TEXT,marketplace TEXT);
        CREATE TABLE media_asset (asin TEXT,marketplace TEXT);
        CREATE TABLE content_module (asin TEXT,marketplace TEXT);
        CREATE TABLE review_summary (asin TEXT,marketplace TEXT);
        CREATE TABLE review_record (asin TEXT,marketplace TEXT);
        INSERT INTO collection_evidence VALUES(1,'run-1','B000000001','US','https://www.amazon.com/dp/B000000001',200,1234,'2026-01-01T00:00:00+00:00','http_html',NULL,NULL,NULL);
        """)
        conn.commit()
        conn.close()

        result = metrics.build_report(db, run_id="run-1")
        assert result["transfer_bytes_total"] == 1234
        assert result["transfer_bytes_known_count"] == 1


def test_all_runs_aggregates_evidence_and_current_product_snapshots():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "state.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript("""
        CREATE TABLE collection_evidence (id INTEGER PRIMARY KEY,run_id TEXT,asin TEXT,marketplace TEXT,url TEXT,http_status INTEGER,transfer_bytes INTEGER,retrieved_at TEXT,source_type TEXT,error_code TEXT,block_reason TEXT,raw_html_path TEXT);
        CREATE TABLE item_state (asin TEXT,marketplace TEXT,status TEXT);
        CREATE TABLE product_snapshot (asin TEXT,marketplace TEXT);
        CREATE TABLE media_asset (asin TEXT,marketplace TEXT);
        CREATE TABLE content_module (asin TEXT,marketplace TEXT);
        CREATE TABLE review_summary (asin TEXT,marketplace TEXT);
        CREATE TABLE review_record (asin TEXT,marketplace TEXT);
        INSERT INTO collection_evidence VALUES(1,'r1','B000000001','US','https://www.amazon.com/dp/B000000001',200,100,'2026-01-01T00:00:00+00:00','http_html',NULL,NULL,NULL);
        INSERT INTO collection_evidence VALUES(2,'r2','B000000002','US','https://www.amazon.com/dp/B000000002',200,200,'2026-01-01T00:01:00+00:00','http_html',NULL,NULL,NULL);
        INSERT INTO item_state VALUES('B000000001','US','succeeded');
        INSERT INTO item_state VALUES('B000000002','US','failed');
        INSERT INTO product_snapshot VALUES('B000000001','US');
        """)
        conn.commit()
        conn.close()
        result = metrics.build_report(db, all_runs=True)
        assert result["run_id"] == "all-runs"
        assert result["evidence_count"] == 2
        assert result["transfer_bytes_total"] == 300
        assert result["unique_successful_asin_count"] == 1


def test_traffic_categories_keep_http_browser_and_proxy_scopes_separate_with_unknown_null():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "state.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript("""
        CREATE TABLE collection_evidence (
          id INTEGER PRIMARY KEY,run_id TEXT,asin TEXT,marketplace TEXT,url TEXT,http_status INTEGER,
          transfer_bytes INTEGER,retrieved_at TEXT,source_type TEXT,error_code TEXT,block_reason TEXT,
          raw_html_path TEXT,context_json TEXT
        );
        CREATE TABLE item_state (asin TEXT,marketplace TEXT,status TEXT);
        CREATE TABLE product_snapshot (asin TEXT,marketplace TEXT);
        CREATE TABLE media_asset (asin TEXT,marketplace TEXT);
        CREATE TABLE content_module (asin TEXT,marketplace TEXT);
        CREATE TABLE review_summary (asin TEXT,marketplace TEXT);
        CREATE TABLE review_record (asin TEXT,marketplace TEXT);
        """)
        conn.execute(
            "INSERT INTO collection_evidence VALUES(1,'run-1','B000000001','US','https://www.amazon.com/dp/B000000001',200,123,'2026-01-01T00:00:00+00:00','http_html',NULL,NULL,NULL,?)",
            ('{"postal_code":"90001","traffic":{"http_compressed_response_bytes":123}}',),
        )
        conn.execute(
            "INSERT INTO collection_evidence VALUES(2,'run-1','B000000002','US','https://www.amazon.com/dp/B000000002',200,NULL,'2026-01-01T00:00:01+00:00','selenium_dom',NULL,NULL,NULL,?)",
            ('{"postal_code":"90001","fallback_reason":"context_mismatch","traffic":{"http_compressed_response_bytes":77,"firefox_main_document_bytes":null,"firefox_subresource_bytes":456,"firefox_main_document_unknown_count":1,"firefox_subresource_unknown_count":0}}',),
        )
        conn.commit()
        conn.close()

        result = metrics.build_report(db, run_id="run-1")

        assert result["traffic"]["http_compressed_response"] == {
            "bytes": 200, "known_records": 2, "unknown_records": 0
        }
        assert result["traffic"]["firefox_main_document"] == {
            "bytes": None, "known_records": 0, "unknown_records": 1
        }
        assert result["traffic"]["firefox_subresources"] == {
            "bytes": 456, "known_records": 1, "unknown_records": 0
        }
        assert result["traffic"]["proxy_dashboard_bill"] == {
            "bytes": None, "known_records": 0, "unknown_records": 1
        }
        assert result["transfer_bytes_total"] == 200
        assert result["transfer_bytes_missing_count"] == 0
