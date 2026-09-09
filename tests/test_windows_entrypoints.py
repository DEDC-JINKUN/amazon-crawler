from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest


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


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI runtime test")
def test_secure_dpapi_launcher_works_under_windows_powershell(request):
    tmp_path = Path(tempfile.mkdtemp(prefix="amazon-dpapi-"))
    request.addfinalizer(lambda: shutil.rmtree(tmp_path, ignore_errors=True))
    powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    vault = tmp_path / "test.secrets.dpapi"
    create = tmp_path / "create.ps1"
    create.write_text(textwrap.dedent(rf"""
        [void][System.Reflection.Assembly]::LoadWithPartialName('System.Security')
        $payload = @{{schema_version='amazon-us-secrets-v1';AMAZON_PROXY_USER='fake-user'}} | ConvertTo-Json -Compress
        $plain = [Text.Encoding]::UTF8.GetBytes($payload)
        $cipher = [System.Security.Cryptography.ProtectedData]::Protect(
            $plain, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
        [IO.File]::WriteAllText('{str(vault).replace("'", "''")}', [Convert]::ToBase64String($cipher))
    """), encoding="utf-8-sig")
    child = tmp_path / "child.ps1"
    marker = tmp_path / "child.ok"
    child.write_text(
        f"if ($env:AMAZON_PROXY_USER -ne 'fake-user') {{ exit 9 }}; "
        f"[IO.File]::WriteAllText('{str(marker).replace(chr(39), chr(39) * 2)}','child-ok')",
        encoding="utf-8-sig",
    )
    wrapper = tmp_path / "run.ps1"
    launcher = tmp_path / "secure_dpapi_launcher.ps1"
    shutil.copy2(ROOT / "scripts" / "secure_dpapi_launcher.ps1", launcher)
    wrapper.write_text(textwrap.dedent(rf"""
        & '{str(launcher).replace("'", "''")}' `
          -SecretPath '{str(vault).replace("'", "''")}' `
          -SecretNames @('AMAZON_PROXY_USER') `
          -FilePath '{str(powershell).replace("'", "''")}' `
          -ArgumentList @('-NoProfile','-File','{str(child).replace("'", "''")}')
        exit $LASTEXITCODE
    """), encoding="utf-8-sig")

    created = subprocess.run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", create], capture_output=True)
    assert created.returncode == 0, created.stderr.decode("utf-8", errors="replace")
    result = subprocess.run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", wrapper], capture_output=True)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert marker.exists(), {
        "stdout": result.stdout.decode("utf-8", errors="replace"),
        "stderr": result.stderr.decode("utf-8", errors="replace"),
    }
    assert marker.read_text(encoding="utf-8") == "child-ok"
    assert b"fake-user" not in result.stdout


def test_owned_full_secure_wrapper_uses_dpapi_and_fixed_tenant():
    wrapper = (ROOT / "run_owned_full_secure.ps1").read_text(encoding="utf-8")
    assert "secure_dpapi_launcher.ps1" in wrapper
    assert "owned_us_asin_20260902_full_01" in wrapper
    assert "manifest_1093.csv" in wrapper
    assert "owned_us_full.toml" in wrapper
    assert "ConfirmLargeBatch" in wrapper
    assert "ValidateSet('egress','probe','run','reviews','status','console','stop')" in wrapper
    assert "[int]$Port = 8770" in wrapper
