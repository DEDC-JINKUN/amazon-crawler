from __future__ import annotations

import importlib.util
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "amazon_us_throttle.py"


def load_throttle():
    spec = importlib.util.spec_from_file_location("amazon_us_throttle_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ThrottleTests(unittest.TestCase):
    def test_disabled_bucket_does_not_sleep(self):
        throttle = load_throttle()
        sleeps: list[float] = []
        bucket = throttle.TokenBucket(0, sleeper=sleeps.append)
        self.assertEqual(bucket.acquire(), 0.0)
        self.assertEqual(sleeps, [])

    def test_bucket_waits_for_the_next_token(self):
        throttle = load_throttle()
        state = {"now": 0.0}
        sleeps: list[float] = []

        def clock():
            return state["now"]

        def sleeper(delay):
            sleeps.append(delay)
            state["now"] += delay

        bucket = throttle.TokenBucket(2, capacity=1, clock=clock, sleeper=sleeper)
        self.assertEqual(bucket.acquire(), 0.0)
        self.assertAlmostEqual(bucket.acquire(), 0.5)
        self.assertEqual(sleeps, [0.5])

    def test_worker_pool_preserves_input_order(self):
        throttle = load_throttle()
        pool = throttle.WorkerPool(2)
        values = pool.map([1, 2, 3, 4], lambda value: (time.sleep(0.01), value * value)[1])
        self.assertEqual(values, [1, 4, 9, 16])

    def test_worker_pool_rejects_zero_workers(self):
        throttle = load_throttle()
        with self.assertRaises(ValueError):
            throttle.WorkerPool(0)

    def test_jitter_adds_random_extra_delay_within_bounds(self):
        throttle = load_throttle()
        state = {"now": 0.0}
        sleeps: list[float] = []

        def clock():
            return state["now"]

        def sleeper(delay):
            sleeps.append(delay)
            state["now"] += delay

        rng = throttle.random.Random(42)
        limiter = throttle.EgressLimiter(
            1.0, 0.0, 1, jitter_fraction=0.15, clock=clock, sleeper=sleeper, rng=rng
        )
        # 第一次 acquire：桶有存量令牌不等待，但 jitter 仍追加一次随机延迟
        first = limiter.acquire("direct")
        self.assertGreaterEqual(first, 0.0)
        self.assertLessEqual(first, 0.15)
        # 第二次 acquire：桶等待（基础间隔 - 第一次jitter恢复的令牌）+ jitter
        second = limiter.acquire("direct")
        self.assertGreaterEqual(second, 1.0 - 0.15)
        self.assertLessEqual(second, 1.0 + 0.15)
        self.assertGreaterEqual(sleeps[-1], 0.0)
        self.assertLessEqual(sleeps[-1], 0.15)

    def test_zero_jitter_keeps_original_behavior(self):
        throttle = load_throttle()
        state = {"now": 0.0}
        sleeps: list[float] = []

        def clock():
            return state["now"]

        def sleeper(delay):
            sleeps.append(delay)
            state["now"] += delay

        limiter = throttle.EgressLimiter(1.0, 0.0, 1, jitter_fraction=0.0, clock=clock, sleeper=sleeper)
        limiter.acquire("direct")
        waited = limiter.acquire("direct")
        self.assertAlmostEqual(waited, 1.0)
        self.assertEqual(sleeps, [1.0])

    def test_jitter_ignored_when_rates_disabled(self):
        throttle = load_throttle()
        sleeps: list[float] = []
        limiter = throttle.EgressLimiter(0.0, 0.0, 1, jitter_fraction=0.5, sleeper=sleeps.append)
        self.assertEqual(limiter.acquire("direct"), 0.0)
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
