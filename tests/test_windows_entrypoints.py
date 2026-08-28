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
    for name in ("run_once_windows.bat", "run_scheduled_windows.bat"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "--backend postgres" in text
        assert "AMAZON_US_POSTGRES_DSN" in text
        assert "state\\amazon_us.sqlite3" not in text


def test_windows_entrypoints_keep_distinct_error_stages():
    for name in ("run_once_windows.bat", "run_scheduled_windows.bat"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "WORKER_EXIT" in text
        assert "VERIFY_EXIT" in text
    assert "VERIFY_EXIT" in (ROOT / "verify_windows.bat").read_text(encoding="utf-8")


def test_windows_verification_reads_postgres_dsn_from_environment():
    text = (ROOT / "verify_windows.bat").read_text(encoding="utf-8")
    assert "verify_postgres.py" in text
    assert "--dsn-env AMAZON_US_POSTGRES_DSN" in text
    assert "--tenant-id amazon_us_local" in text


def test_replay_postgres_exposes_tenant_id():
    text = (ROOT / "scripts" / "replay_postgres.ps1").read_text(encoding="utf-8")
    assert "$TenantId = \"default\"" in text
    assert "--tenant-id $TenantId" in text
