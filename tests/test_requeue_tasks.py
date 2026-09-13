from pathlib import Path
import importlib.util
import sqlite3
import tempfile


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("requeue_tasks", ROOT / "scripts" / "requeue_tasks.py")
requeue_tasks = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(requeue_tasks)


def _db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE item_state (marketplace TEXT, asin TEXT, status TEXT, next_review_url TEXT, attempts INTEGER, resume_status TEXT, task_stage TEXT, block_reason TEXT, last_error TEXT, updated_at TEXT);
    CREATE TABLE state_history (marketplace TEXT, asin TEXT, from_status TEXT, to_status TEXT, reason TEXT, changed_at TEXT);
    INSERT INTO item_state VALUES ('US','A1','failed',NULL,2,NULL,'product',NULL,'oops','now');
    INSERT INTO item_state VALUES ('US','A2','blocked','https://www.amazon.com/product-reviews/A2',0,'reviews_pending','reviews','captcha','captcha','now');
    """)
    conn.commit(); conn.close()


def test_requeue_failed_resets_attempts_and_preserves_history():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "state.sqlite3"
        _db(db)
        result = requeue_tasks.requeue(db, asin="A1", reason="operator_review")
        assert result["updated_count"] == 1
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT status,attempts,resume_status,task_stage,last_error FROM item_state WHERE asin='A1'").fetchone()
        history = conn.execute("SELECT from_status,to_status,reason FROM state_history").fetchone()
        conn.close()
        assert row == ("pending", 0, "pending", "product", None)
        assert history == ("failed", "pending", "manual_requeue:operator_review")


def test_blocked_requires_explicit_flag():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "state.sqlite3"
        _db(db)
        assert requeue_tasks.requeue(db, asin="A2", reason="captcha_cleared")["updated_count"] == 0
        assert requeue_tasks.requeue(db, asin="A2", reason="captcha_cleared", include_blocked=True)["updated_count"] == 1


def test_dry_run_does_not_change_blocked_task():
    with tempfile.TemporaryDirectory() as directory:
        db = Path(directory) / "state.sqlite3"
        _db(db)
        result = requeue_tasks.requeue(db, asin="A2", reason="review_only", include_blocked=True, dry_run=True)
        assert result["dry_run"] is True
        assert result["selected_count"] == 1
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT status FROM item_state WHERE asin='A2'").fetchone()[0] == "blocked"
        assert conn.execute("SELECT COUNT(*) FROM state_history").fetchone()[0] == 0
        conn.close()
