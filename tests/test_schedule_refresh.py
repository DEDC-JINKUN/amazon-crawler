from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ScheduleRefreshTests(unittest.TestCase):
    def test_enqueues_only_stale_asins_and_deduplicates(self):
        worker = load("amazon_us_worker")
        scheduler = load("schedule_refresh")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "state.sqlite3"
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\nB000000001,https://www.amazon.com/dp/B000000001,US,test,fixture.csv\n", encoding="utf-8")
            conn = worker.init_db(db)
            worker.initialize_manifest(conn, manifest, worker.DEFAULTS)
            conn.execute("UPDATE item_state SET status='succeeded' WHERE asin='B00RCPDCQU'")
            conn.execute("UPDATE item_state SET status='succeeded' WHERE asin='B000000001'")
            conn.execute("INSERT INTO product_snapshot(marketplace,asin,collected_at,status) VALUES('US','B00RCPDCQU','2026-01-01T00:00:00+00:00','succeeded')")
            conn.execute("INSERT INTO product_snapshot(marketplace,asin,collected_at,status) VALUES('US','B000000001','2026-01-01T23:59:30+00:00','succeeded')")
            conn.commit()
            conn.close()
            now = datetime(2026, 1, 2, tzinfo=timezone.utc)
            requests = scheduler.enqueue_stale(db, ["price"], now=now)
            self.assertEqual([item["asin"] for item in requests], ["B00RCPDCQU"])
            self.assertEqual(scheduler.enqueue_stale(db, ["price"], now=now), [])


if __name__ == "__main__":
    unittest.main()
