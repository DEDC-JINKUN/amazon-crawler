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
