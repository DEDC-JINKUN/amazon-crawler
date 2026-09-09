from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "amazon_us_worker.py"


def load_worker():
    spec = importlib.util.spec_from_file_location("amazon_us_worker_pacing_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CaptchaGateTests(unittest.TestCase):
    def setUp(self):
        self.worker = load_worker()
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())

    def test_gate_disabled_without_explicit_path(self):
        self.assertIsNone(self.worker._captcha_gate_path({}))
        self.assertIsNone(self.worker._captcha_gate_path({"paths": {"state": "state/amazon_us.sqlite3"}}))
        path = self.tmp / "gate.json"
        self.assertEqual(self.worker._captcha_gate_path({"captcha_gate_path": str(path)}), path)

    def test_gate_file_resets_on_new_day(self):
        path = self.tmp / "gate.json"
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        path.write_text(
            json.dumps({"date": yesterday, "count": 5, "consecutive": 3, "paused_until": None}),
            encoding="utf-8",
        )
        gate = self.worker._load_captcha_gate(path)
        self.assertEqual(gate["count"], 0)
        self.assertEqual(gate["consecutive"], 0)

    def test_two_captcha_hits_trip_six_hour_pause(self):
        path = self.tmp / "gate.json"
        gate = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "count": 0, "consecutive": 0, "paused_until": None}
        gate, tripped = self.worker._record_captcha_gate_hit(path, gate, "captcha")
        self.assertFalse(tripped)
        gate, tripped = self.worker._record_captcha_gate_hit(path, gate, "robot")
        self.assertTrue(tripped)
        self.assertGreater(self.worker._captcha_gate_pause_remaining(gate), 5.9 * 3600)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["count"], 2)
        self.assertIsNotNone(saved["paused_until"])

    def test_non_captcha_failures_trip_after_consecutive_limit(self):
        path = self.tmp / "gate.json"
        gate = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "count": 0, "consecutive": 0, "paused_until": None}
        tripped = False
        for _ in range(self.worker.CAPTCHA_GATE_CONSECUTIVE_LIMIT):
            gate, tripped = self.worker._record_captcha_gate_hit(path, gate, "http_transport_error")
        self.assertTrue(tripped)
        self.assertEqual(gate["count"], 0)

    def test_expired_pause_reports_zero_remaining(self):
        gate = {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "count": 2,
            "consecutive": 2,
            "paused_until": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        }
        self.assertEqual(self.worker._captcha_gate_pause_remaining(gate), 0.0)

    def test_reset_streak_clears_consecutive_only(self):
        path = self.tmp / "gate.json"
        gate = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "count": 1, "consecutive": 3, "paused_until": None}
        self.worker._reset_captcha_gate_streak(path, gate)
        self.assertEqual(gate["consecutive"], 0)
        self.assertEqual(gate["count"], 1)


class _NoTaskStorage:
    """永远领不到任务的 storage：用于验证熔断门行为（睡完门后才尝试领取）。"""

    def __init__(self):
        self.claims = 0

    def claim_task(self, worker_id, lease_seconds=None):
        self.claims += 1
        return None


class _OneTaskThenNoneStorage:
    """只发一个任务然后领空的 storage。"""

    def __init__(self):
        self.claims = 0
        self.saved = []

    def claim_task(self, worker_id, lease_seconds=None):
        self.claims += 1
        if self.claims > 1:
            return None
        return {
            "asin": "B00RCPDCQU", "marketplace": "US",
            "url": "https://www.amazon.com/dp/B00RCPDCQU",
            "status": "running", "task_stage": "product",
            "lease_token": "token-1", "lease_owner": worker_id,
            "reported_review_count": 0, "fetched_review_count": 0, "review_pages_fetched": 0,
        }

    def save_failure(self, **payload):
        self.saved.append(payload)
        return True


class _CaptchaBlockAdapter:
    source_type = "http_html"
    last_transfer_bytes = 100
    last_retry_after_seconds = None

    def fetch(self, url):
        return "<html><body>Enter the characters you see below</body></html>", 403


class _SleepRecorder:
    def __init__(self, on_sleep=None):
        self.calls = []
        self.on_sleep = on_sleep

    def __call__(self, seconds):
        self.calls.append(seconds)
        if self.on_sleep:
            self.on_sleep(seconds)


def _gate_config(worker, gate_path, **overrides):
    config = dict(worker.DEFAULTS)
    config.update({
        "max_actions_per_run": 5,
        "raw_html_dir": None,
        "context": {},
        "captcha_gate_path": str(gate_path),
    })
    config.update(overrides)
    return config


