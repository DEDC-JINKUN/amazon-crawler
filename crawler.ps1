param(
    [Parameter(Position = 0)]
    [ValidateSet('egress', 'canary', 'probe', 'run', 'reviews', 'status', 'console', 'stop', 'help')]
    [string]$Command = 'help',
    [int]$Limit = 0,
    [string]$TenantId = 'real_batch_20260828_500_04',
    [string]$ManifestPath = 'data\postgres_real_batch_20260828_500_04\manifest_500.csv',
    [string]$ConfigPath = 'data\postgres_real_batch_20260828_500_04\batch500.toml',
    [string]$OutputDir = 'data\postgres_real_batch_20260828_500_04',
    [int]$Port = 8770,
    [string]$EgressId = 'dataimpulse-us',
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
$operationLedgerScript = Join-Path $projectRoot 'scripts\operation_ledger.py'
$egressOperationScript = Join-Path $projectRoot 'scripts\egress_operation.py'
$proxyCanaryScript = Join-Path $projectRoot 'scripts\proxy_canary.py'
$proxyCapacityGateScript = Join-Path $projectRoot 'scripts\proxy_capacity_gate.py'
$nonAmazonCanaryUrl = 'https://api.ipify.org?format=json'
$consoleUrl = "http://127.0.0.1:${Port}"
$script:promptedForPassword = $false
$script:setDefaultDsn = $false
$script:workerMutex = $null
$script:processHostPython = $null
$script:PendingOperationId = $null
$script:PendingRunId = $null
$script:PendingOperationStarted = $false
$script:PendingOperationFinished = $false
$script:PendingOperationStage = $null
$script:PendingCapacityGateReason = $null

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
    $dsnContainsPassword = $env:AMAZON_US_POSTGRES_DSN -match '(?i)(?:^|\s)password\s*='
    if (-not $env:PGPASSWORD -and -not $dsnContainsPassword) {
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

function Ensure-CredentialGeneration {
    if ([string]::IsNullOrWhiteSpace([string]$env:AMAZON_PROXY_CREDENTIAL_GENERATION)) {
        throw 'AMAZON_PROXY_CREDENTIAL_GENERATION is required; use run_owned_full_secure.ps1 or set a non-secret version and rotate it with proxy credentials.'
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

function Invoke-ConsoleApi([string]$Path, [int]$TimeoutSec = 5) {
    if (-not $Path.StartsWith('/api/', [StringComparison]::Ordinal)) {
        throw 'Console API path must stay under /api/.'
    }
    $base = [Uri]$consoleUrl
    $target = [Uri]::new($base, $Path)
    if ($base.Scheme -ne 'http' -or -not $base.IsLoopback -or
        $target.Scheme -ne $base.Scheme -or $target.Authority -ne $base.Authority -or
        -not $target.AbsolutePath.StartsWith('/api/', [StringComparison]::Ordinal)) {
        throw 'Console API is restricted to the configured loopback endpoint.'
    }
    $headers = @{}
    $key = [string]$env:AMAZON_COLLECTION_API_KEY
    if (-not [string]::IsNullOrWhiteSpace($key)) {
        $headers['X-Collection-API-Key'] = $key
    }
    try {
        return Invoke-RestMethod -Uri $target.AbsoluteUri -Headers $headers -TimeoutSec $TimeoutSec
    }
    finally {
        $headers.Clear()
        $key = $null
    }
}

function Get-RunProjection([string]$RunId, [int]$TimeoutSec = 2) {
    $tenantQuery = [Uri]::EscapeDataString($TenantId)
    return Invoke-ConsoleApi "/api/runs/${RunId}?tenant=${tenantQuery}" $TimeoutSec
}

function Invoke-RunLedger([string[]]$Arguments) {
    & $python $runLedgerScript @Arguments
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL run ledger failed with exit code $LASTEXITCODE" }
}

function Ensure-RunLedgerSchema { Invoke-RunLedger @('ensure-schema') }

function Invoke-OperationLedger([string[]]$Arguments) {
    & $python $operationLedgerScript @Arguments
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL operation ledger failed with exit code $LASTEXITCODE" }
}

function Ensure-OperationLedgerSchema { Invoke-OperationLedger @('ensure-schema') }

function Start-Operation([string]$OperationId, [string]$Type, [object]$CollectionRunId) {
    $arguments = [Collections.Generic.List[string]]::new()
    foreach ($value in @(
        'start','--operation-id',$OperationId,'--tenant-id',$TenantId,
        '--operation-type',$Type,'--egress-id',$EgressId
    )) { $arguments.Add([string]$value) }
    if ($CollectionRunId) {
        $arguments.Add('--collection-run-id')
        $arguments.Add([string]$CollectionRunId)
    }
    Invoke-OperationLedger $arguments.ToArray()
}

function Mark-OperationPreflight([string]$OperationId, [string]$Status, [double]$DurationMs, [object]$ErrorClass) {
    $arguments = [Collections.Generic.List[string]]::new()
    foreach ($value in @(
        'preflight','--operation-id',$OperationId,'--tenant-id',$TenantId,
        '--status',$Status,'--duration-ms',[string]$DurationMs
    )) { $arguments.Add([string]$value) }
    if ($ErrorClass) {
        $arguments.Add('--error-class')
        $arguments.Add([string]$ErrorClass)
    }
    Invoke-OperationLedger $arguments.ToArray()
}

function Finish-Operation(
    [string]$OperationId,
    [string]$Status,
    [object]$FailureStage,
    [object]$ErrorClass,
    [object]$HttpStatus,
    [object]$ResponseBytes,
    [object]$ProbeElapsedMs
) {
    $arguments = [Collections.Generic.List[string]]::new()
    foreach ($value in @(
        'finish','--operation-id',$OperationId,'--tenant-id',$TenantId,'--status',$Status
    )) { $arguments.Add([string]$value) }
    foreach ($pair in @(
        @('--failure-stage',$FailureStage),@('--error-class',$ErrorClass),@('--http-status',$HttpStatus),
        @('--response-bytes',$ResponseBytes),@('--probe-elapsed-ms',$ProbeElapsedMs)
    )) {
        if ($null -ne $pair[1] -and [string]$pair[1] -ne '') {
            $arguments.Add([string]$pair[0])
            $arguments.Add([string]$pair[1])
        }
    }
    Invoke-OperationLedger $arguments.ToArray()
}

function Get-PreflightErrorClass([object]$Preflight) {
    if ($null -eq $Preflight) { return 'preflight_output_invalid' }
    $failed = @($Preflight.checks | Where-Object { -not $_.ok } | Select-Object -First 1)
    if ($failed.Count -eq 0) { return $null }
    if ($failed[0].name -eq 'proxy_probe') {
        try {
            $detail = [string]$failed[0].detail | ConvertFrom-Json
            if ($detail.block_reason) { return [string]$detail.block_reason }
        }
        catch { }
    }
    return "$($failed[0].name)_failed"
}

function Update-LegacyIdentityEvidence {
    & $python $identityBackfillScript --dsn-env AMAZON_US_POSTGRES_DSN
    if ($LASTEXITCODE -ne 0) { throw "Legacy identity evidence backfill failed with exit code $LASTEXITCODE" }
}

function Start-RunLedger([string]$RunId, [string]$Mode, [int]$ActionLimit, [string]$WorkerId, [string]$OperationId) {
    Invoke-RunLedger @(
        'start','--tenant-id',$TenantId,'--run-id',$RunId,'--command',$Mode,
        '--requested-actions',[string]$ActionLimit,'--worker-id',$WorkerId,'--controller-pid',[string]$PID,
        '--operation-id',$OperationId
    )
}

function Finish-RunLedger([string]$RunId, [string]$Status, [int]$ControllerExitCode, [object]$WorkerExitCode, [string]$Reason, [object]$Receipt) {
    $arguments = [Collections.Generic.List[string]]::new()
    foreach ($value in @('finish','--tenant-id',$TenantId,'--run-id',$RunId,'--status',$Status,
            '--controller-exit-code',[string]$ControllerExitCode)) { $arguments.Add([string]$value) }
    if ($null -ne $WorkerExitCode) { $arguments.Add('--worker-exit-code'); $arguments.Add([string]$WorkerExitCode) }
    if ($Reason) { $arguments.Add('--termination-reason'); $arguments.Add($Reason) }
    $ledgerReceipt = Join-Path ([IO.Path]::GetTempPath()) ("amazon-us-ledger-{0}.json" -f [guid]::NewGuid().ToString('N'))
    try {
        $Receipt | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $ledgerReceipt -Encoding UTF8
        $arguments.Add('--receipt')
        $arguments.Add($ledgerReceipt)
        $nativeArguments = $arguments.ToArray()
        & $python $runLedgerScript @nativeArguments
        if ($LASTEXITCODE -ne 0) { throw "PostgreSQL run ledger failed with exit code $LASTEXITCODE" }
    }
    finally {
        Remove-Item -LiteralPath $ledgerReceipt -Force -ErrorAction SilentlyContinue
    }
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
    $overview = Invoke-ConsoleApi "/api/overview?tenant=${tenantQuery}" 5
    Write-Host "Tenant: $($overview.tenant_id)"
    Write-Host "Progress: $($overview.progress.touched)/$($overview.progress.total) ($($overview.progress.percent)%)"
    Write-Host "Products: $($overview.progress.successful_products)"
    Write-Host ('Status: ' + (($overview.status_counts.psobject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ', '))
    $runs = Invoke-ConsoleApi "/api/runs?limit=3&tenant=${tenantQuery}" 5
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
            $latest = Get-RunProjection $RunId 2
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
    if ($null -eq $Run) {
        return [pscustomobject][ordered]@{
            quality_gate_ok = $false
            quality_gate_reason = 'run_projection_unavailable'
            recorded_actions = $null
            completed_actions = $null
            variant_redirect_actions = $null
            failed_actions = $null
            blocked_actions = $null
            inferred_actions = $null
            unrequested_actions = $null
            traffic = $null
        }
    }
    $items = if ($null -eq $Run) { @() } else { @($Run.items) }
    $recorded = if ($null -eq $Run.recorded_actions) { $null } else { [int]$Run.recorded_actions }
    $inferred = if ($null -eq $Run.inferred_actions) { $null } else { [int]$Run.inferred_actions }
    $completed = @($items | Where-Object { $_.outcome -eq 'completed' }).Count
    $variant = @($items | Where-Object { $_.outcome -eq 'variant_redirect' }).Count
    $failed = @($items | Where-Object { $_.outcome -eq 'failed' }).Count
    $blocked = @($items | Where-Object { $_.outcome -eq 'blocked' }).Count
    $unrequested = if ($null -eq $Run.proxy_session_pool -or $null -eq $Run.proxy_session_pool.unrequested_count) {
        $null
    } else { [int]$Run.proxy_session_pool.unrequested_count }
    $nonEvidence = @($items | Where-Object { $_.attribution -ne 'evidence' }).Count
    $reasons = [Collections.Generic.List[string]]::new()
    if ($recorded -ne $ExpectedActions) { $reasons.Add("recorded_actions:$recorded/$ExpectedActions") }
    if ($inferred -ne 0) { $reasons.Add("inferred_actions:$inferred") }
    if ($failed -ne 0) { $reasons.Add("failed_actions:$failed") }
    if ($blocked -ne 0) { $reasons.Add("blocked_actions:$blocked") }
    if (($completed + $variant) -ne $recorded) { $reasons.Add("resolved_actions:$($completed + $variant)/$recorded") }
    if ($null -ne $unrequested -and $unrequested -ne 0) { $reasons.Add("unrequested_actions:$unrequested") }
    if ($nonEvidence -ne 0) { $reasons.Add("non_evidence_items:$nonEvidence") }
    if ($items.Count -ne $recorded) { $reasons.Add("item_count:$($items.Count)/$recorded") }
    return [pscustomobject][ordered]@{
        quality_gate_ok = $reasons.Count -eq 0
        quality_gate_reason = ($reasons -join ';')
        recorded_actions = $recorded
        completed_actions = $completed
        variant_redirect_actions = $variant
        failed_actions = $failed
        blocked_actions = $blocked
        inferred_actions = $inferred
        unrequested_actions = $unrequested
        traffic = if ($null -eq $Run) { $null } else { $Run.traffic }
    }
}

function Test-RunCompleteness([object]$Run, [int]$ExpectedActions) {
    if ($null -eq $Run) {
        return [pscustomobject][ordered]@{
            completion_gate_ok = $false
            completion_gate_reason = 'run_projection_unavailable'
        }
    }
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

function Get-RunObservability([object]$CapacityAuthorization, [object]$Quality, [int]$RequestedActions) {
    $snapshot = if ($null -eq $CapacityAuthorization) { $null } else { $CapacityAuthorization.capacity_snapshot }
    $recorded = $Quality.recorded_actions
    $blocked = $Quality.blocked_actions
    $accessControlRate = if ($null -ne $recorded -and [int]$recorded -gt 0 -and $null -ne $blocked) {
        [Math]::Round(([double]$blocked / [double]$recorded), 4)
    } else { $null }
    return [pscustomobject][ordered]@{
        proxy_connectivity = [ordered]@{
            egress_profile = 'proxy_sessions'
            canary_operation_id = if ($null -eq $CapacityAuthorization) { $null } else { $CapacityAuthorization.canary_operation_id }
            canary_status = if ($null -eq $snapshot) { $null } else { $snapshot.canary_status }
            tested_slots = if ($null -eq $snapshot) { $null } else { $snapshot.tested_slots }
            available_slots = if ($null -eq $snapshot) { $null } else { $snapshot.available_slots }
            unique_egress_count = if ($null -eq $snapshot) { $null } else { $snapshot.unique_egress_count }
            slot_capacity = if ($null -eq $snapshot) { $null } else { $snapshot.slot_capacity }
            gate_status = if ($null -eq $snapshot) { $null } else { $snapshot.capacity_gate_status }
            gate_reason = if ($null -eq $snapshot) { $null } else { $snapshot.capacity_gate_reason }
            fact_expires_at = if ($null -eq $CapacityAuthorization) { $null } else { $CapacityAuthorization.fact_expires_at }
        }
        amazon_business = [ordered]@{
            requested_actions = $RequestedActions
            recorded_actions = $Quality.recorded_actions
            completed_actions = $Quality.completed_actions
            variant_redirect_actions = $Quality.variant_redirect_actions
            failed_actions = $Quality.failed_actions
            blocked_actions = $Quality.blocked_actions
            unrequested_actions = $Quality.unrequested_actions
            access_control_rate = $accessControlRate
        }
    }
}

function Start-EgressOperation([string]$ConfigValue) {
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    $entropy = [Guid]::NewGuid().ToString('N').Substring(0, 10)
    $operationId = "op-control-${stamp}-${PID}-${entropy}"
    $operationStarted = $false
    $operationFinished = $false
    $operationStage = 'credentials'
    try {
        Ensure-Credentials
        Ensure-OperationLedgerSchema
        Start-Operation $operationId 'egress' $null
        $operationStarted = $true
        $operationStage = 'configuration'
        $resolvedConfig = Resolve-ProjectPath $ConfigValue
        $operationStage = 'egress'
        $probeOutput = & $python $egressOperationScript --config $resolvedConfig
        $probeExit = $LASTEXITCODE
        try { $probe = ($probeOutput | Out-String) | ConvertFrom-Json }
        catch { throw 'Egress check returned invalid output.' }
        $status = if ($probe.ok) { 'succeeded' } else { 'failed' }
        $errorClass = if ($probe.error_class) { [string]$probe.error_class } elseif ($probe.ok) { $null } else { 'egress_failed' }
        $failureStage = if ($probe.ok) { $null } else { 'egress' }
        Finish-Operation $operationId $status $failureStage $errorClass $probe.status $probe.response_bytes $probe.elapsed_ms
        $operationFinished = $true
        Write-Host ("Egress: operation_id={0} status={1} http={2} elapsed_ms={3} error={4}" -f $operationId,$status,$probe.status,$probe.elapsed_ms,$errorClass)
        return $probeExit
    }
    catch {
        if ($operationStarted -and -not $operationFinished) {
            $errorClass = if ($operationStage -eq 'configuration') { 'configuration_error' } else { 'controller_error' }
            Finish-Operation $operationId 'failed' $operationStage $errorClass $null $null $null
            $operationFinished = $true
        }
        throw
    }
}

function Start-ProxyCanary([string]$ConfigValue, [int]$RequestedActions) {
    if ($RequestedActions -lt 1 -or $RequestedActions -gt 500) { throw 'canary limit must be between 1 and 500' }
    Ensure-Credentials
    Ensure-CredentialGeneration
    $resolvedConfig = Resolve-ProjectPath $ConfigValue
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    $entropy = [Guid]::NewGuid().ToString('N').Substring(0, 10)
    $operationId = "op-canary-${stamp}-${PID}-${entropy}"
    $output = & $python $proxyCanaryScript --config $resolvedConfig --tenant-id $TenantId `
        --requested-actions $RequestedActions --operation-id $operationId
    $canaryExit = $LASTEXITCODE
    try { $result = ($output | Out-String) | ConvertFrom-Json }
    catch { throw 'Proxy canary returned invalid output.' }
    Write-Host ("Proxy canary: operation_id={0} status={1} planned/tested/available/unique={2}/{3}/{4}/{5} capacity={6}/{7} gate={8}:{9} p95_ms={10}" -f `
        $operationId,$result.canary_status,$result.planned_slots,$result.tested_slots,$result.available_slots,
        $result.unique_egress_count,$result.slot_capacity,$result.requested_capacity,
        $result.capacity_gate_status,$result.capacity_gate_reason,$result.p95_latency_ms)
    return $canaryExit
}

function Invoke-CapacityGate([string]$ResolvedConfig, [int]$RequestedActions, [string]$OwnerId, [string]$ReservationId) {
    $output = & $python $proxyCapacityGateScript --config $ResolvedConfig --tenant-id $TenantId `
        --requested-actions $RequestedActions --reserve --reservation-owner $OwnerId `
        --reservation-id $ReservationId --lease-seconds 600
    $gateExit = $LASTEXITCODE
    try { $result = ($output | Out-String) | ConvertFrom-Json }
    catch { throw 'Capacity gate returned invalid output.' }
    if ($gateExit -ne 0 -or $result.status -ne 'active') {
        $reason = if ($result.reason) { [string]$result.reason } else { 'capacity_gate_error' }
        $script:PendingCapacityGateReason = $reason
        throw "Capacity gate denied: $reason"
    }
    Write-Host ("Capacity gate: reserved reason={0} reservation={1} canary={2} slots={3} capacity={4}" -f `
        $result.reason,$result.reservation_id,$result.canary_operation_id,$result.reserved_slots,$result.requested_capacity)
    return $result
}

function Bind-OperationCapacity([string]$OperationId, [string]$AuthorizationPath) {
    Invoke-OperationLedger @(
        'capacity','--operation-id',$OperationId,'--tenant-id',$TenantId,
        '--capacity-authorization',$AuthorizationPath
    )
}

function Release-CapacityReservation([string]$ResolvedConfig, [int]$RequestedActions, [string]$OwnerId, [string]$ReservationId) {
    & $python $proxyCapacityGateScript --config $ResolvedConfig --tenant-id $TenantId `
        --requested-actions $RequestedActions --release-reservation-id $ReservationId `
        --reservation-owner $OwnerId *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Capacity reservation release failed.' }
}

function Initialize-CrawlOperation([string]$Mode) {
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    $entropy = [Guid]::NewGuid().ToString('N').Substring(0, 10)
    $script:PendingRunId = "run-control-${stamp}-${PID}-${entropy}"
    $script:PendingOperationId = "op-control-${stamp}-${PID}-${entropy}"
    $script:PendingOperationStarted = $false
    $script:PendingOperationFinished = $false
    $script:PendingOperationStage = 'configuration'
    Ensure-Credentials
    Ensure-CredentialGeneration
    Ensure-OperationLedgerSchema
    Start-Operation $script:PendingOperationId $Mode $script:PendingRunId
    $script:PendingOperationStarted = $true
}

function Start-Crawl([string]$Mode, [int]$ActionLimit, [string]$ResolvedManifest, [string]$ResolvedConfig, [string]$ResolvedOutput, [string]$WorkerLock, [string]$ConsoleLock) {
    $script:PendingOperationStage = 'configuration'
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
    $script:PendingOperationStage = 'lock'
    Acquire-WorkerMutex
    try {
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    $entropy = [Guid]::NewGuid().ToString('N').Substring(0, 10)
    $runId = if ($script:PendingRunId) { $script:PendingRunId } else { "run-control-${stamp}-${PID}-${entropy}" }
    $operationId = if ($script:PendingOperationId) { $script:PendingOperationId } else { "op-control-${stamp}-${PID}-${entropy}" }
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
    $capacityAuthorizationPath = Join-Path $runDir 'capacity.authorization.json'
    $startedAt = [DateTime]::UtcNow
    $controllerStartTime = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('o')
    $runLedgerStarted = $false
    $runLedgerFinished = $false
    $operationStarted = $script:PendingOperationStarted
    $operationFinished = $false
    $operationStage = 'credentials'
    $capacityAuthorization = $null
    Reserve-Worker $WorkerLock $runId $Mode $ActionLimit
    $process = $null
    try {
        Ensure-Credentials
        if (-not $operationStarted) {
            Ensure-OperationLedgerSchema
            Start-Operation $operationId $Mode $runId
            $operationStarted = $true
        }
        $operationStage = 'console'
        Ensure-Console $ConsoleLock
        $operationStage = 'preflight'
        Write-Host "Preflight: tenant=$TenantId mode=$Mode limit=$ActionLimit"
        Mark-OperationPreflight $operationId 'running' 0 $null
        $preflightStarted = [DateTime]::UtcNow
        $preflightOutput = & $python $preflightScript --manifest $ResolvedManifest --config $ResolvedConfig `
            --backend postgres --dsn-env AMAZON_US_POSTGRES_DSN --require-live --probe-target-url $nonAmazonCanaryUrl
        $preflightExit = $LASTEXITCODE
        $preflightDurationMs = [Math]::Round(([DateTime]::UtcNow - $preflightStarted).TotalMilliseconds, 1)
        $preflightOutput | Out-String | Set-Content -LiteralPath $preflightLog -Encoding UTF8
        $preflightOutput | ForEach-Object { Write-Host $_ }
        try { $preflightResult = ($preflightOutput | Out-String) | ConvertFrom-Json }
        catch { $preflightResult = $null }
        $preflightErrorClass = Get-PreflightErrorClass $preflightResult
        $preflightStatus = if ($preflightExit -eq 0) { 'succeeded' } else { 'failed' }
        Mark-OperationPreflight $operationId $preflightStatus $preflightDurationMs $preflightErrorClass
        if ($preflightExit -ne 0) { throw "Preflight failed with exit code $preflightExit" }
        $operationStage = 'capacity_gate'
        $reservationId = "capacity-${runId}"
        $capacityAuthorization = Invoke-CapacityGate $ResolvedConfig $ActionLimit $workerId $reservationId
        Write-JsonAtomic $capacityAuthorization $capacityAuthorizationPath
        Bind-OperationCapacity $operationId $capacityAuthorizationPath
        $operationStage = 'collection_ledger'
        Start-RunLedger $runId $Mode $ActionLimit $workerId $operationId
        $runLedgerStarted = $true
        $operationStage = 'worker'
        $stageOnlyArgument = if ($Mode -eq 'reviews') { '--reviews-only' } else { '--product-only' }
        $workerArguments = @(
            $workerScript, '--config', $ResolvedConfig,
            '--backend', 'postgres', '--dsn-env', 'AMAZON_US_POSTGRES_DSN',
            '--tenant-id', $TenantId, '--subject-type', 'own',
            '--worker-id', $workerId, '--lease-seconds', '600',
            '--manifest', $ResolvedManifest, '--output-dir', $ResolvedOutput,
            '--run-id', $runId, '--capacity-reservation-id', $capacityAuthorization.reservation_id,
            '--live', '--once', '--limit', [string]$ActionLimit, $stageOnlyArgument
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
                worker_id = $workerId
                receipt_path = $receiptPath
                operation_id = $operationId
                capacity_authorization = $capacityAuthorization
                capacity_authorization_path = $capacityAuthorizationPath
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
            capacity_authorization = $capacityAuthorization
        }) $WorkerLock
        New-Item -ItemType File -Path $gatePath | Out-Null
        Write-Host "Worker started: pid=$($process.Id) run_id=$runId"
        Write-Host "Console: $consoleUrl"
        Write-Host "Logs: $runDir"
        while (-not $process.HasExited) {
            [IO.File]::WriteAllText($heartbeatPath, [DateTime]::UtcNow.ToString('o'))
            try {
                $run = Get-RunProjection $runId 2
                $completed = $run.items.Count
                $failed = @($run.items | Where-Object { $_.outcome -eq 'failed' }).Count
                $blocked = @($run.items | Where-Object { $_.outcome -eq 'blocked' }).Count
                Write-Host ("Progress: {0}/{1} failed={2} blocked={3}" -f $completed,$ActionLimit,$failed,$blocked)
            }
            catch { Write-Host "Progress: unavailable (counts=unknown target=$ActionLimit)" }
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
        $outcome = if ($workerExitCode -eq 0) { 'completed' } elseif ($workerExitCode -eq 3) { 'blocked' } elseif ($workerExitCode -eq 130) { 'interrupted' } else { 'failed' }
        if ($workerExitCode -eq 130) {
            $controllerExitCode = 130
            $runVerificationReason = 'controller_exited'
        }
        elseif ($Mode -in @('probe', 'reviews') -and ($workerExitCode -ne 0 -or -not $qualityGateOk)) {
            $outcome = 'quality_failed'
            $controllerExitCode = 4
        }
        elseif ($Mode -eq 'run' -and $null -ne $quality.blocked_actions -and [int]$quality.blocked_actions -gt 0) {
            $outcome = 'blocked'
            $controllerExitCode = 3
        }
        elseif ($Mode -eq 'run' -and $workerExitCode -eq 0 -and -not $completionGateOk) {
            $outcome = 'failed'
            $controllerExitCode = 4
        }
        $finishedAt = [DateTime]::UtcNow
        $observability = Get-RunObservability $capacityAuthorization $quality $ActionLimit
        Write-Host ("Final: {0}/{1} completed={2} variant={3} failed={4} blocked={5} unrequested={6} inferred={7} quality_gate_ok={8}" -f $quality.recorded_actions,$ActionLimit,$quality.completed_actions,$quality.variant_redirect_actions,$quality.failed_actions,$quality.blocked_actions,$quality.unrequested_actions,$quality.inferred_actions,$qualityGateOk)
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
            variant_redirect_actions = $quality.variant_redirect_actions
            failed_actions = $quality.failed_actions
            blocked_actions = $quality.blocked_actions
            inferred_actions = $quality.inferred_actions
            unrequested_actions = $quality.unrequested_actions
            traffic = $quality.traffic
            proxy_session_pool = if ($null -ne $finalSnapshot.run) { $finalSnapshot.run.proxy_session_pool } else { $null }
            capacity_authorization = $capacityAuthorization
            proxy_connectivity = $observability.proxy_connectivity
            amazon_business = $observability.amazon_business
            quality_gate_ok = $qualityGateOk
            completion_gate_ok = $completionGateOk
            run_verification_reason = $runVerificationReason
            termination_reason = if ($outcome -eq 'interrupted') { 'controller_exited' } else { $null }
            started_at = $startedAt.ToString('o')
            finished_at = $finishedAt.ToString('o')
            elapsed_seconds = [Math]::Round(($finishedAt - $startedAt).TotalSeconds, 2)
            stdout_log = $stdout
            stderr_log = $stderr
            preflight_log = $preflightLog
            console_url = $consoleUrl
        }
        $ledgerReason = if ($outcome -eq 'interrupted') { 'controller_exited' } else { $runVerificationReason }
        Finish-RunLedger $runId $outcome $controllerExitCode $workerExitCode $ledgerReason $receipt
        $runLedgerFinished = $true
        Write-JsonAtomic $receipt $receiptPath
        $operationStatus = if ($outcome -eq 'completed') { 'succeeded' } elseif ($outcome -eq 'blocked') { 'blocked' } elseif ($outcome -eq 'interrupted') { 'interrupted' } else { 'failed' }
        $operationFailureStage = if ($operationStatus -eq 'succeeded') { $null } else { 'worker' }
        $operationErrorClass = if ($operationStatus -eq 'succeeded') { $null } else { $outcome }
        $operationStage = 'operation_ledger'
        Finish-Operation $operationId $operationStatus $operationFailureStage $operationErrorClass $null $null $null
        $operationFinished = $true
        $script:PendingOperationFinished = $true
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
        $caughtType = $_.Exception.GetType().FullName
        $isInterrupted = $caughtType -eq 'System.Management.Automation.PipelineStoppedException'
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            try { $process.WaitForExit(5000) | Out-Null } catch { }
            $process.Refresh()
        }
        $finishedAt = [DateTime]::UtcNow
        $failureStatus = if ($isInterrupted) { 'interrupted' } else { 'failed' }
        $failureExitCode = if ($isInterrupted) { 130 } else { 2 }
        $failureReason = if ($isInterrupted) { 'controller_interrupted' } else { 'controller_exception' }
        $failedReceipt = [ordered]@{
            schema_version = 'amazon-us-control-receipt-v3'
            run_id = $runId
            worker_id = $workerId
            tenant_id = $TenantId
            command = $Mode
            requested_limit = $ActionLimit
            status = $failureStatus
            exit_code = $failureExitCode
            worker_exit_code = if ($null -ne $process -and $process.HasExited) { $process.ExitCode } else { $null }
            recorded_actions = $null
            completed_actions = $null
            variant_redirect_actions = $null
            failed_actions = $null
            blocked_actions = $null
            inferred_actions = $null
            unrequested_actions = $null
            traffic = $null
            proxy_session_pool = $null
            capacity_authorization = $capacityAuthorization
            proxy_connectivity = (Get-RunObservability $capacityAuthorization (Test-ProbeRunQuality $null $ActionLimit) $ActionLimit).proxy_connectivity
            amazon_business = (Get-RunObservability $capacityAuthorization (Test-ProbeRunQuality $null $ActionLimit) $ActionLimit).amazon_business
            quality_gate_ok = $false
            run_verification_reason = $failureReason
            termination_reason = $failureReason
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
            Finish-RunLedger $runId $failureStatus $failureExitCode $failedReceipt.worker_exit_code $failureReason $failedReceipt
            $runLedgerFinished = $true
        }
        Write-JsonAtomic $failedReceipt $receiptPath
        if ($operationStarted -and -not $operationFinished) {
            $operationErrorClass = if ($isInterrupted) { 'controller_interrupted' } else { switch ($operationStage) {
                'console' { 'console_unavailable' }
                'preflight' { if ($preflightErrorClass) { $preflightErrorClass } else { 'preflight_failed' } }
                'capacity_gate' { if ($script:PendingCapacityGateReason) { $script:PendingCapacityGateReason } else { 'capacity_gate_denied' } }
                'collection_ledger' { 'collection_ledger_error' }
                'worker' { 'worker_failed' }
                default { 'controller_error' }
            }}
            Finish-Operation $operationId $failureStatus $operationStage $operationErrorClass $null $null $null
            $operationFinished = $true
            $script:PendingOperationFinished = $true
        }
        throw
    }
    finally {
        if ($null -ne $capacityAuthorization -and $capacityAuthorization.reservation_id) {
            try {
                Release-CapacityReservation $ResolvedConfig $ActionLimit $workerId ([string]$capacityAuthorization.reservation_id)
            }
            catch { Write-Host 'WARNING: capacity reservation release will rely on TTL expiry.' }
        }
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

  .\crawler.ps1 egress
  .\crawler.ps1 canary -Limit 3
  .\crawler.ps1 probe
  .\crawler.ps1 run -Limit 10
  .\crawler.ps1 reviews -Limit 3
  .\crawler.ps1 status
  .\crawler.ps1 console
  .\crawler.ps1 stop
  .\crawler.ps1 stop -All

egress records the legacy independent proxy health operation. canary tests every planned session against a non-Amazon HTTPS endpoint.
probe defaults to 3 product actions and requires a fresh matching canary with sufficient unique capacity.
run defaults to 10. reviews defaults to 3 review actions.
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
    elseif ($Command -eq 'egress') {
        if (-not (Test-Path -LiteralPath $python)) { throw 'Run setup_windows.bat first.' }
        $exitCode = Start-EgressOperation $ConfigPath
    }
    elseif ($Command -eq 'canary') {
        if (-not (Test-Path -LiteralPath $python)) { throw 'Run setup_windows.bat first.' }
        $actualLimit = if ($Limit -gt 0) { $Limit } else { 3 }
        $exitCode = Start-ProxyCanary $ConfigPath $actualLimit
    }
    else {
        if ($Command -in @('probe','run','reviews')) { Initialize-CrawlOperation $Command }
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
    if ($script:PendingOperationStarted -and -not $script:PendingOperationFinished) {
        $setupStage = if ($script:PendingOperationStage) { [string]$script:PendingOperationStage } else { 'setup' }
        try {
            Finish-Operation $script:PendingOperationId 'failed' $setupStage "${setupStage}_failed" $null $null $null
            $script:PendingOperationFinished = $true
        }
        catch { }
    }
    Write-Host ("ERROR: " + $_.Exception.Message) -ForegroundColor Red
    $exitCode = 2
}
finally {
    Clear-Credentials
}
exit $exitCode
