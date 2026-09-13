from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location(
        "action_timing_test", ROOT / "scripts" / "action_timing.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Clock:
    def __init__(self) -> None:
        self.value = 10.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_snapshot_reports_monotonic_stage_totals_without_sensitive_context():
    module = load_module()
    clock = Clock()
    timing = module.ActionTimingRecorder(clock=clock)

    with timing.measure("http_connect_headers"):
        clock.advance(0.125)
    with timing.measure("http_body"):
        clock.advance(0.375)
    with timing.measure("http_body"):
        clock.advance(0.5)

    snapshot = timing.snapshot()
    action_id = snapshot.pop("action_id")
    assert isinstance(action_id, str) and len(action_id) == 32
    assert snapshot == {
        "schema_version": "amazon-action-timing-v1",
        "clock": "monotonic",
        "measured_elapsed_ms": 1000.0,
        "stages": {
            "http_connect_headers": {"elapsed_ms": 125.0, "count": 1},
            "http_body": {"elapsed_ms": 875.0, "count": 2},
        },
        "overlapping_totals": True,
        "not_measured": ["storage"],
    }
    assert "url" not in repr(snapshot).lower()
    assert "credential" not in repr(snapshot).lower()


def test_measure_records_failed_stage_and_reset_starts_a_fresh_action():
    module = load_module()
    clock = Clock()
    timing = module.ActionTimingRecorder(clock=clock)

    with pytest.raises(RuntimeError):
        with timing.measure("firefox_navigation"):
            clock.advance(0.2)
            raise RuntimeError("fixture")

    assert timing.snapshot()["stages"]["firefox_navigation"] == {
        "elapsed_ms": 200.0,
        "count": 1,
    }
    previous_action_id = timing.snapshot()["action_id"]
    timing.reset()
    clock.advance(0.05)
    assert timing.snapshot()["action_id"] != previous_action_id
    assert timing.snapshot()["stages"] == {}
    assert timing.snapshot()["measured_elapsed_ms"] == 50.0


def test_unknown_stage_is_rejected_instead_of_creating_unbounded_evidence_keys():
    module = load_module()
    timing = module.ActionTimingRecorder()

    with pytest.raises(ValueError, match="unknown action timing stage"):
        with timing.measure("https://secret.example/path"):
            pass