class GatePauseBehaviorTests(unittest.TestCase):
    """熔断暂停期 worker 必须睡满后继续，而不是退出（退出曾引发协调器重启风暴）。"""

    def setUp(self):
        self.worker = load_worker()
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.gate_path = self.tmp / "gate.json"

    def _write_gate(self, **fields):
        payload = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                   "count": 0, "consecutive": 0, "paused_until": None}
        payload.update(fields)
        self.gate_path.write_text(json.dumps(payload), encoding="utf-8")

    def _patch_sleep(self, recorder):
        original = self.worker.time.sleep
        self.worker.time.sleep = recorder
        self.addCleanup(setattr, self.worker.time, "sleep", original)

    def test_paused_gate_sleeps_then_claims(self):
        self._write_gate(count=2, consecutive=2,
                         paused_until=(datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat())
        storage = _NoTaskStorage()
        recorder = _SleepRecorder()
        self._patch_sleep(recorder)

        result = self.worker.run_postgres_actions(
            storage, _CaptchaBlockAdapter(), _gate_config(self.worker, self.gate_path),
            limit=5, worker_id="w-gate",
        )

        self.assertEqual(result, 0)
        # 10 秒暂停 < 300 秒分块 → 单次睡完
        self.assertEqual(len(recorder.calls), 1)
        self.assertAlmostEqual(recorder.calls[0], 10.0, delta=2.0)
        # 睡醒后照常去领任务，而不是在门口退出
        self.assertEqual(storage.claims, 1)

    def test_gate_cleared_externally_wakes_early(self):
        self._write_gate(count=2, consecutive=2,
                         paused_until=(datetime.now(timezone.utc) + timedelta(hours=6)).isoformat())
        storage = _NoTaskStorage()

        def clear_gate(seconds):
            self._write_gate(paused_until=None)

        recorder = _SleepRecorder(on_sleep=clear_gate)
        self._patch_sleep(recorder)

        result = self.worker.run_postgres_actions(
            storage, _CaptchaBlockAdapter(), _gate_config(self.worker, self.gate_path),
            limit=5, worker_id="w-gate",
        )

        self.assertEqual(result, 0)
        # 第一个 300 秒块睡完时门已被外部清除 → 下一轮读到暂停结束，不再睡
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(storage.claims, 1)

    def test_midrun_trip_sleeps_and_continues(self):
        # 预置 1 次命中：本次 403 拦截后 count=2 → 触发熔断
        self._write_gate(count=1, consecutive=1)
        storage = _OneTaskThenNoneStorage()
        recorder = _SleepRecorder()
        self._patch_sleep(recorder)

        result = self.worker.run_postgres_actions(
            storage, _CaptchaBlockAdapter(), _gate_config(self.worker, self.gate_path),
            limit=5, worker_id="w-trip",
        )

        # 关键回归断言：熔断后睡满暂停期继续干活，不再 return -1
        self.assertEqual(result, 1)
        # 拦截落库 + 熔断已写入门文件
        self.assertEqual(storage.saved[0]["next_status"], "blocked")
        saved = json.loads(self.gate_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["count"], 2)
        self.assertIsNotNone(saved["paused_until"])
        # 6 小时暂停按 300 秒分块睡
        self.assertGreaterEqual(len(recorder.calls), 70)
        # 睡完继续领取（claims=2：1 个真任务 + 1 次领空退出）
        self.assertEqual(storage.claims, 2)

    def test_midrun_trip_gate_cleared_wakes_early(self):
        """运行中熔断睡满逻辑也要响应手工清门（换节点/代理场景）。

        2026-09-08 夜间实际事故：运行中触发的睡眠不重读门文件，
        清门后 worker 仍睡满 6 小时。
        """
        self._write_gate(count=1, consecutive=1)
        storage = _OneTaskThenNoneStorage()

        def clear_gate(seconds):
            # 第一个 300 秒块睡完时外部清门（模拟换节点后手工清门）
            self._write_gate(count=0, consecutive=0, paused_until=None)

        recorder = _SleepRecorder(on_sleep=clear_gate)
        self._patch_sleep(recorder)

        result = self.worker.run_postgres_actions(
            storage, _CaptchaBlockAdapter(), _gate_config(self.worker, self.gate_path),
            limit=5, worker_id="w-wake",
        )

        self.assertEqual(result, 1)
        # 只睡第一个 300 秒块就因清门提前唤醒，不睡满 6 小时
        self.assertEqual(len(recorder.calls), 1)
        # 提前唤醒后继续领取（claims=2：1 个真任务 + 1 次领空退出）
        self.assertEqual(storage.claims, 2)


if __name__ == "__main__":
    unittest.main()
