param(
    [Parameter(Position = 0)]
    [ValidateSet('probe', 'run', 'reviews', 'status', 'console', 'stop', 'help')]
    [string]$Command = 'help',
    [int]$Limit = 0,
    [string]$TenantId = 'real_batch_20260828_500_04',
    [string]$ManifestPath = 'data\postgres_real_batch_20260828_500_04\manifest_500.csv',
    [string]$ConfigPath = 'data\postgres_real_batch_20260828_500_04\batch500.toml',
    [string]$OutputDir = 'data\postgres_real_batch_20260828_500_04',
    [int]$Port = 8770,
    [switch]$ConfirmLargeBatch,
    [switch]$All
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$workerScript = Join-Path $projectRoot 'scripts\amazon_us_worker.py'
$processHostScript = Join-Path $projectRoot 'scripts\crawler_process_host.py'
$preflightScript = Join-Path $projectRoot 'scripts\preflight.py'
$consoleScript = Join-Path $projectRoot 'scripts\collection_console.py'
$runLedgerScript = Join-Path $projectRoot 'scripts\postgres_run_ledger.py'
$identityBackfillScript = Join-Path $projectRoot 'scripts\backfill_identity_evidence.py'
$consoleUrl = "http://127.0.0.1:${Port}"
$script:promptedForPassword = $false
$script:setDefaultDsn = $false
$script:workerMutex = $null
$script:processHostPython = $null

function Get-WorkerMutexName {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes("${projectRoot}|${TenantId}")
        $hash = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').Substring(0, 20)
        return "Local\AmazonCrawler_${hash}"
    }
    finally { $sha.Dispose() }
}

function Acquire-WorkerMutex {
    $mutex = [System.Threading.Mutex]::new($false, (Get-WorkerMutexName))
    $acquired = $false
    try { $acquired = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) {
        $mutex.Dispose()
        throw 'Another crawler controller already owns the tenant mutex.'
    }
    $script:workerMutex = $mutex
}

function Release-WorkerMutex {
    if ($null -eq $script:workerMutex) { return }
    try { $script:workerMutex.ReleaseMutex() } catch { }
    $script:workerMutex.Dispose()
    $script:workerMutex = $null
}

