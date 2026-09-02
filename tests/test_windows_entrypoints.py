from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONFIGURE = ROOT / "configure_owned_full.ps1"


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
    """), encoding="utf-8")
    child = tmp_path / "child.ps1"
    marker = tmp_path / "child.ok"
    child.write_text(
        f"if ($env:AMAZON_PROXY_USER -ne 'fake-user') {{ exit 9 }}; "
        f"[IO.File]::WriteAllText('{str(marker).replace(chr(39), chr(39) * 2)}','child-ok')",
        encoding="utf-8",
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
    """), encoding="utf-8")

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


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI runtime test")
def test_dpapi_envelope_verifies_and_launches_for_calling_windows_user(request):
    tmp_path = Path(tempfile.mkdtemp(prefix="amazon-configure-"))
    request.addfinalizer(lambda: shutil.rmtree(tmp_path, ignore_errors=True))
    powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    vault = tmp_path / "amazon_us.secrets.dpapi"
    configure = tmp_path / "configure_owned_full.ps1"
    launcher = tmp_path / "secure_dpapi_launcher.ps1"
    shutil.copy2(CONFIGURE, configure)
    shutil.copy2(ROOT / "scripts" / "secure_dpapi_launcher.ps1", launcher)

    configure_env = os.environ.copy()
    configure_env.update({
        "AMAZON_PROXY_USER": "fake-login__cr.us",
        "AMAZON_PROXY_PASS": "fake-proxy-pass",
        "AMAZON_US_POSTGRES_DSN": "host=127.0.0.1 password=fake-pg-pass",
        "AMAZON_COLLECTION_API_KEY": "fake-api-key",
    })
    configured = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", configure,
         "-Mode", "Configure", "-SecretPath", vault],
        env=configure_env,
        capture_output=True,
    )
    assert configured.returncode == 0, configured.stderr.decode("utf-8", errors="replace")
    assert vault.exists(), configured.stdout.decode("utf-8", errors="replace")
    envelope = json.loads(vault.read_text(encoding="ascii"))
    assert envelope["schema"] == "amazon-us-dpapi-envelope-v1"
    assert envelope["scope"] == "CurrentUser"
    assert re.fullmatch(r"S-\d(?:-\d+)+", envelope["owner_sid"])
    assert envelope["ciphertext"]
    combined = configured.stdout + configured.stderr
    for secret in (b"fake-login", b"fake-proxy-pass", b"fake-pg-pass", b"fake-api-key"):
        assert secret not in combined
    marker = tmp_path / "configured.ok"
    child = tmp_path / "child.ps1"
    child.write_text(textwrap.dedent(rf"""
        if ($env:AMAZON_PROXY_USER -ne 'fake-login__cr.us') {{ exit 11 }}
        if ($env:AMAZON_PROXY_PASS -ne 'fake-proxy-pass') {{ exit 12 }}
        if ($env:AMAZON_US_POSTGRES_DSN -notlike '*password=fake-pg-pass') {{ exit 13 }}
        if ([string]::IsNullOrWhiteSpace($env:AMAZON_COLLECTION_API_KEY)) {{ exit 14 }}
        [IO.File]::WriteAllText('{str(marker).replace("'", "''")}','ok')
    """), encoding="utf-8")
    launch_wrapper = tmp_path / "launch.ps1"
    launch_wrapper.write_text(textwrap.dedent(rf"""
        & '{str(launcher).replace("'", "''")}' `
          -SecretPath '{str(vault).replace("'", "''")}' `
          -FilePath '{str(powershell).replace("'", "''")}' `
          -ArgumentList @('-NoProfile','-File','{str(child).replace("'", "''")}')
        exit $LASTEXITCODE
    """), encoding="utf-8")
    launched = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", launch_wrapper],
        capture_output=True,
    )
    assert launched.returncode == 0, launched.stderr.decode("utf-8", errors="replace")
    assert marker.read_text(encoding="utf-8") == "ok"

    verified = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", configure,
         "-Mode", "Verify", "-SecretPath", vault],
        capture_output=True,
    )
    assert verified.returncode == 0, verified.stderr.decode("utf-8", errors="replace")
    assert b"verified for the current Windows user" in verified.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI runtime test")
def test_secure_launcher_rejects_a_vault_owned_by_another_windows_sid(request):
    tmp_path = Path(tempfile.mkdtemp(prefix="amazon-dpapi-owner-"))
    request.addfinalizer(lambda: shutil.rmtree(tmp_path, ignore_errors=True))
    powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    vault = tmp_path / "amazon_us.secrets.dpapi"
    vault.write_text(json.dumps({
        "schema": "amazon-us-dpapi-envelope-v1",
        "scope": "CurrentUser",
        "owner_sid": "S-1-0-0",
        "ciphertext": "not-read-because-owner-does-not-match",
    }), encoding="ascii")

    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         ROOT / "scripts" / "secure_dpapi_launcher.ps1", "-SecretPath", vault,
         "-SecretNames", "AMAZON_PROXY_USER", "-FilePath", powershell,
         "-ArgumentList", "-NoProfile,-Command,exit 0"],
        capture_output=True,
    )
    assert result.returncode == 2
    assert b"secure launcher failed at vault-owner-mismatch" in result.stderr


def test_configure_owned_full_uses_current_user_dpapi_and_secure_prompts():
    configure = CONFIGURE.read_text(encoding="utf-8")
    assert "Read-Host $Prompt -AsSecureString" in configure
    assert "DataProtectionScope]::CurrentUser" in configure
    assert "WindowsIdentity]::GetCurrent().User.Value" in configure
    assert "amazon-us-dpapi-envelope-v1" in configure


def test_owned_full_secure_wrapper_uses_dpapi_and_fixed_tenant():
    wrapper = (ROOT / "run_owned_full_secure.ps1").read_text(encoding="utf-8")
    assert "secure_dpapi_launcher.ps1" in wrapper
    assert "owned_us_asin_20260902_full_01" in wrapper
    assert "manifest_1093.csv" in wrapper
    assert "owned_us_full.toml" in wrapper
    assert "ConfirmLargeBatch" in wrapper
    assert "'configure','verify-secrets','rotate'" in wrapper
    assert "configure_owned_full.ps1" in wrapper
    assert "[int]$Port = 8770" in wrapper
