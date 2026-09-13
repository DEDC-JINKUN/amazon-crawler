from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("preflight_test", ROOT / "scripts" / "preflight.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PreflightTests(unittest.TestCase):
    def test_configured_geckodriver_path_is_resolved_from_project_root(self):
        preflight = load()
        relative_driver = Path("tools/geckodriver-v0.37.1/geckodriver.exe")
        self.assertTrue((ROOT / relative_driver).exists(), "repository geckodriver fixture is required")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            config = root / "config.toml"
            config.write_text(
                '[worker]\nagent_name="test-agent"\nuser_agent="Agent/test-agent"\n'
                f'geckodriver_path="{relative_driver.as_posix()}"\n'
                '[context]\nexpected_country="US"\nexpected_currency="USD"\npostal_code="90001"\n',
                encoding="utf-8",
            )
            previous = Path.cwd()
            try:
                os.chdir(root)
                result = preflight.run_preflight(
                    manifest, config, root / "state.sqlite3", require_live=True
                )
            finally:
                os.chdir(previous)
        check = next(item for item in result["checks"] if item["name"] == "geckodriver")
        self.assertTrue(check["ok"], check)
        self.assertEqual(Path(check["detail"]), ROOT / relative_driver)

    def test_proxy_validation_rejects_embedded_credentials(self):
        preflight = load()
        ok, detail = preflight._validate_proxy_url("http://user:secret@127.0.0.1:8080")
        self.assertFalse(ok)
        self.assertNotIn("secret", detail)

    def test_proxy_validation_accepts_explicit_http_proxy(self):
        preflight = load()
        self.assertEqual(preflight._validate_proxy_url("http://127.0.0.1:8080"), (True, "configured http proxy"))

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

    def test_live_us_preflight_allows_observation_without_fixed_postal_code(self):
        preflight = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            config = root / "config.toml"
            config.write_text('[worker]\nagent_name="test-agent"\nuser_agent="Agent/test-agent"\n[context]\nexpected_country="US"\nexpected_currency="USD"\npostal_code=""\n', encoding="utf-8")
            result = preflight.run_preflight(manifest, config, root / "state" / "state.sqlite3", require_live=True)
            check = next(item for item in result["checks"] if item["name"] == "us_postal_code")
            self.assertTrue(check["ok"])

    def test_live_us_preflight_rejects_invalid_postal_code(self):
        preflight = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            config = root / "config.toml"
            config.write_text('[worker]\nagent_name="test-agent"\nuser_agent="Agent/test-agent"\n[context]\nexpected_country="US"\nexpected_currency="USD"\npostal_code="123"\n', encoding="utf-8")
            result = preflight.run_preflight(manifest, config, root / "state" / "state.sqlite3", require_live=True)
            check = next(item for item in result["checks"] if item["name"] == "us_postal_code")
            self.assertFalse(check["ok"])

    def test_postgres_preflight_requires_dsn_and_checks_schema(self):
        preflight = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            config = root / "config.toml"
            config.write_text('[worker]\nagent_name="test-agent"\nuser_agent="Agent/test-agent"\n', encoding="utf-8")

            missing = preflight.run_preflight(manifest, config, root / "unused.sqlite3", backend="postgres")
            self.assertFalse(next(item for item in missing["checks"] if item["name"] == "postgres") ["ok"])

            ready = preflight.run_preflight(
                manifest, config, root / "unused.sqlite3", backend="postgres", dsn="postgresql://example",
                postgres_probe=lambda dsn: (True, "schema ready"),
            )
            self.assertTrue(next(item for item in ready["checks"] if item["name"] == "postgres") ["ok"])

    def test_explicit_egress_probe_is_a_preflight_gate(self):
        preflight = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            config = root / "config.toml"
            config.write_text('[worker]\nagent_name="test-agent"\nuser_agent="Agent/test-agent"\nproxy_url="http://127.0.0.1:8080"\n', encoding="utf-8")

            class Probe:
                @staticmethod
                def probe(*args, **kwargs):
                    return {"ok": True, "status": 200, "block_reason": None, "elapsed_ms": 1, "response_bytes": 2}

            with patch.object(preflight, "_load_egress_probe", return_value=Probe):
                result = preflight.run_preflight(manifest, config, root / "state.sqlite3", probe_egress=True)
            check = next(item for item in result["checks"] if item["name"] == "proxy_probe")
            self.assertTrue(check["ok"])

    def test_explicit_egress_probe_failure_blocks_preflight(self):
        preflight = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            config = root / "config.toml"
            config.write_text('[worker]\nagent_name="test-agent"\nuser_agent="Agent/test-agent"\nproxy_url="http://127.0.0.1:8080"\n', encoding="utf-8")

            class Probe:
                @staticmethod
                def probe(*args, **kwargs):
                    return {"ok": False, "status": 429, "block_reason": "http_429", "elapsed_ms": 1, "response_bytes": 2}

            with patch.object(preflight, "_load_egress_probe", return_value=Probe):
                result = preflight.run_preflight(manifest, config, root / "state.sqlite3", probe_egress=True)
            check = next(item for item in result["checks"] if item["name"] == "proxy_probe")
            self.assertFalse(check["ok"])
            self.assertFalse(result["ok"])

    def test_live_preflight_auto_probes_when_proxy_is_configured(self):
        preflight = load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.csv"
            manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
            config = root / "config.toml"
            config.write_text('[worker]\nagent_name="test-agent"\nuser_agent="Agent/test-agent"\nproxy_url="http://127.0.0.1:8080"\n', encoding="utf-8")

            class Probe:
                @staticmethod
                def probe(*args, **kwargs):
                    return {"ok": True, "status": 200, "block_reason": None, "elapsed_ms": 1, "response_bytes": 2}

            with patch.object(preflight, "_load_egress_probe", return_value=Probe):
                result = preflight.run_preflight(manifest, config, root / "state.sqlite3", require_live=True)
            check = next(item for item in result["checks"] if item["name"] == "proxy_probe")
            self.assertTrue(check["ok"])


if __name__ == "__main__":
    unittest.main()