function Resolve-ProjectPath([string]$Value) {
    $candidate = if ([IO.Path]::IsPathRooted($Value)) { $Value } else { Join-Path $projectRoot $Value }
    $resolved = (Resolve-Path -LiteralPath $candidate).Path
    $rootBoundary = $projectRoot.TrimEnd('\') + '\'
    if ($resolved -ne $projectRoot -and -not $resolved.StartsWith($rootBoundary, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path must stay inside project root: $Value"
    }
    return $resolved
}

function Write-JsonAtomic([object]$Value, [string]$Path) {
    $temporary = "${Path}.${PID}.tmp"
    $Value | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Read-Lock([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json }
    catch { return $null }
}

function Get-VerifiedProcess([object]$Lock) {
    if ($null -eq $Lock -or -not $Lock.pid -or -not $Lock.start_time) { return $null }
    try { $process = Get-Process -Id ([int]$Lock.pid) -ErrorAction Stop }
    catch { return $null }
    try {
        if ($Lock.start_time -is [DateTime]) {
            $expectedUtc = ([DateTime]$Lock.start_time).ToUniversalTime()
        }
        elseif ($Lock.start_time -is [DateTimeOffset]) {
            $expectedUtc = ([DateTimeOffset]$Lock.start_time).UtcDateTime
        }
        else {
            $parsed = [DateTimeOffset]::MinValue
            $valid = [DateTimeOffset]::TryParseExact(
                [string]$Lock.start_time,
                'o',
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::RoundtripKind,
                [ref]$parsed
            )
            if (-not $valid) { return $null }
            $expectedUtc = $parsed.UtcDateTime
        }
        $actualUtc = $process.StartTime.ToUniversalTime()
    }
    catch { return $null }
    if ($actualUtc.Ticks -ne $expectedUtc.Ticks) { return $null }
    return $process
}

function Remove-StaleLock([string]$Path) {
    $lock = Read-Lock $Path
    if ($null -ne $lock -and $null -ne (Get-VerifiedProcess $lock)) { return $lock }
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Force }
    return $null
}

function Get-ConsoleFingerprint {
    return (Get-FileHash -LiteralPath $consoleScript -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Ensure-Credentials {
    if (-not $env:AMAZON_US_POSTGRES_DSN) {
        $env:AMAZON_US_POSTGRES_DSN = 'host=127.0.0.1 port=5432 dbname=postgres user=postgres'
        $script:setDefaultDsn = $true
    }
    if (-not $env:PGPASSWORD) {
        $securePassword = Read-Host 'Enter local PostgreSQL postgres password (input hidden)' -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
        try { $env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
        finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
            $securePassword.Dispose()
        }
        $script:promptedForPassword = $true
    }
}

function Clear-Credentials {
    if ($script:promptedForPassword) { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
    if ($script:setDefaultDsn) { Remove-Item Env:AMAZON_US_POSTGRES_DSN -ErrorAction SilentlyContinue }
}

function Quote-NativeArgument([string]$Value) {
    return '"' + ($Value -replace '"', '\"') + '"'
}

function Get-ProcessHostPython {
    if ($script:processHostPython) { return $script:processHostPython }
    $candidate = (& $python -c "import sys; print(sys._base_executable)").Trim()
    if (-not (Test-Path -LiteralPath $candidate)) { throw 'Base Python executable was not found.' }
    $script:processHostPython = $candidate
    return $candidate
}

function Start-ManagedHost([string]$RequestPath, [string]$Stdout, [string]$Stderr) {
    $hostPython = Get-ProcessHostPython
    $arguments = @(
        (Quote-NativeArgument $processHostScript), '--request', (Quote-NativeArgument $RequestPath)
    ) -join ' '
    return Start-Process -FilePath $hostPython -ArgumentList $arguments -WorkingDirectory $projectRoot `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
}

function Get-ConsoleReady([string]$ConsoleLock) {
    $lock = Read-Lock $ConsoleLock
    if ($null -eq $lock -or $null -eq (Get-VerifiedProcess $lock)) { return $null }
    $fingerprint = Get-ConsoleFingerprint
    if ([string]$lock.runtime_fingerprint -ne $fingerprint) { return $null }
    try {
        $ready = Invoke-RestMethod -Uri "${consoleUrl}/readyz" -TimeoutSec 2
        if (-not $ready.ok) { return $null }
        if ([string]$ready.runtime_fingerprint -ne $fingerprint) { return $null }
        return $ready
    }
    catch { return $null }
}

function Invoke-RunLedger([string[]]$Arguments) {
    & $python $runLedgerScript @Arguments
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL run ledger failed with exit code $LASTEXITCODE" }
}

function Ensure-RunLedgerSchema { Invoke-RunLedger @('ensure-schema') }

function Update-LegacyIdentityEvidence {
    & $python $identityBackfillScript --dsn-env AMAZON_US_POSTGRES_DSN
    if ($LASTEXITCODE -ne 0) { throw "Legacy identity evidence backfill failed with exit code $LASTEXITCODE" }
}

function Start-RunLedger([string]$RunId, [string]$Mode, [int]$ActionLimit, [string]$WorkerId) {
    Invoke-RunLedger @(
        'start','--tenant-id',$TenantId,'--run-id',$RunId,'--command',$Mode,
        '--requested-actions',[string]$ActionLimit,'--worker-id',$WorkerId,'--controller-pid',[string]$PID
    )
}

function Finish-RunLedger([string]$RunId, [string]$Status, [int]$ControllerExitCode, [object]$WorkerExitCode, [string]$Reason, [object]$Receipt) {
    $arguments = [Collections.Generic.List[string]]::new()
    foreach ($value in @('finish','--tenant-id',$TenantId,'--run-id',$RunId,'--status',$Status,
            '--controller-exit-code',[string]$ControllerExitCode)) { $arguments.Add([string]$value) }
    if ($null -ne $WorkerExitCode) { $arguments.Add('--worker-exit-code'); $arguments.Add([string]$WorkerExitCode) }
    if ($Reason) { $arguments.Add('--termination-reason'); $arguments.Add($Reason) }
    $nativeArguments = $arguments.ToArray()
    $Receipt | ConvertTo-Json -Depth 12 -Compress | & $python $runLedgerScript @nativeArguments
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL run ledger failed with exit code $LASTEXITCODE" }
}

function Ensure-Console([string]$ConsoleLock) {
    $liveLock = Remove-StaleLock $ConsoleLock
    if ($null -ne $liveLock) {
        if ($null -ne (Get-ConsoleReady $ConsoleLock)) {
            Write-Host "Console ready: $consoleUrl"
            return
        }
        for ($index = 0; $index -lt 20; $index++) {
            if ($null -ne (Get-ConsoleReady $ConsoleLock)) { Write-Host "Console ready: $consoleUrl"; return }
            Start-Sleep -Milliseconds 250
        }
        Write-Host 'Managed Console runtime does not match; restarting it.'
        Stop-Locked $ConsoleLock 'Console'
    }
    try {
        $health = Invoke-RestMethod -Uri "${consoleUrl}/healthz" -TimeoutSec 2
        if ($health.ok) { throw 'Port is occupied by an unmanaged or stale Console. Stop it outside this controller or choose another -Port.' }
    }
    catch {
        if ($_.Exception.Message -like 'Port is occupied by an unmanaged or stale Console*') { throw }
    }
    $unknownListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $unknownListener) {
        throw 'Port is occupied by an unmanaged or stale Console. Stop it outside this controller or choose another -Port.'
    }
    Ensure-Credentials
    Ensure-RunLedgerSchema
    Update-LegacyIdentityEvidence
    $controlDir = Split-Path -Parent $ConsoleLock
    $stdout = Join-Path $controlDir 'console.stdout.log'
    $stderr = Join-Path $controlDir 'console.stderr.log'
    $requestPath = Join-Path $controlDir 'console.request.json'
    $gatePath = Join-Path $controlDir 'console.start.gate'
    $cancelPath = Join-Path $controlDir 'console.cancel'
    Remove-Item -LiteralPath $requestPath,$gatePath,$cancelPath -Force -ErrorAction SilentlyContinue
    Write-JsonAtomic ([ordered]@{
        python = $python
        working_directory = $projectRoot
        arguments = @($consoleScript,'--host','127.0.0.1','--port',[string]$Port)
        gate_path = $gatePath
        cancel_path = $cancelPath
    }) $requestPath
    $process = Start-ManagedHost $requestPath $stdout $stderr
    try {
        Write-JsonAtomic ([ordered]@{
            pid = $process.Id
            start_time = $process.StartTime.ToUniversalTime().ToString('o')
            runtime_fingerprint = Get-ConsoleFingerprint
            url = $consoleUrl
            stdout = $stdout
            stderr = $stderr
        }) $ConsoleLock
        New-Item -ItemType File -Path $gatePath | Out-Null
        for ($index = 0; $index -lt 30; $index++) {
            if ($null -ne (Get-ConsoleReady $ConsoleLock)) {
                Remove-Item -LiteralPath $requestPath,$gatePath,$cancelPath -Force -ErrorAction SilentlyContinue
                Write-Host "Console started: $consoleUrl"
                return
            }
            if ($process.HasExited) {
                $detail = if (Test-Path -LiteralPath $stderr) { Get-Content -LiteralPath $stderr -Raw } else { '' }
                throw "Console exited during startup. $detail"
            }
            Start-Sleep -Milliseconds 250
        }
        throw 'Console did not become ready in time.'
    }
    catch {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $ConsoleLock,$requestPath,$gatePath,$cancelPath -Force -ErrorAction SilentlyContinue
        throw
    }
}

function Show-Status([string]$WorkerLock, [string]$ConsoleLock) {
    $worker = Read-Lock $WorkerLock
    $workerProcess = Get-VerifiedProcess $worker
    if ($null -ne $workerProcess) {
        Write-Host "Worker: RUNNING pid=$($worker.pid) run_id=$($worker.run_id) command=$($worker.command)"
    }
    else { Write-Host 'Worker: stopped' }
    $console = Read-Lock $ConsoleLock
    $consoleProcess = Get-VerifiedProcess $console
    $ready = Get-ConsoleReady $ConsoleLock
    Write-Host ("Console: " + ($(if ($null -ne $ready) { "ready $consoleUrl" } elseif ($null -ne $consoleProcess) { 'process alive but not ready' } else { 'stopped' })))
    if ($null -eq $ready) { return 1 }
    $tenantQuery = [Uri]::EscapeDataString($TenantId)
    $overview = Invoke-RestMethod -Uri "${consoleUrl}/api/overview?tenant=${tenantQuery}" -TimeoutSec 5
    Write-Host "Tenant: $($overview.tenant_id)"
    Write-Host "Progress: $($overview.progress.touched)/$($overview.progress.total) ($($overview.progress.percent)%)"
    Write-Host "Products: $($overview.progress.successful_products)"
    Write-Host ('Status: ' + (($overview.status_counts.psobject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ', '))
    $runs = Invoke-RestMethod -Uri "${consoleUrl}/api/runs?limit=3&tenant=${tenantQuery}" -TimeoutSec 5
    if ($runs.items.Count -gt 0) {
        Write-Host 'Recent runs:'
        foreach ($run in $runs.items) {
            Write-Host ("  {0} requested/recorded={1}/{2} success={3} variant={4} failed={5} blocked={6} terminal={7}" -f `
                $run.run_id,$run.requested_actions,$run.recorded_actions,$run.product_succeeded,$run.variant_redirect,$run.failed,$run.blocked,$run.terminal_status)
        }
    }
    return 0
}

function Reserve-Worker([string]$WorkerLock, [string]$RunId, [string]$Mode, [int]$ActionLimit) {
    $liveLock = Remove-StaleLock $WorkerLock
    if ($null -ne $liveLock) {
        throw "Worker already running: pid=$($liveLock.pid) run_id=$($liveLock.run_id)"
    }
    $controller = Get-Process -Id $PID
    Write-JsonAtomic ([ordered]@{
        pid = $PID
        start_time = $controller.StartTime.ToUniversalTime().ToString('o')
        role = 'controller'
        run_id = $RunId
        command = $Mode
        limit = $ActionLimit
        tenant_id = $TenantId
    }) $WorkerLock
}

function Get-FinalRunSnapshot([string]$RunId, [int]$ExpectedActions, [int]$MaxAttempts = 40) {
    $latest = $null
    for ($index = 0; $index -lt $MaxAttempts; $index++) {
        try {
            $tenantQuery = [Uri]::EscapeDataString($TenantId)
            $latest = Invoke-RestMethod -Uri "${consoleUrl}/api/runs/${RunId}?tenant=${tenantQuery}" -TimeoutSec 2
            if ([int]$latest.recorded_actions -ge $ExpectedActions) {
                return [pscustomobject][ordered]@{ run = $latest; verification_reason = $null }
            }
        }
        catch { }
        if ($index -lt ($MaxAttempts - 1)) { Start-Sleep -Milliseconds 250 }
    }
    if ($null -eq $latest) {
        return [pscustomobject][ordered]@{ run = $null; verification_reason = 'run_api_unavailable' }
    }
    return [pscustomobject][ordered]@{
        run = $latest
        verification_reason = "recorded_actions_incomplete:$([int]$latest.recorded_actions)/${ExpectedActions}"
    }
}

function Test-ProbeRunQuality([object]$Run, [int]$ExpectedActions) {
    $items = if ($null -eq $Run) { @() } else { @($Run.items) }
    $recorded = if ($null -eq $Run -or $null -eq $Run.recorded_actions) { 0 } else { [int]$Run.recorded_actions }
    $inferred = if ($null -eq $Run -or $null -eq $Run.inferred_actions) { 0 } else { [int]$Run.inferred_actions }
    $completed = @($items | Where-Object { $_.outcome -eq 'completed' }).Count
    $failed = @($items | Where-Object { $_.outcome -eq 'failed' }).Count
    $blocked = @($items | Where-Object { $_.outcome -eq 'blocked' }).Count
    $nonEvidence = @($items | Where-Object { $_.attribution -ne 'evidence' }).Count
    $reasons = [Collections.Generic.List[string]]::new()
    if ($recorded -ne $ExpectedActions) { $reasons.Add("recorded_actions:$recorded/$ExpectedActions") }
    if ($inferred -ne 0) { $reasons.Add("inferred_actions:$inferred") }
    if ($failed -ne 0) { $reasons.Add("failed_actions:$failed") }
    if ($blocked -ne 0) { $reasons.Add("blocked_actions:$blocked") }
    if ($completed -ne $ExpectedActions) { $reasons.Add("completed_actions:$completed/$ExpectedActions") }
    if ($nonEvidence -ne 0) { $reasons.Add("non_evidence_items:$nonEvidence") }
    if ($items.Count -ne $recorded) { $reasons.Add("item_count:$($items.Count)/$recorded") }
    return [pscustomobject][ordered]@{
        quality_gate_ok = $reasons.Count -eq 0
        quality_gate_reason = ($reasons -join ';')
        recorded_actions = $recorded
        completed_actions = $completed
        failed_actions = $failed
        blocked_actions = $blocked
        inferred_actions = $inferred
        traffic = if ($null -eq $Run) { $null } else { $Run.traffic }
    }
}

function Test-RunCompleteness([object]$Run, [int]$ExpectedActions) {
    $items = if ($null -eq $Run) { @() } else { @($Run.items) }
    $recorded = if ($null -eq $Run -or $null -eq $Run.recorded_actions) { 0 } else { [int]$Run.recorded_actions }
    $inferred = if ($null -eq $Run -or $null -eq $Run.inferred_actions) { 0 } else { [int]$Run.inferred_actions }
    $nonEvidence = @($items | Where-Object { $_.attribution -ne 'evidence' }).Count
    $reasons = [Collections.Generic.List[string]]::new()
    if ($recorded -ne $ExpectedActions) { $reasons.Add("recorded_actions:$recorded/$ExpectedActions") }
    if ($inferred -ne 0) { $reasons.Add("inferred_actions:$inferred") }
    if ($nonEvidence -ne 0) { $reasons.Add("non_evidence_items:$nonEvidence") }
    if ($items.Count -ne $recorded) { $reasons.Add("item_count:$($items.Count)/$recorded") }
    return [pscustomobject][ordered]@{
        completion_gate_ok = $reasons.Count -eq 0
        completion_gate_reason = ($reasons -join ';')
    }
}

function Start-Crawl([string]$Mode, [int]$ActionLimit, [string]$ResolvedManifest, [string]$ResolvedConfig, [string]$ResolvedOutput, [string]$WorkerLock, [string]$ConsoleLock) {
    if ($Mode -eq 'probe' -and ($ActionLimit -lt 1 -or $ActionLimit -gt 5)) {
        throw 'probe limit must be between 1 and 5'
    }
    if ($Mode -eq 'run' -and ($ActionLimit -lt 1 -or $ActionLimit -gt 500)) {
        throw 'run limit must be between 1 and 500'
    }
    if ($Mode -eq 'reviews' -and ($ActionLimit -lt 1 -or $ActionLimit -gt 3)) {
        throw 'reviews limit must be between 1 and 3'
    }
    if ($ActionLimit -gt 100 -and -not $ConfirmLargeBatch) {
        throw 'Limits above 100 require -ConfirmLargeBatch'
    }
    Acquire-WorkerMutex
    try {
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    $entropy = [Guid]::NewGuid().ToString('N').Substring(0, 10)
    $runId = "run-control-${stamp}-${PID}-${entropy}"
    $workerId = "amazon-us-${Mode}-${PID}"
    $runDir = Join-Path (Join-Path $ResolvedOutput 'control\runs') $runId
    if (Test-Path -LiteralPath $runDir) { throw "Run directory already exists: $runDir" }
    New-Item -ItemType Directory -Path $runDir | Out-Null
    $stdout = Join-Path $runDir 'worker.stdout.log'
    $stderr = Join-Path $runDir 'worker.stderr.log'
    $preflightLog = Join-Path $runDir 'preflight.log'
    $receiptPath = Join-Path $runDir 'receipt.json'
    $requestPath = Join-Path $runDir 'worker.request.json'
    $gatePath = Join-Path $runDir 'worker.start.gate'
    $cancelPath = Join-Path $runDir 'worker.cancel'
    $heartbeatPath = Join-Path $runDir 'controller.heartbeat'
    $startedAt = [DateTime]::UtcNow
    $controllerStartTime = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('o')
    $runLedgerStarted = $false
    $runLedgerFinished = $false
    Reserve-Worker $WorkerLock $runId $Mode $ActionLimit
    $process = $null
    try {
        Ensure-Credentials
        Ensure-Console $ConsoleLock
        Write-Host "Preflight: tenant=$TenantId mode=$Mode limit=$ActionLimit"
        $preflightOutput = & $python $preflightScript --manifest $ResolvedManifest --config $ResolvedConfig `
            --backend postgres --dsn-env AMAZON_US_POSTGRES_DSN --require-live
        $preflightExit = $LASTEXITCODE
        $preflightOutput | Out-String | Set-Content -LiteralPath $preflightLog -Encoding UTF8
        $preflightOutput | ForEach-Object { Write-Host $_ }
        if ($preflightExit -ne 0) { throw "Preflight failed with exit code $preflightExit" }
        Start-RunLedger $runId $Mode $ActionLimit $workerId
        $runLedgerStarted = $true
        $stageOnlyArgument = if ($Mode -eq 'reviews') { '--reviews-only' } else { '--product-only' }
        $workerArguments = @(
            $workerScript, '--config', $ResolvedConfig,
            '--backend', 'postgres', '--dsn-env', 'AMAZON_US_POSTGRES_DSN',
            '--tenant-id', $TenantId, '--subject-type', 'own',
            '--worker-id', $workerId, '--lease-seconds', '600',
            '--manifest', $ResolvedManifest, '--output-dir', $ResolvedOutput,
            '--run-id', $runId, '--live', '--once', '--limit', [string]$ActionLimit, $stageOnlyArgument
        )
        Write-JsonAtomic ([ordered]@{
            python = $python
            working_directory = $projectRoot
            arguments = $workerArguments
            gate_path = $gatePath
            cancel_path = $cancelPath
            owner_pid = $PID
            owner_start_time = $controllerStartTime
            owner_heartbeat_path = $heartbeatPath
            owner_heartbeat_timeout_seconds = 15
            lifecycle = [ordered]@{
                dsn_env = 'AMAZON_US_POSTGRES_DSN'
                tenant_id = $TenantId
                run_id = $runId
                command = $Mode
                requested_actions = $ActionLimit
                receipt_path = $receiptPath
            }
        }) $requestPath
        [IO.File]::WriteAllText($heartbeatPath, [DateTime]::UtcNow.ToString('o'))
        $process = Start-ManagedHost $requestPath $stdout $stderr
        Write-JsonAtomic ([ordered]@{
            pid = $process.Id
            start_time = $process.StartTime.ToUniversalTime().ToString('o')
            role = 'worker'
            run_id = $runId
            command = $Mode
            limit = $ActionLimit
            tenant_id = $TenantId
            stdout = $stdout
            stderr = $stderr
            receipt = $receiptPath
        }) $WorkerLock
        New-Item -ItemType File -Path $gatePath | Out-Null
        Write-Host "Worker started: pid=$($process.Id) run_id=$runId"
        Write-Host "Console: $consoleUrl"
        Write-Host "Logs: $runDir"
        while (-not $process.HasExited) {
            [IO.File]::WriteAllText($heartbeatPath, [DateTime]::UtcNow.ToString('o'))
            try {
                $tenantQuery = [Uri]::EscapeDataString($TenantId)
                $run = Invoke-RestMethod -Uri "${consoleUrl}/api/runs/${runId}?tenant=${tenantQuery}" -TimeoutSec 2
                $completed = $run.items.Count
                $failed = @($run.items | Where-Object { $_.outcome -eq 'failed' }).Count
                $blocked = @($run.items | Where-Object { $_.outcome -eq 'blocked' }).Count
                Write-Host ("Progress: {0}/{1} failed={2} blocked={3}" -f $completed,$ActionLimit,$failed,$blocked)
            }
            catch { Write-Host "Progress: waiting for first action evidence (target=$ActionLimit)" }
            Start-Sleep -Seconds 5
            $process.Refresh()
        }
        $workerExitCode = $process.ExitCode
        $finalAttempts = 20
        $finalSnapshot = Get-FinalRunSnapshot $runId $ActionLimit $finalAttempts
        $quality = Test-ProbeRunQuality $finalSnapshot.run $ActionLimit
        $completion = Test-RunCompleteness $finalSnapshot.run $ActionLimit
        $reasonParts = [Collections.Generic.List[string]]::new()
        if ($finalSnapshot.verification_reason) { $reasonParts.Add([string]$finalSnapshot.verification_reason) }
        if ($Mode -in @('probe', 'reviews')) {
            if ($quality.quality_gate_reason) { $reasonParts.Add([string]$quality.quality_gate_reason) }
        }
        elseif ($completion.completion_gate_reason) { $reasonParts.Add([string]$completion.completion_gate_reason) }
        $runVerificationReason = $reasonParts -join ';'
        $completionGateOk = $null -eq $finalSnapshot.verification_reason -and [bool]$completion.completion_gate_ok
        $qualityGateOk = $completionGateOk -and [bool]$quality.quality_gate_ok
        $controllerExitCode = $workerExitCode
        $outcome = if ($workerExitCode -eq 0) { 'completed' } elseif ($workerExitCode -eq 3) { 'blocked' } else { 'failed' }
        if ($Mode -in @('probe', 'reviews') -and ($workerExitCode -ne 0 -or -not $qualityGateOk)) {
            $outcome = 'quality_failed'
            $controllerExitCode = 4
        }
        elseif ($Mode -eq 'run' -and $workerExitCode -eq 0 -and -not $completionGateOk) {
            $outcome = 'failed'
            $controllerExitCode = 4
        }
        $finishedAt = [DateTime]::UtcNow
        Write-Host ("Final: {0}/{1} completed={2} failed={3} blocked={4} inferred={5} quality_gate_ok={6}" -f $quality.recorded_actions,$ActionLimit,$quality.completed_actions,$quality.failed_actions,$quality.blocked_actions,$quality.inferred_actions,$qualityGateOk)
        if ($runVerificationReason) { Write-Host "Final verification: $runVerificationReason" }
        $receipt = [ordered]@{
            schema_version = 'amazon-us-control-receipt-v3'
            run_id = $runId
            worker_id = $workerId
            tenant_id = $TenantId
            command = $Mode
            requested_limit = $ActionLimit
            status = $outcome
            exit_code = $controllerExitCode
            worker_exit_code = $workerExitCode
            recorded_actions = $quality.recorded_actions
            completed_actions = $quality.completed_actions
            failed_actions = $quality.failed_actions
            blocked_actions = $quality.blocked_actions
            inferred_actions = $quality.inferred_actions
            traffic = $quality.traffic
            quality_gate_ok = $qualityGateOk
            completion_gate_ok = $completionGateOk
            run_verification_reason = $runVerificationReason
            started_at = $startedAt.ToString('o')
            finished_at = $finishedAt.ToString('o')
            elapsed_seconds = [Math]::Round(($finishedAt - $startedAt).TotalSeconds, 2)
            stdout_log = $stdout
            stderr_log = $stderr
            preflight_log = $preflightLog
            console_url = $consoleUrl
        }
        Finish-RunLedger $runId $outcome $controllerExitCode $workerExitCode $runVerificationReason $receipt
        $runLedgerFinished = $true
        Write-JsonAtomic $receipt $receiptPath
        Write-Host "Worker finished: status=$outcome exit=$controllerExitCode worker_exit=$workerExitCode receipt=$receiptPath"
        if (Test-Path -LiteralPath $stdout) {
            Get-Content -LiteralPath $stdout -Tail 40 | ForEach-Object { Write-Host $_ }
        }
        if ((Test-Path -LiteralPath $stderr) -and (Get-Item -LiteralPath $stderr).Length -gt 0) {
            Write-Host 'Worker stderr:'
            Get-Content -LiteralPath $stderr -Tail 40 | ForEach-Object { Write-Host $_ }
        }
        return $controllerExitCode
    }
    catch {
        $caughtError = $_.Exception.Message
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            try { $process.WaitForExit(5000) | Out-Null } catch { }
            $process.Refresh()
        }
        $finishedAt = [DateTime]::UtcNow
        $failedReceipt = [ordered]@{
            schema_version = 'amazon-us-control-receipt-v3'
            run_id = $runId
            worker_id = $workerId
            tenant_id = $TenantId
            command = $Mode
            requested_limit = $ActionLimit
            status = 'failed'
            exit_code = 2
            worker_exit_code = if ($null -ne $process -and $process.HasExited) { $process.ExitCode } else { $null }
            recorded_actions = 0
            completed_actions = 0
            failed_actions = 0
            blocked_actions = 0
            inferred_actions = 0
            traffic = $null
            quality_gate_ok = $false
            run_verification_reason = 'controller_exception'
            termination_reason = 'controller_exception'
            error = $caughtError
            started_at = $startedAt.ToString('o')
            finished_at = $finishedAt.ToString('o')
            elapsed_seconds = [Math]::Round(($finishedAt - $startedAt).TotalSeconds, 2)
            stdout_log = $stdout
            stderr_log = $stderr
            preflight_log = $preflightLog
            console_url = $consoleUrl
        }
        if ($runLedgerStarted -and -not $runLedgerFinished) {
            Finish-RunLedger $runId 'failed' 2 $failedReceipt.worker_exit_code 'controller_exception' $failedReceipt
            $runLedgerFinished = $true
        }
        Write-JsonAtomic $failedReceipt $receiptPath
        throw
    }
    finally {
        $lock = Read-Lock $WorkerLock
        $processStillRunning = $null -ne $process -and -not $process.HasExited
        if (-not $processStillRunning -and $null -ne $lock -and $lock.run_id -eq $runId) {
            Remove-Item -LiteralPath $WorkerLock -Force -ErrorAction SilentlyContinue
        }
        if (-not $processStillRunning) {
            Remove-Item -LiteralPath $requestPath,$gatePath,$cancelPath,$heartbeatPath -Force -ErrorAction SilentlyContinue
        }
    }
    }
    finally { Release-WorkerMutex }
}

function Stop-Locked([string]$LockPath, [string]$Name) {
    $lock = Read-Lock $LockPath
    $process = Get-VerifiedProcess $lock
    if ($null -eq $process) {
        if (Test-Path -LiteralPath $LockPath) { Remove-Item -LiteralPath $LockPath -Force }
        Write-Host "${Name}: not running"
        return
    }
    Stop-Process -Id $process.Id -Force
    for ($index = 0; $index -lt 20; $index++) {
        try { $null = Get-Process -Id $process.Id -ErrorAction Stop }
        catch { break }
        Start-Sleep -Milliseconds 250
    }
    $stillRunning = $false
    try { $null = Get-Process -Id $process.Id -ErrorAction Stop; $stillRunning = $true }
    catch { }
    if ($stillRunning) { throw "${Name} process did not stop after Job Object host termination." }
    Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue
    Write-Host "${Name}: stopped pid=$($process.Id)"
}

function Report-UnmanagedConsoleListener([string]$ConsoleLock) {
    $lock = Read-Lock $ConsoleLock
    if ($null -ne $lock -and $null -ne (Get-VerifiedProcess $lock)) { return }
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $listener) {
        Write-Host "Console: unmanaged listener remains on $consoleUrl; it was not stopped."
    }
}

function Show-Help {
    Write-Host @'
Amazon crawler control

  .\crawler.ps1 probe
  .\crawler.ps1 run -Limit 10
  .\crawler.ps1 reviews -Limit 3
  .\crawler.ps1 status
  .\crawler.ps1 console
  .\crawler.ps1 stop
  .\crawler.ps1 stop -All

probe defaults to 3 product actions. run defaults to 10. reviews defaults to 3 review actions.
Limits above 100 require -ConfirmLargeBatch.
Blocked tasks are never requeued automatically.
'@
}

$exitCode = 0
try {
    if ($Command -eq 'help') {
        Show-Help
        exit 0
    }
    $consoleControlDir = Join-Path $projectRoot 'data\console_control'
    New-Item -ItemType Directory -Path $consoleControlDir -Force | Out-Null
    $consoleLock = Join-Path $consoleControlDir '.console.lock.json'
    if ($Command -eq 'console') {
        if (-not (Test-Path -LiteralPath $python)) { throw 'Run setup_windows.bat first.' }
        Ensure-Console $consoleLock
        Write-Host ("Open: {0}/?tenant={1}" -f $consoleUrl,[Uri]::EscapeDataString($TenantId))
    }
    else {
        $resolvedOutput = Resolve-ProjectPath $OutputDir
        $workerControlDir = Join-Path $resolvedOutput 'control'
        New-Item -ItemType Directory -Path $workerControlDir -Force | Out-Null
        $workerLock = Join-Path $workerControlDir '.worker.lock.json'
        switch ($Command) {
        'probe' {
            if (-not (Test-Path -LiteralPath $python)) { throw 'Run setup_windows.bat first.' }
            $resolvedManifest = Resolve-ProjectPath $ManifestPath
            $resolvedConfig = Resolve-ProjectPath $ConfigPath
            $actualLimit = if ($Limit -gt 0) { $Limit } else { 3 }
            $exitCode = Start-Crawl 'probe' $actualLimit $resolvedManifest $resolvedConfig $resolvedOutput $workerLock $consoleLock
        }
        'run' {
            if (-not (Test-Path -LiteralPath $python)) { throw 'Run setup_windows.bat first.' }
            $resolvedManifest = Resolve-ProjectPath $ManifestPath
            $resolvedConfig = Resolve-ProjectPath $ConfigPath
            $actualLimit = if ($Limit -gt 0) { $Limit } else { 10 }
            $exitCode = Start-Crawl 'run' $actualLimit $resolvedManifest $resolvedConfig $resolvedOutput $workerLock $consoleLock
        }
        'reviews' {
            if (-not (Test-Path -LiteralPath $python)) { throw 'Run setup_windows.bat first.' }
            $resolvedManifest = Resolve-ProjectPath $ManifestPath
            $resolvedConfig = Resolve-ProjectPath $ConfigPath
            $actualLimit = if ($Limit -gt 0) { $Limit } else { 3 }
            $exitCode = Start-Crawl 'reviews' $actualLimit $resolvedManifest $resolvedConfig $resolvedOutput $workerLock $consoleLock
        }
        'status' { $exitCode = Show-Status $workerLock $consoleLock }
        'stop' {
            Stop-Locked $workerLock 'Worker'
            if ($All) {
                Stop-Locked $consoleLock 'Console'
                Report-UnmanagedConsoleListener $consoleLock
            }
        }
    }
    }
}
catch {
    Write-Host ("ERROR: " + $_.Exception.Message) -ForegroundColor Red
    $exitCode = 2
}
finally {
    Clear-Credentials
}
exit $exitCode
