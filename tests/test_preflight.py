from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("preflight_test", ROOT / "scripts" / "preflight.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PreflightTests(unittest.TestCase):
    def test_non_live_preflight_allows_missing_browser(self):
        preflight = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            config = root / "config.toml"
            config.write_text('[worker]\nagent_name="test-agent"\nuser_agent="Agent/test-agent"\n', encoding="utf-8")
            result = preflight.run_preflight(manifest, config, root / "state" / "state.sqlite3")
            self.assertTrue(result["ok"])

    def test_live_preflight_rejects_missing_browser_when_unavailable(self):
        preflight = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            config = root / "config.toml"
            config.write_text('[worker]\nagent_name="test-agent"\nuser_agent="Agent/test-agent"\n', encoding="utf-8")
            result = preflight.run_preflight(manifest, config, root / "state" / "state.sqlite3", require_live=True)
            browser_checks = [item for item in result["checks"] if item["name"] in {"selenium", "firefox"}]
            if any(not item["ok"] for item in browser_checks):
                self.assertFalse(result["ok"])

    def test_live_us_preflight_requires_postal_code(self):
        preflight = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            config = root / "config.toml"
            config.write_text('[worker]\nagent_name="test-agent"\nuser_agent="Agent/test-agent"\n[context]\nexpected_country="US"\nexpected_currency="USD"\npostal_code=""\n', encoding="utf-8")
            result = preflight.run_preflight(manifest, config, root / "state" / "state.sqlite3", require_live=True)
            check = next(item for item in result["checks"] if item["name"] == "us_postal_code")
            self.assertFalse(check["ok"])


if __name__ == "__main__":
    unittest.main()
