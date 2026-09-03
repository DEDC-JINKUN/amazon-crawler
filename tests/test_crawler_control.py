import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "crawler.ps1"
HOST = ROOT / "scripts" / "crawler_process_host.py"


def load_host():
    spec = importlib.util.spec_from_file_location("crawler_process_host_test", HOST)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_console():
    path = ROOT / "scripts" / "collection_console.py"
    spec = importlib.util.spec_from_file_location("collection_console_fingerprint_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(os.name != "nt", reason="Windows junction fingerprint contract")
def test_windows_junction_raw_fingerprint_matches_controller_lexical_path():
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    assert powershell is not None
    raw_link = ROOT / "data" / "owned_us_asin_20260902_full_01" / "raw_html"
    assert raw_link.exists()
    lexical = Path(os.path.abspath(raw_link))
    physical = raw_link.resolve()
    if os.path.normcase(str(lexical)) == os.path.normcase(str(physical)):
        pytest.skip("workspace raw path is not junction-backed")
    tenant = "owned_us_asin_20260902_full_01"
    console = load_console()
    expected = console.raw_root_fingerprint(tenant, lexical)
    server = console.ConsoleServer(
        ("127.0.0.1", 0), object(), raw_html_dir=lexical, raw_tenant_id=tenant,
    )
    try:
        assert server.raw_root_fingerprint == expected
    finally:
        server.server_close()
    script_path = str(SCRIPT).replace("'", "''")
    raw_value = str(lexical).replace("'", "''")
    command = rf"""
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('{script_path}',[ref]$tokens,[ref]$errors)
$fn=$ast.FindAll({{param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-RawRootFingerprint'}},$true) | Select-Object -First 1
Invoke-Expression $fn.Extent.Text
$TenantId='{tenant}'
$projectRoot='{str(ROOT).replace("'", "''")}'
$python='{str(ROOT / ".venv" / "Scripts" / "python.exe").replace("'", "''")}'
Get-RawRootFingerprint '{raw_value}'
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT, capture_output=True, text=True, timeout=20,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip().splitlines()[-1] == expected
    assert expected != console.raw_root_fingerprint(tenant, physical)


def test_control_script_exposes_small_safe_command_surface():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "ValidateSet('egress', 'canary', 'probe', 'run', 'reviews', 'status', 'console', 'stop', 'help')" in text
    assert "Read-Host" in text and "-AsSecureString" in text
    assert "--product-only" in text
    assert "--reviews-only" in text
    assert "--run-id" in text
    assert "--probe-egress" not in text
    assert "include-blocked" not in text.lower()
    assert "egress_operation.py" in text
    assert "operation_ledger.py" in text
    assert "proxy_canary.py" in text
    assert "proxy_capacity_gate.py" in text


def test_collection_operations_are_registered_before_console_and_preflight():
    text = SCRIPT.read_text(encoding="utf-8")
    body = text[text.index("function Start-Crawl"):text.index("function Stop-Locked")]
    assert body.index("Start-Operation") < body.index("Ensure-Console")
    assert body.index("Start-Operation") < body.index("& $python $preflightScript")
    assert "Mark-OperationPreflight" in body
    assert "Finish-Operation" in body


def test_controller_atomically_reserves_and_binds_capacity_before_collection_run():
    text = SCRIPT.read_text(encoding="utf-8")
    body = text[text.index("function Start-Crawl"):text.index("function Stop-Locked")]

    assert "--reserve" in text
    assert "Bind-OperationCapacity" in body
    assert body.index("Invoke-CapacityGate") < body.index("Bind-OperationCapacity") < body.index("Start-RunLedger")
    assert "--capacity-reservation-id" in body
    assert body.count("capacity_authorization = $capacityAuthorization") >= 2
    assert "Release-CapacityReservation" in body


def test_setup_failures_are_audited_before_paths_limits_and_locks():
    text = SCRIPT.read_text(encoding="utf-8")
    main = text[text.index("$exitCode = 0"):]
    assert main.index("Initialize-CrawlOperation $Command") < main.index("Resolve-ProjectPath $OutputDir")
    setup = text[text.index("function Initialize-CrawlOperation"):text.index("function Start-Crawl")]
    assert "Start-Operation" in setup
    assert "$script:PendingOperationStarted = $true" in setup
    assert 'Finish-Operation $script:PendingOperationId' in main


def test_interrupt_status_is_preserved_by_controller_and_operation_ledgers():
    text = SCRIPT.read_text(encoding="utf-8")
    body = text[text.index("function Start-Crawl"):text.index("function Stop-Locked")]
    assert "$workerExitCode -eq 130" in body
    assert "$outcome -eq 'interrupted'" in body
    assert "System.Management.Automation.PipelineStoppedException" in body
    assert "$failureStatus = if ($isInterrupted) { 'interrupted' } else { 'failed' }" in body


def test_egress_command_uses_official_audited_entry_without_collection_run():
    text = SCRIPT.read_text(encoding="utf-8")
    egress = text[text.index("function Start-EgressOperation"):text.index("function Start-Crawl")]
    assert "Start-Operation" in egress
    assert "$egressOperationScript" in egress
    assert "Finish-Operation" in egress
    assert "Start-RunLedger" not in egress
    assert "requested_actions" not in egress


def test_reviews_command_is_bounded_and_selects_only_review_stage():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "reviews limit must be between 1 and 3" in text
    assert "if ($Mode -eq 'reviews') { '--reviews-only' } else { '--product-only' }" in text


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
        "variant_redirect_actions",
        "failed_actions",
        "blocked_actions",
        "inferred_actions",
        "unrequested_actions",
        "run_verification_reason",
        "Final: {0}/{1}",
        "unmanaged listener remains",
        "Get-NetTCPConnection",
        "postgres_run_ledger.py",
        "backfill_identity_evidence.py",
        "owner_pid",
        "owner_start_time",
        "amazon-us-control-receipt-v3",
        "proxy_session_pool = if ($null -ne $finalSnapshot.run)",
    ):
        assert expected in text
    assert "PGPASSWORD" in text
    assert "Remove-Item Env:PGPASSWORD" in text
    assert "123456" not in text
    normal_finish = text[text.index("$receipt = [ordered]@{"):text.index('Write-Host "Worker finished:')]
    assert normal_finish.index("Finish-RunLedger") < normal_finish.index("Write-JsonAtomic $receipt")
    progress_loop = text[text.index("while (-not $process.HasExited)"):text.index("$workerExitCode =")]
    assert "Get-RunProjection $runId" in progress_loop


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
    assert "raw_root_fingerprint" in ready
    assert "lock.tenant_id" in ready
    assert "ready.raw_tenant_id" in ready
    assert "console_ready_raw_root_mismatch" in ready
    assert "Console did not become ready: $readyFailure" in ensure
    assert "console_process_exited" in ensure
    assert "Get-ControlledRawHtmlDir" in ensure
    assert "Update-LegacyIdentityEvidence $rawHtmlDir" in ensure
    assert "'--tenant-id',$TenantId,'--raw-html-dir',$rawHtmlDir" in ensure
    assert "data\\console_control" in text


def test_controller_status_progress_and_final_projection_authenticate_to_loopback_console():
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    assert powershell is not None
    run = {
        "run_id": "run-control-observability", "requested_actions": 20, "recorded_actions": 10,
        "inferred_actions": 0, "terminal_status": "blocked",
        "product_succeeded": 6, "variant_redirect": 3, "failed": 0, "blocked": 1,
        "items": ([{"outcome": "completed", "attribution": "evidence"}] * 6
                  + [{"outcome": "variant_redirect", "attribution": "evidence"}] * 3
                  + [{"outcome": "blocked", "attribution": "evidence"}]),
        "proxy_session_pool": {"unrequested_count": 10}, "traffic": {"known_bytes": 4049923},
    }
    authorized_paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.headers.get("X-Collection-API-Key") != "fixture-console-key":
                self.send_response(401); self.end_headers(); return
            authorized_paths.append(self.path)
            payload = (
                {"tenant_id": "tenant-a", "progress": {"touched": 10, "total": 20, "percent": 50, "successful_products": 6}, "status_counts": {"blocked": 1}}
                if self.path.startswith("/api/overview") else
                {"items": [run]} if self.path.startswith("/api/runs?") else run
            )
            body = json.dumps(payload).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        script_path = str(SCRIPT).replace("'", "''")
        command = rf"""
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('{script_path}',[ref]$tokens,[ref]$errors)
foreach($name in @('Invoke-ConsoleApi','Get-RunProjection','Show-Status','Get-FinalRunSnapshot')) {{
  $fn=$ast.FindAll({{param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}},$true) | Select-Object -First 1
  if($null -ne $fn) {{ Invoke-Expression $fn.Extent.Text }}
}}
$consoleUrl='http://127.0.0.1:{server.server_port}'; $TenantId='tenant-a'
$env:AMAZON_COLLECTION_API_KEY='fixture-console-key'
function Read-Lock {{ return [pscustomobject]@{{pid=1;start_time='x'}} }}
function Get-VerifiedProcess {{ return [pscustomobject]@{{Id=1}} }}
function Get-ConsoleReady {{ return [pscustomobject]@{{ok=$true}} }}
$statusError=$null; try {{ $status=Show-Status 'worker.lock' 'console.lock' }} catch {{ $statusError=$_.Exception.Message; $status=$null }}
$progress=$null; if(Get-Command Get-RunProjection -ErrorAction SilentlyContinue) {{ try {{ $progress=Get-RunProjection 'run-control-observability' 2 }} catch {{}} }}
$final=Get-FinalRunSnapshot 'run-control-observability' 20 1
[ordered]@{{status=$status;status_error=$statusError;progress_recorded=$progress.recorded_actions;final_recorded=$final.run.recorded_actions;final_reason=$final.verification_reason}} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=ROOT, capture_output=True, timeout=20,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        assert result.returncode == 0, stderr or stdout
        payload = json.loads(stdout.strip().splitlines()[-1])
        assert payload == {
            "status": 0, "status_error": None, "progress_recorded": 10,
            "final_recorded": 10, "final_reason": "recorded_actions_incomplete:10/20",
        }
        assert len(authorized_paths) == 4
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_controller_projection_preserves_real_partial_blocked_counts_and_unknown_is_not_zero():
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    assert powershell is not None
    script_path = str(SCRIPT).replace("'", "''")
    command = rf"""
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('{script_path}',[ref]$tokens,[ref]$errors)
foreach($name in @('Test-ProbeRunQuality','Test-RunCompleteness','Get-RunObservability')) {{
  $fn=$ast.FindAll({{param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}},$true) | Select-Object -First 1
  if($null -ne $fn) {{ Invoke-Expression $fn.Extent.Text }}
}}
$items=@()
1..6 | ForEach-Object {{ $items += [pscustomobject]@{{outcome='completed';attribution='evidence'}} }}
1..3 | ForEach-Object {{ $items += [pscustomobject]@{{outcome='variant_redirect';attribution='evidence'}} }}
$items += [pscustomobject]@{{outcome='blocked';attribution='evidence'}}
$run=[pscustomobject]@{{requested_actions=20;recorded_actions=10;inferred_actions=0;items=$items;traffic=@{{known_bytes=4049923}};proxy_session_pool=[pscustomobject]@{{unrequested_count=10}}}}
$quality=Test-ProbeRunQuality $run 20
$capacity=[pscustomobject]@{{canary_operation_id='op-canary-1';fact_expires_at='2026-09-03T10:00:00Z';capacity_snapshot=[pscustomobject]@{{canary_status='partial';tested_slots=34;available_slots=32;unique_egress_count=32;slot_capacity=32;capacity_gate_status='allowed';capacity_gate_reason='capacity_sufficient'}}}}
$observability=if(Get-Command Get-RunObservability -ErrorAction SilentlyContinue) {{ Get-RunObservability $capacity $quality 20 }} else {{ $null }}
[ordered]@{{quality=$quality;complete=(Test-RunCompleteness $run 20);unknown=(Test-ProbeRunQuality $null 20);observability=$observability}} | ConvertTo-Json -Depth 8 -Compress
"""
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["quality"]["recorded_actions"] == 10
    assert payload["quality"]["completed_actions"] == 6
    assert payload["quality"]["variant_redirect_actions"] == 3
    assert payload["quality"]["failed_actions"] == 0
    assert payload["quality"]["blocked_actions"] == 1
    assert payload["quality"]["unrequested_actions"] == 10
    assert payload["complete"]["completion_gate_ok"] is False
    assert "recorded_actions:10/20" in payload["complete"]["completion_gate_reason"]
    for field in ("recorded_actions", "completed_actions", "variant_redirect_actions", "failed_actions", "blocked_actions", "inferred_actions", "unrequested_actions"):
        assert payload["unknown"][field] is None
    assert payload["unknown"]["quality_gate_ok"] is False
    assert payload["unknown"]["quality_gate_reason"] == "run_projection_unavailable"
    assert payload["observability"]["proxy_connectivity"]["available_slots"] == 32
    assert payload["observability"]["proxy_connectivity"]["gate_status"] == "allowed"
    assert payload["observability"]["amazon_business"]["requested_actions"] == 20
    assert payload["observability"]["amazon_business"]["recorded_actions"] == 10
    assert payload["observability"]["amazon_business"]["access_control_rate"] == 0.1


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
$mixed = @(
  [pscustomobject]@{{outcome='completed'; attribution='evidence'}},
  [pscustomobject]@{{outcome='completed'; attribution='evidence'}},
  [pscustomobject]@{{outcome='variant_redirect'; attribution='evidence'}}
)
$variantComplete = Test-ProbeRunQuality ([pscustomobject]@{{recorded_actions=3;inferred_actions=0;items=$mixed;proxy_session_pool=[pscustomobject]@{{unrequested_count=0}};traffic=@{{}}}}) 3
$variantUnrequested = Test-ProbeRunQuality ([pscustomobject]@{{recorded_actions=3;inferred_actions=0;items=$mixed;proxy_session_pool=[pscustomobject]@{{unrequested_count=1}};traffic=@{{}}}}) 3
[ordered]@{{good=$good;bad=$bad;partial=$partial;variant_complete=$variantComplete;variant_unrequested=$variantUnrequested}} | ConvertTo-Json -Depth 8 -Compress
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
    assert payload["variant_complete"]["quality_gate_ok"] is True
    assert payload["variant_complete"]["completed_actions"] == 2
    assert payload["variant_complete"]["variant_redirect_actions"] == 1
    assert payload["variant_unrequested"]["quality_gate_ok"] is False
    assert "unrequested_actions" in payload["variant_unrequested"]["quality_gate_reason"]


def test_normal_run_completeness_requires_requested_evidence_but_allows_terminal_variants():
    powershell = shutil.which("powershell") or shutil.which("powershell.exe")
    assert powershell is not None
    script_path = str(SCRIPT).replace("'", "''")
    command = rf"""
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script_path}', [ref]$tokens, [ref]$errors)
$fn = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-RunCompleteness' }}, $true) | Select-Object -First 1
Invoke-Expression $fn.Extent.Text
$short = Test-RunCompleteness ([pscustomobject]@{{recorded_actions=3;inferred_actions=0;items=@([pscustomobject]@{{attribution='evidence'}},[pscustomobject]@{{attribution='evidence'}},[pscustomobject]@{{attribution='evidence'}})}}) 10
$completeWithVariants = Test-RunCompleteness ([pscustomobject]@{{recorded_actions=2;inferred_actions=0;items=@([pscustomobject]@{{attribution='evidence';outcome='completed'}},[pscustomobject]@{{attribution='evidence';outcome='variant_redirect'}})}}) 2
[ordered]@{{short=$short;complete=$completeWithVariants}} | ConvertTo-Json -Depth 5 -Compress
"""
    result = subprocess.run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["short"]["completion_gate_ok"] is False
    assert "recorded_actions:3/10" in payload["short"]["completion_gate_reason"]
    assert payload["complete"]["completion_gate_ok"] is True


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


def test_run_ledger_receipt_uses_json_file_not_native_stdin_pipe():
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("function Finish-RunLedger")
    end = text.index("function Ensure-Console", start)
    body = text[start:end]
    assert "--receipt" in body
    assert "Set-Content -LiteralPath $ledgerReceipt -Encoding UTF8" in body
    assert "| & $python $runLedgerScript" not in body
    assert "Remove-Item -LiteralPath $ledgerReceipt" in body


def test_controller_does_not_prompt_when_dsn_already_contains_password():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "$dsnContainsPassword" in text
    assert "password\\s*=" in text
    assert "-not $dsnContainsPassword" in text


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
    assert text.index("open_owner_monitor(") < text.index("wait_for_gate(gate, cancel, owner_monitor=owner_monitor)")


def test_managed_host_terminates_worker_when_controller_process_exits():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        gate = root / "gate"
        cancel = root / "cancel"
        heartbeat = root / "heartbeat"
        child_pid = root / "child.pid"
        owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            if os.name == "nt":
                owner_started = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command", f"(Get-Process -Id {owner.pid}).StartTime.ToUniversalTime().ToString('o')"],
                    text=True,
                ).strip()
            else:
                owner_started = ""
            child_code = (
                "import os,pathlib,time; "
                f"pathlib.Path(r'{child_pid}').write_text(str(os.getpid())); "
                f"p=pathlib.Path(r'{heartbeat}'); "
                "\nwhile True: p.write_text(str(time.time())); time.sleep(.05)"
            )
            request = root / "request.json"
            request.write_text(json.dumps({
                "python": sys.executable,
                "working_directory": str(ROOT),
                "arguments": ["-c", child_code],
                "gate_path": str(gate),
                "cancel_path": str(cancel),
                "owner_pid": owner.pid,
                "owner_start_time": owner_started,
            }), encoding="utf-8")
            host = subprocess.Popen([sys.executable, str(HOST), "--request", str(request)])
            gate.touch()
            deadline = time.time() + 5
            while time.time() < deadline and not heartbeat.exists():
                time.sleep(.05)
            assert heartbeat.exists(), "controlled worker never started"
            owner.terminate()
            owner.wait(timeout=5)
            assert host.wait(timeout=8) != 0
            before = heartbeat.stat().st_mtime_ns
            time.sleep(.3)
            assert heartbeat.stat().st_mtime_ns == before
            pid = int(child_pid.read_text())
            if os.name == "nt":
                probe = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ exit 1 }}"],
                    timeout=5,
                )
                assert probe.returncode == 0
        finally:
            if owner.poll() is None:
                owner.kill()
                owner.wait(timeout=5)


