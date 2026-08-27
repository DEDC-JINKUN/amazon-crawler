from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "amazon_us_egress.py"


def load_egress():
    spec = importlib.util.spec_from_file_location("amazon_us_egress_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class EgressPoolTests(unittest.TestCase):
    def _pool(self):
        egress = load_egress()
        clock = lambda: 100.0
        first = egress.ApprovedEgress("primary", "http://10.0.0.1:8080", _clock=clock)
        backup = egress.ApprovedEgress("backup", "http://10.0.0.2:8080", _clock=clock)
        return egress, first, backup, egress.ApprovedEgressPool([first, backup])

    def test_keeps_healthy_current_egress(self):
        _, first, _, pool = self._pool()
        self.assertIs(pool.choose("primary"), first)
        self.assertEqual(pool.failovers_used, 0)

    def test_allows_only_one_failover_per_run(self):
        _, first, backup, pool = self._pool()
        pool.mark_blocked("primary", 60)
        self.assertIs(pool.choose("primary"), backup)
        backup.mark_blocked(60)
        self.assertIsNone(pool.choose("backup"))
        pool.start_run()
        self.assertIsNone(pool.choose("backup"))

    def test_rejects_embedded_proxy_credentials(self):
        egress = load_egress()
        with self.assertRaises(ValueError):
            egress.ApprovedEgress("bad", "http://user:pass@10.0.0.1:8080")

    def test_summary_does_not_expose_proxy_url(self):
        _, _, _, pool = self._pool()
        self.assertNotIn("proxy_url", pool.summary()[0])


if __name__ == "__main__":
    unittest.main()
