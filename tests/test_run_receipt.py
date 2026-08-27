from __future__ import annotations

import importlib.util
import json
import tempfile
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
            "failed": [],
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


def test_compose_receipt_lists_actionable_asins():
    receipt = load_module().compose_receipt(
        {"ok": True, "blocked": [{"asin": "B000000001", "reason": "captcha"}], "failed": [{"asin": "B000000002", "status": "failed"}], "exhausted_failed": ["B000000003"]},
        [], {}, None,
    )
    assert receipt["action_items"] == {
        "blocked_asins": ["B000000001"],
        "failed_asins": ["B000000002"],
        "exhausted_failed_asins": ["B000000003"],
    }


def test_compose_receipt_marks_verification_errors():
    receipt = load_module().compose_receipt({"ok": False}, ["bad evidence"], {}, None)
    assert receipt["verification"]["ok"] is False
    assert receipt["verification"]["errors"] == ["bad evidence"]


def test_write_atomic_replaces_target_without_leaving_temp_file():
    module = load_module()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "receipt.json"
        target.write_text("old", encoding="utf-8")
        module._write_atomic(target, "new")
        assert target.read_text(encoding="utf-8") == "new"
        assert not (root / ".receipt.json.tmp").exists()


def test_main_builds_receipt_from_fresh_offline_database(capsys):
    receipt = load_module()
    worker_spec = importlib.util.spec_from_file_location("run_receipt_worker_test", ROOT / "scripts" / "amazon_us_worker.py")
    worker = importlib.util.module_from_spec(worker_spec)
    assert worker_spec.loader is not None
    worker_spec.loader.exec_module(worker)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        manifest = root / "manifest.csv"
        manifest.write_text("asin,url,marketplace,source_site_label,source_workbook\nB00RCPDCQU,https://www.amazon.com/dp/B00RCPDCQU,US,test,fixture.csv\n", encoding="utf-8")
        db = root / "state.sqlite3"
        output = root / "output"
        conn = worker.init_db(db)
        worker.initialize_manifest(conn, manifest, worker.DEFAULTS)
        worker.materialize_csvs(conn, output)
        conn.close()
        result_path = output / "run_receipt.json"

        assert receipt.main([
            "--manifest", str(manifest), "--state", str(db), "--output-dir", str(output),
            "--raw-html-dir", str(root / "raw"), "--output", str(result_path),
        ]) == 0
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        assert payload["verification"]["ok"] is True
        assert payload["verification"]["collection_phase"] == "not_collected"
        assert payload["collection_metrics"]["run_id"] is None
        assert capsys.readouterr().out
