import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "crawler.ps1"
HOST = ROOT / "scripts" / "crawler_process_host.py"


def test_control_script_exposes_small_safe_command_surface():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "ValidateSet('probe', 'run', 'status', 'console', 'stop', 'help')" in text
    assert "Read-Host" in text and "-AsSecureString" in text
    assert "--product-only" in text
    assert "--run-id" in text
    assert "--probe-egress" not in text
    assert "include-blocked" not in text.lower()


def test_control_script_has_locks_logs_receipts_and_safe_stop():
    text = SCRIPT.read_text(encoding="utf-8")
    for expected in (
        ".worker.lock.json",
        ".console.lock.json",
        "worker.stdout.log",
        "worker.stderr.log",
        "receipt.json",
        "StartTime",
        "RedirectStandardOutput",
        "RedirectStandardError",
        "stop -All",
        "System.Threading.Mutex",
        "crawler_process_host.py",
        "Stop-Process",
        "/readyz",
        "preflight.log",
        "Guid]::NewGuid",
        "runtime_fingerprint",
        "Get-ConsoleFingerprint",
        "Get-FinalRunSnapshot",
        "Test-ProbeRunQuality",
        "quality_gate_ok",
        "quality_failed",
        "recorded_actions",
        "completed_actions",
        "failed_actions",
        "blocked_actions",
        "inferred_actions",
        "run_verification_reason",
        "Final: {0}/{1}",
        "unmanaged listener remains",
        "Get-NetTCPConnection",
    ):
        assert expected in text
    assert "PGPASSWORD" in text
    assert "Remove-Item Env:PGPASSWORD" in text
    assert "123456" not in text


def test_console_reuse_requires_verified_lock_and_matching_runtime_fingerprint():
    text = SCRIPT.read_text(encoding="utf-8")
    ensure = text[text.index("function Ensure-Console"):text.index("function Show-Status")]
    ready = text[text.index("function Get-ConsoleReady"):text.index("function Ensure-Console")]
    assert ensure.index("Remove-StaleLock") < ensure.index("Get-ConsoleReady")
    assert "Get-VerifiedProcess" in ready
    assert "runtime_fingerprint" in ready
    assert "Port is occupied by an unmanaged or stale Console" in ensure
    report = text[text.index("function Report-UnmanagedConsoleListener"):text.index("function Show-Help")]
    assert "Get-NetTCPConnection" in report
    assert "Stop-Process" not in report


def test_probe_quality_function_requires_complete_successful_evidence():
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    assert powershell is not None
    script_path = str(SCRIPT).replace("'", "''")
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script_path}', [ref]$tokens, [ref]$errors)
$fn = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-ProbeRunQuality' }}, $true) | Select-Object -First 1
if ($null -eq $fn) {{ throw 'Test-ProbeRunQuality not found' }}
Invoke-Expression $fn.Extent.Text
$completed = @(
  [pscustomobject]@{{outcome='completed'; attribution='evidence'}},
  [pscustomobject]@{{outcome='completed'; attribution='evidence'}},
  [pscustomobject]@{{outcome='completed'; attribution='evidence'}}
)
$failed = @(
  [pscustomobject]@{{outcome='failed'; attribution='evidence'}},
  [pscustomobject]@{{outcome='failed'; attribution='evidence'}},
  [pscustomobject]@{{outcome='failed'; attribution='evidence'}}
)
$good = Test-ProbeRunQuality ([pscustomobject]@{{recorded_actions=3;inferred_actions=0;items=$completed;traffic=@{{}}}}) 3
$bad = Test-ProbeRunQuality ([pscustomobject]@{{recorded_actions=3;inferred_actions=0;items=$failed;traffic=@{{}}}}) 3
$partial = Test-ProbeRunQuality ([pscustomobject]@{{recorded_actions=2;inferred_actions=0;items=$completed[0..1];traffic=@{{}}}}) 3
[ordered]@{{good=$good;bad=$bad;partial=$partial}} | ConvertTo-Json -Depth 8 -Compress
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["good"]["quality_gate_ok"] is True
    assert payload["good"]["recorded_actions"] == 3
    assert payload["good"]["completed_actions"] == 3
    assert payload["bad"]["quality_gate_ok"] is False
    assert payload["bad"]["failed_actions"] == 3
    assert payload["partial"]["quality_gate_ok"] is False
    assert "recorded_actions" in payload["partial"]["quality_gate_reason"]


