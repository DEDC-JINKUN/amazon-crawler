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
        scripts = re.findall(r"(?:python|\.venv\\Scripts\\python\.exe)\s+scripts\\([^\s]+\.py)", text, flags=re.IGNORECASE)
        assert scripts, name
        for script in scripts:
            assert (ROOT / "scripts" / script).exists(), f"{name} references missing scripts/{script}"
    for name in ("run_once_windows.bat", "run_scheduled_windows.bat"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "crawler.ps1" in text
        if name == "run_once_windows.bat":
            assert ".venv\\Scripts\\python.exe scripts\\verify_postgres.py" in text
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
        f"if ([string]::IsNullOrWhiteSpace($env:AMAZON_PROXY_CREDENTIAL_GENERATION)) {{ exit 10 }}; "
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
    assert re.fullmatch(r"[a-f0-9]{32}", envelope["credential_generation"])
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
        if ($env:AMAZON_PROXY_CREDENTIAL_GENERATION -ne '{envelope["credential_generation"]}') {{ exit 15 }}
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

    rotated = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", configure,
         "-Mode", "Rotate", "-SecretPath", vault],
        env=configure_env,
        capture_output=True,
    )
    assert rotated.returncode == 0, rotated.stderr.decode("utf-8", errors="replace")
    rotated_envelope = json.loads(vault.read_text(encoding="ascii"))
    assert rotated_envelope["credential_generation"] != envelope["credential_generation"]


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


def test_owned_full_wrapper_exposes_agent_service_lifecycle_and_scoped_calls():
    wrapper = (ROOT / "run_owned_full_secure.ps1").read_text(encoding="utf-8")
    control = (ROOT / "scripts" / "agent_service_control.ps1").read_text(encoding="utf-8")

    for mode in ("agent-service", "agent-status", "agent-health", "agent-stop", "agent-get", "agent-batch", "agent-refresh", "agent-job"):
        assert f"'{mode}'" in wrapper
    assert "-AgentId" in wrapper
    assert "refresh-agent" in wrapper
    assert "read-agent" in wrapper
    assert "agent_collection_service.py" in control
    assert "crawler_process_host.py" in control
    assert "agent_service_control" in control
    assert "runtime_fingerprint" in control
    assert "agent_service_unmanaged_listener" in control
    assert "$lockStart -is [DateTime]" in control
    assert "AMAZON_US_POSTGRES_DSN" not in control
    assert "AMAZON_COLLECTION_API_KEY" not in control


def test_agent_business_commands_ensure_the_local_service_before_calling_client():
    wrapper = (ROOT / "run_owned_full_secure.ps1").read_text(encoding="utf-8")
    client_block = wrapper.split("if ($Mode -in @('agent-get','agent-batch','agent-refresh','agent-job'))", 1)[1]
    client_block = client_block.split("$arguments = [Collections.Generic.List[string]]::new()", 1)[0]

    assert "$ensureArguments = Get-AgentControlArguments 'start'" in client_block
    assert "& $launcher -FilePath $shell -ArgumentList $ensureArguments" in client_block
    assert client_block.index("-ArgumentList $ensureArguments") < client_block.index("-AgentId $agentId")


def test_agent_service_runtime_fingerprint_covers_worker_pool_api_storage_and_config():
    control = (ROOT / "scripts" / "agent_service_control.ps1").read_text(encoding="utf-8")

    assert "function Get-AgentRuntimeFingerprint" in control
    for dependency in (
        "agent_collection_service.py",
        "amazon_us_worker.py",
        "proxy_session_pool.py",
        "proxy_canary.py",
        "proxy_capacity_gate.py",
        "collection_api.py",
        "collection_storage.py",
        "postgres_worker_storage.py",
        "$resolvedConfig",
    ):
        assert dependency in control
    assert control.count("Get-AgentRuntimeFingerprint") >= 3
    assert "Get-CredentialGeneration" in control
    assert "credential_generation" in control


def test_agent_service_start_replaces_only_a_verified_ready_stale_runtime():
    control = (ROOT / "scripts" / "agent_service_control.ps1").read_text(encoding="utf-8")
    start_block = control.split("if ($null -ne $hostProcess)", 1)[1]
    start_block = start_block.split("[IO.Directory]::CreateDirectory($controlDir)", 1)[0]

    assert "$live.Ok -and (Test-CurrentRuntime $lock)" in start_block
    assert "if ($live.Ok)" in start_block
    assert "Stop-Process -Id $hostProcess.Id -Force" in start_block
    assert "Wait-AgentOffline" in start_block
    assert "Remove-ControlFiles" in start_block
    assert "agent_service_lock_is_live_but_not_ready" in start_block


def test_agent_status_uses_liveness_so_reads_survive_a_blocked_refresh_worker():
    control = (ROOT / "scripts" / "agent_service_control.ps1").read_text(encoding="utf-8")
    health_block = control.split("if ($Mode -eq 'health')", 1)[1].split("if ($Mode -eq 'status')", 1)[0]
    status_block = control.split("if ($Mode -eq 'status')", 1)[1].split("if ($Mode -eq 'stop')", 1)[0]

    assert "function Get-Live" in control
    assert '"$url/healthz"' in control
    assert "Get-Ready" in health_block
    assert "Get-Live" in status_block
    assert "Get-Ready" not in status_block


def test_all_windows_production_entries_share_non_amazon_canary_and_preclaim_capacity_gate():
    controller = (ROOT / "crawler.ps1").read_text(encoding="utf-8")
    wrapper = (ROOT / "run_owned_full_secure.ps1").read_text(encoding="utf-8")
    run_once = (ROOT / "run_once_windows.bat").read_text(encoding="utf-8")
    scheduled = (ROOT / "run_scheduled_windows.bat").read_text(encoding="utf-8")

    assert "'canary'" in controller
    assert "proxy_canary.py" in controller
    assert "proxy_capacity_gate.py" in controller
    assert "--probe-target-url" in controller
    assert "api.ipify.org" in controller
    crawl = controller.split("function Start-Crawl", 1)[1].split("function Stop-Locked", 1)[0]
    assert crawl.index("Invoke-CapacityGate") < crawl.index("Start-RunLedger")
    assert "'canary'" in wrapper
    assert "amazon_us_worker.py --config" not in run_once
    assert "amazon_us_worker.py --config" not in scheduled
    assert "crawler.ps1" in run_once
    assert "crawler.ps1" in scheduled