def test_owner_loss_finalizes_database_receipt_after_worker_termination():
    module = load_host()
    events = []

    class Process:
        returncode = None
        def poll(self): return None if not events else -15
        def terminate(self): events.append("terminated")
        def wait(self, timeout=None): self.returncode = -15; return -15

    module.owner_is_alive = lambda _monitor: False
    module.finalize_interrupted_run = lambda lifecycle, worker_exit, python: events.append(
        (lifecycle["run_id"], worker_exit)
    )

    assert module.wait_for_process(Process(), object(), {"run_id": "run-owner-loss"}, sys.executable) == 130
    assert events == ["terminated", ("run-owner-loss", -15)]


def test_managed_host_stops_worker_when_controller_heartbeat_stales_but_shell_lives():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        gate = root / "gate"
        heartbeat = root / "controller.heartbeat"
        worker_heartbeat = root / "worker.heartbeat"
        owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        host = None
        try:
            if os.name == "nt":
                owner_started = subprocess.check_output(
                    ["powershell", "-NoProfile", "-Command", f"(Get-Process -Id {owner.pid}).StartTime.ToUniversalTime().ToString('o')"],
                    text=True,
                ).strip()
            else:
                owner_started = "owner"
            heartbeat.touch()
            keepalive_stop = threading.Event()
            def refresh_heartbeat():
                while not keepalive_stop.is_set():
                    heartbeat.touch()
                    time.sleep(.1)
            keepalive = threading.Thread(target=refresh_heartbeat, daemon=True)
            keepalive.start()
            child_code = f"import pathlib,time; p=pathlib.Path(r'{worker_heartbeat}');\nwhile True: p.write_text(str(time.time())); time.sleep(.05)"
            request = root / "request.json"
            request.write_text(json.dumps({
                "python": sys.executable, "working_directory": str(ROOT), "arguments": ["-c", child_code],
                "gate_path": str(gate), "cancel_path": str(root / "cancel"),
                "owner_pid": owner.pid, "owner_start_time": owner_started,
                "owner_heartbeat_path": str(heartbeat), "owner_heartbeat_timeout_seconds": 2.0,
            }), encoding="utf-8")
            host = subprocess.Popen([sys.executable, str(HOST), "--request", str(request)])
            gate.touch()
            deadline = time.time() + 5
            while time.time() < deadline and not worker_heartbeat.exists(): time.sleep(.05)
            assert worker_heartbeat.exists()
            keepalive_stop.set()
            keepalive.join(timeout=2)
            assert host.wait(timeout=10) == 130
            assert owner.poll() is None, "the shell/controller process should still be alive in this reproduction"
            before = worker_heartbeat.stat().st_mtime_ns
            time.sleep(.3)
            assert worker_heartbeat.stat().st_mtime_ns == before
        finally:
            if host is not None and host.poll() is None:
                host.kill(); host.wait(timeout=5)
            if owner.poll() is None:
                owner.kill(); owner.wait(timeout=5)


def test_managed_host_aborts_before_gate_when_controller_heartbeat_stales():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        heartbeat = root / "controller.heartbeat"
        owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        host = None
        try:
            owner_started = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", f"(Get-Process -Id {owner.pid}).StartTime.ToUniversalTime().ToString('o')"],
                text=True,
            ).strip() if os.name == "nt" else "owner"
            heartbeat.touch()
            request = root / "request.json"
            request.write_text(json.dumps({
                "python": sys.executable, "working_directory": str(ROOT),
                "arguments": ["-c", "raise SystemExit('must not start')"],
                "gate_path": str(root / "never-created-gate"), "cancel_path": str(root / "cancel"),
                "owner_pid": owner.pid, "owner_start_time": owner_started,
                "owner_heartbeat_path": str(heartbeat), "owner_heartbeat_timeout_seconds": .4,
            }), encoding="utf-8")
            host = subprocess.Popen([sys.executable, str(HOST), "--request", str(request)])
            assert host.wait(timeout=8) == 130
            assert owner.poll() is None
        finally:
            if host is not None and host.poll() is None:
                host.kill(); host.wait(timeout=5)
            if owner.poll() is None:
                owner.kill(); owner.wait(timeout=5)
