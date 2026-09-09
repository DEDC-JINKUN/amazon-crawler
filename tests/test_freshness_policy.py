from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("freshness_policy_test", ROOT / "scripts" / "freshness_policy.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FreshnessPolicyTests(unittest.TestCase):
    def test_price_expires_before_content(self):
        policy = load().FreshnessPolicy()
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        captured = "2026-01-01T00:00:00+00:00"
        result = policy.evaluate(captured, ["price", "content"], now)
        self.assertTrue(result["stale"])
        self.assertEqual(result["stale_groups"], ["price"])

    def test_custom_ttl_and_unknown_group(self):
        module = load()
        policy = module.FreshnessPolicy.from_mapping({"price": 60, "default_ttl_seconds": 120})
        now = datetime(2026, 1, 1, 0, 1, 59, tzinfo=timezone.utc)
        result = policy.evaluate("2026-01-01T00:00:00+00:00", ["price", "custom"], now)
        self.assertEqual(result["age_seconds"], 119)
        self.assertEqual(result["stale_groups"], ["price"])

    def test_missing_or_invalid_timestamp_is_stale(self):
        policy = load().FreshnessPolicy()
        self.assertTrue(policy.evaluate(None, ["price"]) ["stale"])
        self.assertIsNone(policy.age_seconds("not-a-timestamp"))


if __name__ == "__main__":
    unittest.main()