def test_verified_process_accepts_json_datetime_and_iso_but_rejects_unsafe_locks():
    powershell = shutil.which("pwsh") or shutil.which("pwsh.exe") or shutil.which("powershell") or shutil.which("powershell.exe")
    assert powershell is not None
    script_path = str(SCRIPT).replace("'", "''")
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script_path}', [ref]$tokens, [ref]$errors)
$fn = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-VerifiedProcess' }}, $true) | Select-Object -First 1
if ($null -eq $fn) {{ throw 'Get-VerifiedProcess not found' }}
Invoke-Expression $fn.Extent.Text
$process = Get-Process -Id $PID
$iso = $process.StartTime.ToUniversalTime().ToString('o')
$jsonLock = ('{{{{"pid":{{0}},"start_time":"{{1}}"}}}}' -f $PID,$iso) | ConvertFrom-Json
$stringLock = [pscustomobject]@{{pid=$PID;start_time=[string]$iso}}
$invalidLock = [pscustomobject]@{{pid=$PID;start_time='not-a-time'}}
$mismatchLock = [pscustomobject]@{{pid=$PID;start_time=$process.StartTime.ToUniversalTime().AddTicks(1).ToString('o')}}
$missingProcessLock = [pscustomobject]@{{pid=2147483647;start_time=$iso}}
[ordered]@{{
  json_type=$jsonLock.start_time.GetType().FullName
  json_ok=$null -ne (Get-VerifiedProcess $jsonLock)
  string_ok=$null -ne (Get-VerifiedProcess $stringLock)
  invalid_rejected=$null -eq (Get-VerifiedProcess $invalidLock)
  mismatch_rejected=$null -eq (Get-VerifiedProcess $mismatchLock)
  missing_process_rejected=$null -eq (Get-VerifiedProcess $missingProcessLock)
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {
        "json_type": "System.DateTime",
        "json_ok": True,
        "string_ok": True,
        "invalid_rejected": True,
        "mismatch_rejected": True,
        "missing_process_rejected": True,
    }


def test_worker_and_console_locks_share_the_same_verified_process_gate():
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.count("Get-VerifiedProcess") >= 5
    assert "Get-VerifiedProcess $worker" in text
    assert "Get-VerifiedProcess $console" in text


def test_verified_process_fails_closed_when_start_time_access_throws():
    powershell = shutil.which("pwsh") or shutil.which("pwsh.exe") or shutil.which("powershell") or shutil.which("powershell.exe")
    assert powershell is not None
    script_path = str(SCRIPT).replace("'", "''")
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script_path}', [ref]$tokens, [ref]$errors)
$fn = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-VerifiedProcess' }}, $true) | Select-Object -First 1
if ($null -eq $fn) {{ throw 'Get-VerifiedProcess not found' }}
Invoke-Expression $fn.Extent.Text
$throwingProcess = [pscustomobject]@{{}}
$throwingProcess | Add-Member -MemberType ScriptProperty -Name StartTime -Value {{ throw 'start time unavailable' }}
function Get-Process {{ param([int]$Id, $ErrorAction) return $throwingProcess }}
$lock = [pscustomobject]@{{pid=123;start_time='2026-08-31T04:52:15.7663775Z'}}
$result = Get-VerifiedProcess $lock
[ordered]@{{rejected=$null -eq $result}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"rejected": True}


def test_control_script_defaults_to_current_isolated_tenant():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "real_batch_20260828_500_04" in text
    assert "data\\postgres_real_batch_20260828_500_04\\manifest_500.csv" in text
    assert "data\\postgres_real_batch_20260828_500_04\\batch500.toml" in text


def test_worker_host_waits_for_registration_gate_and_uses_kill_on_close_job():
    text = HOST.read_text(encoding="utf-8")
    assert "gate_path" in text
    assert "cancel_path" in text
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in text
    assert "AssignProcessToJobObject" in text
    assert "return process.wait()" in text
