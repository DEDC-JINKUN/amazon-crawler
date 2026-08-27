from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_receipt.py"


def load_module():
    spec = importlib.util.spec_from_file_location("run_receipt_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_compose_receipt_keeps_verification_and_metrics_separate():
    receipt = load_module().compose_receipt(
        {
            "ok": True,
            "phase": "collecting",
            "collection_phase": "collecting",
            "manifest": {"count": 10},
            "state": {"count": 10, "status_counts": {"succeeded": 9, "pending": 1}},
            "coverage": {"missing_state": 0, "extra_state": 0},
            "blocked": [],
            "exhausted_failed": [],
        },
        [],
        {"run_id": "run-1", "success_page_count": 9, "transfer_bytes_total": 1000},
        {"proxy_bill_measurement": None},
    )
    assert receipt["verification"]["ok"] is True
    assert receipt["verification"]["collection_phase"] == "collecting"
    assert receipt["collection_metrics"]["transfer_bytes_total"] == 1000
    assert receipt["cost"]["proxy_bill_measurement"] is None


def test_compose_receipt_marks_verification_errors():
    receipt = load_module().compose_receipt({"ok": False}, ["bad evidence"], {}, None)
    assert receipt["verification"]["ok"] is False
    assert receipt["verification"]["errors"] == ["bad evidence"]
