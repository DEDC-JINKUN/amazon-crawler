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
        raw_path = raw / "US" / "A1" / "page.html"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_text("x" * 12, encoding="utf-8")
        conn = sqlite3.connect(db)
        conn.executescript("""
        CREATE TABLE collection_evidence (id INTEGER PRIMARY KEY,run_id TEXT,asin TEXT,marketplace TEXT,http_status INTEGER,retrieved_at TEXT,source_type TEXT,error_code TEXT,block_reason TEXT,raw_html_path TEXT);
        CREATE TABLE item_state (asin TEXT,marketplace TEXT,status TEXT);
        CREATE TABLE product_snapshot (asin TEXT,marketplace TEXT);
        CREATE TABLE media_asset (asin TEXT,marketplace TEXT);
        CREATE TABLE content_module (asin TEXT,marketplace TEXT);
        CREATE TABLE review_summary (asin TEXT,marketplace TEXT);
        CREATE TABLE review_record (asin TEXT,marketplace TEXT);
        """)
        conn.execute("INSERT INTO collection_evidence VALUES(1,'run-1','A1','US',200,'2026-01-01T00:00:00+00:00','http_html',NULL,NULL,'US/A1/page.html')")
        conn.execute("INSERT INTO item_state VALUES('A1','US','reviews_pending')")
        conn.execute("INSERT INTO product_snapshot VALUES('A1','US')")
        conn.commit(); conn.close()
        result = metrics.build_report(db, raw, "run-1")
        assert result["success_page_count"] == 1
        assert result["task_status_counts"] == {"reviews_pending": 1}
        assert result["bytes_total"] == 12
        assert result["table_counts"]["product_snapshot"] == 1
