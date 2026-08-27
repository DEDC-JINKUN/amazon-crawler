from __future__ import annotations

import importlib.util
import json
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
                self.assertTrue(payload["evidence"]["raw_html_path"])
                self.assertEqual(api.load_job_status(db)["counts"]["reviews_pending"], 1)

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


if __name__ == "__main__":
    unittest.main()
