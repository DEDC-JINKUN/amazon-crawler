from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_entrypoints_reference_existing_python_scripts():
    entrypoints = ("run_once_windows.bat", "run_scheduled_windows.bat", "verify_windows.bat")
    for name in entrypoints:
        text = (ROOT / name).read_text(encoding="utf-8")
        scripts = re.findall(r"python\s+scripts\\([^\s]+\.py)", text, flags=re.IGNORECASE)
        assert scripts, name
        for script in scripts:
            assert (ROOT / "scripts" / script).exists(), f"{name} references missing scripts/{script}"
        assert "run_receipt.py" in text


def test_windows_entrypoints_keep_distinct_error_stages():
    for name in ("run_once_windows.bat", "run_scheduled_windows.bat", "verify_windows.bat"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "WORKER_EXIT" in text or "MATERIALIZE_EXIT" in text
        assert "VERIFY_EXIT" in text
        assert "RECEIPT_EXIT" in text


def test_replay_postgres_exposes_tenant_id():
    text = (ROOT / "scripts" / "replay_postgres.ps1").read_text(encoding="utf-8")
    assert "$TenantId = \"default\"" in text
    assert "--tenant-id $TenantId" in text
