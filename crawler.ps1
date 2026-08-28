param(
    [Parameter(Position = 0)]
    [ValidateSet('probe', 'run', 'status', 'console', 'stop', 'help')]
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
    $actual = $process.StartTime.ToUniversalTime().ToString('o')
    if ($actual -ne [string]$Lock.start_time) { return $null }
    return $process
}

function Remove-StaleLock([string]$Path) {
    $lock = Read-Lock $Path
    if ($null -ne $lock -and $null -ne (Get-VerifiedProcess $lock)) { return $lock }
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Force }
    return $null
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

function Get-ConsoleReady([string]$ExpectedRawHtml) {
    try {
        $ready = Invoke-RestMethod -Uri "${consoleUrl}/readyz" -TimeoutSec 2
        if (-not $ready.ok -or $ready.tenant_id -ne $TenantId) { return $null }
        $expected = (Resolve-Path -LiteralPath $ExpectedRawHtml).Path
        if ([string]$ready.raw_html_dir -ne $expected) { return $null }
        return $ready
    }
    catch { return $null }
}

function Ensure-Console([string]$ResolvedOutput, [string]$ConsoleLock) {
    $rawHtml = Join-Path $ResolvedOutput 'raw_html'
    if (-not (Test-Path -LiteralPath $rawHtml)) { New-Item -ItemType Directory -Path $rawHtml | Out-Null }
    if ($null -ne (Get-ConsoleReady $rawHtml)) {
        Write-Host "Console ready: $consoleUrl"
        return
    }
    $liveLock = Remove-StaleLock $ConsoleLock
    if ($null -ne $liveLock) {
        for ($index = 0; $index -lt 20; $index++) {
            if ($null -ne (Get-ConsoleReady $rawHtml)) { Write-Host "Console ready: $consoleUrl"; return }
            Start-Sleep -Milliseconds 250
        }
        Write-Host 'Managed Console identity does not match; restarting it for the requested tenant.'
        Stop-Locked $ConsoleLock 'Console'
    }
    try {
        $health = Invoke-RestMethod -Uri "${consoleUrl}/healthz" -TimeoutSec 2
        if ($health.ok) { throw 'Port is occupied by an unmanaged or wrong-tenant Console. Stop it or choose another -Port.' }
    }
    catch {
        if ($_.Exception.Message -like 'Port is occupied*') { throw }
    }
    Ensure-Credentials
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
        arguments = @($consoleScript,'--tenant-id',$TenantId,'--raw-html-dir',$rawHtml,'--host','127.0.0.1','--port',[string]$Port)
        gate_path = $gatePath
        cancel_path = $cancelPath
    }) $requestPath
    $process = Start-ManagedHost $requestPath $stdout $stderr
    try {
        Write-JsonAtomic ([ordered]@{
            pid = $process.Id
            start_time = $process.StartTime.ToUniversalTime().ToString('o')
            tenant_id = $TenantId
            raw_html_dir = $rawHtml
            url = $consoleUrl
            stdout = $stdout
            stderr = $stderr
        }) $ConsoleLock
        New-Item -ItemType File -Path $gatePath | Out-Null
        for ($index = 0; $index -lt 30; $index++) {
            if ($null -ne (Get-ConsoleReady $rawHtml)) {
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

function Show-Status([string]$WorkerLock, [string]$ConsoleLock, [string]$ResolvedOutput) {
    $worker = Read-Lock $WorkerLock
    $workerProcess = Get-VerifiedProcess $worker
    if ($null -ne $workerProcess) {
        Write-Host "Worker: RUNNING pid=$($worker.pid) run_id=$($worker.run_id) command=$($worker.command)"
    }
    else { Write-Host 'Worker: stopped' }
    $console = Read-Lock $ConsoleLock
    $consoleProcess = Get-VerifiedProcess $console
    $rawHtml = Join-Path $ResolvedOutput 'raw_html'
    $ready = if (Test-Path -LiteralPath $rawHtml) { Get-ConsoleReady $rawHtml } else { $null }
    Write-Host ("Console: " + ($(if ($null -ne $ready) { "ready $consoleUrl" } elseif ($null -ne $consoleProcess) { 'process alive but not ready' } else { 'stopped or wrong tenant' })))
    if ($null -eq $ready) { return 1 }
    $overview = Invoke-RestMethod -Uri "${consoleUrl}/api/overview" -TimeoutSec 5
    Write-Host "Tenant: $($overview.tenant_id)"
    Write-Host "Progress: $($overview.progress.touched)/$($overview.progress.total) ($($overview.progress.percent)%)"
    Write-Host "Products: $($overview.progress.successful_products)"
    Write-Host ('Status: ' + (($overview.status_counts.psobject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ', '))
    $runs = Invoke-RestMethod -Uri "${consoleUrl}/api/runs?limit=3" -TimeoutSec 5
    if ($runs.items.Count -gt 0) {
        Write-Host 'Recent runs:'
        foreach ($run in $runs.items) {
            Write-Host ("  {0} actions={1} failed={2} blocked={3} ended={4}" -f `
                $run.run_id,$run.evidence_actions,$run.failed,$run.blocked,$run.ended_at)
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

function Start-Crawl([string]$Mode, [int]$ActionLimit, [string]$ResolvedManifest, [string]$ResolvedConfig, [string]$ResolvedOutput, [string]$WorkerLock, [string]$ConsoleLock) {
    if ($Mode -eq 'probe' -and ($ActionLimit -lt 1 -or $ActionLimit -gt 5)) {
        throw 'probe limit must be between 1 and 5'
    }
    if ($Mode -eq 'run' -and ($ActionLimit -lt 1 -or $ActionLimit -gt 500)) {
        throw 'run limit must be between 1 and 500'
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
    $startedAt = [DateTime]::UtcNow
    Reserve-Worker $WorkerLock $runId $Mode $ActionLimit
    $process = $null
    try {
        Ensure-Credentials
        Ensure-Console $ResolvedOutput $ConsoleLock
        Write-Host "Preflight: tenant=$TenantId mode=$Mode limit=$ActionLimit"
        $preflightOutput = & $python $preflightScript --manifest $ResolvedManifest --config $ResolvedConfig `
            --backend postgres --dsn-env AMAZON_US_POSTGRES_DSN --require-live
        $preflightExit = $LASTEXITCODE
        $preflightOutput | Out-String | Set-Content -LiteralPath $preflightLog -Encoding UTF8
        $preflightOutput | ForEach-Object { Write-Host $_ }
        if ($preflightExit -ne 0) { throw "Preflight failed with exit code $preflightExit" }
        $workerArguments = @(
            $workerScript, '--config', $ResolvedConfig,
            '--backend', 'postgres', '--dsn-env', 'AMAZON_US_POSTGRES_DSN',
            '--tenant-id', $TenantId, '--subject-type', 'own',
            '--worker-id', $workerId, '--lease-seconds', '600',
            '--manifest', $ResolvedManifest, '--output-dir', $ResolvedOutput,
            '--run-id', $runId, '--live', '--once', '--limit', [string]$ActionLimit, '--product-only'
        )
        Write-JsonAtomic ([ordered]@{
            python = $python
            working_directory = $projectRoot
            arguments = $workerArguments
            gate_path = $gatePath
            cancel_path = $cancelPath
        }) $requestPath
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
            try {
                $run = Invoke-RestMethod -Uri "${consoleUrl}/api/runs/${runId}" -TimeoutSec 2
                $completed = $run.items.Count
                $failed = @($run.items | Where-Object { $_.outcome -eq 'failed' }).Count
                $blocked = @($run.items | Where-Object { $_.outcome -eq 'blocked' }).Count
                Write-Host ("Progress: {0}/{1} failed={2} blocked={3}" -f $completed,$ActionLimit,$failed,$blocked)
            }
            catch { Write-Host "Progress: waiting for first action evidence (target=$ActionLimit)" }
            Start-Sleep -Seconds 5
            $process.Refresh()
        }
        $exitCode = $process.ExitCode
        $finishedAt = [DateTime]::UtcNow
        $outcome = if ($exitCode -eq 0) { 'completed' } elseif ($exitCode -eq 3) { 'blocked' } else { 'failed' }
        $receipt = [ordered]@{
            schema_version = 'amazon-us-control-receipt-v1'
            run_id = $runId
            worker_id = $workerId
            tenant_id = $TenantId
            command = $Mode
            requested_limit = $ActionLimit
            status = $outcome
            exit_code = $exitCode
            started_at = $startedAt.ToString('o')
            finished_at = $finishedAt.ToString('o')
            elapsed_seconds = [Math]::Round(($finishedAt - $startedAt).TotalSeconds, 2)
            stdout_log = $stdout
            stderr_log = $stderr
            preflight_log = $preflightLog
            console_url = $consoleUrl
        }
        Write-JsonAtomic $receipt $receiptPath
        Write-Host "Worker finished: status=$outcome exit=$exitCode receipt=$receiptPath"
        if (Test-Path -LiteralPath $stdout) {
            Get-Content -LiteralPath $stdout -Tail 40 | ForEach-Object { Write-Host $_ }
        }
        if ((Test-Path -LiteralPath $stderr) -and (Get-Item -LiteralPath $stderr).Length -gt 0) {
            Write-Host 'Worker stderr:'
            Get-Content -LiteralPath $stderr -Tail 40 | ForEach-Object { Write-Host $_ }
        }
        return $exitCode
    }
    catch {
        $finishedAt = [DateTime]::UtcNow
        Write-JsonAtomic ([ordered]@{
            schema_version = 'amazon-us-control-receipt-v1'
            run_id = $runId
            worker_id = $workerId
            tenant_id = $TenantId
            command = $Mode
            requested_limit = $ActionLimit
            status = 'failed_before_completion'
            exit_code = 2
            error = $_.Exception.Message
            started_at = $startedAt.ToString('o')
            finished_at = $finishedAt.ToString('o')
            elapsed_seconds = [Math]::Round(($finishedAt - $startedAt).TotalSeconds, 2)
            stdout_log = $stdout
            stderr_log = $stderr
            preflight_log = $preflightLog
            console_url = $consoleUrl
        }) $receiptPath
        throw
    }
    finally {
        $lock = Read-Lock $WorkerLock
        $processStillRunning = $null -ne $process -and -not $process.HasExited
        if (-not $processStillRunning -and $null -ne $lock -and $lock.run_id -eq $runId) {
            Remove-Item -LiteralPath $WorkerLock -Force -ErrorAction SilentlyContinue
        }
        if (-not $processStillRunning) {
            Remove-Item -LiteralPath $requestPath,$gatePath,$cancelPath -Force -ErrorAction SilentlyContinue
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

function Show-Help {
    Write-Host @'
Amazon crawler control

  .\crawler.ps1 probe
  .\crawler.ps1 run -Limit 10
  .\crawler.ps1 status
  .\crawler.ps1 console
  .\crawler.ps1 stop
  .\crawler.ps1 stop -All

probe defaults to 3 product actions. run defaults to 10.
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
    $resolvedOutput = Resolve-ProjectPath $OutputDir
    $controlDir = Join-Path $resolvedOutput 'control'
    New-Item -ItemType Directory -Path $controlDir -Force | Out-Null
    $workerLock = Join-Path $controlDir '.worker.lock.json'
    $consoleLock = Join-Path $controlDir '.console.lock.json'
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
        'status' { $exitCode = Show-Status $workerLock $consoleLock $resolvedOutput }
        'console' {
            if (-not (Test-Path -LiteralPath $python)) { throw 'Run setup_windows.bat first.' }
            Ensure-Console $resolvedOutput $consoleLock
            Write-Host "Open: $consoleUrl"
        }
        'stop' {
            Stop-Locked $workerLock 'Worker'
            if ($All) { Stop-Locked $consoleLock 'Console' }
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
