from pathlib import Path
import importlib.util
import sqlite3
import tempfile


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("evidence_health", ROOT / "scripts" / "evidence_health.py")
health = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(health)


def test_audit_detects_hash_mismatch_and_missing_context():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db = root / "state.sqlite3"
        raw = root / "raw"; (raw / "US/A1").mkdir(parents=True)
        (raw / "US/A1/page.html").write_text("changed", encoding="utf-8")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE collection_evidence (id INTEGER PRIMARY KEY,asin TEXT,raw_html_path TEXT,content_hash TEXT,context_json TEXT)")
        conn.execute("INSERT INTO collection_evidence VALUES(1,'A1','US/A1/page.html','bad','{}')")
        conn.commit(); conn.close()
        result = health.audit(db, raw)
        assert result["hash_mismatch_count"] == 1
        assert result["context_missing_count"] == 1
        assert result["ok"] is False


def test_worker_evidence_and_local_html_pass_end_to_end_audit():
    worker_spec = importlib.util.spec_from_file_location("worker", ROOT / "scripts" / "amazon_us_worker.py")
    worker = importlib.util.module_from_spec(worker_spec)
    assert worker_spec.loader is not None
    worker_spec.loader.exec_module(worker)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        db = root / "state.sqlite3"
        manifest = root / "manifest.csv"
        manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
        conn = worker.init_db(db)
        worker.initialize_manifest(conn, manifest, worker.DEFAULTS)
        row = conn.execute("SELECT * FROM item_state").fetchone()
        worker._set_status(conn, "US", row["asin"], "running", reason="test")
        body = "<html>byte-stable</html>"
        worker._insert_evidence(conn, "run-1", row["asin"], row["url"], 200, body, None, source_type="http_html", raw_html_dir=root / "raw", context={"postal_code": "90001"})
        conn.commit(); conn.close()
        result = health.audit(db, root / "raw")
        assert result["ok"] is True
        assert result["raw_html_missing_count"] == 0
        assert result["hash_mismatch_count"] == 0
