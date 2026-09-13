[CmdletBinding()]
param(
    [ValidateSet('start','status','health','stop')][string]$Mode = 'status',
    [string]$TenantId = 'owned_us_asin_20260902_full_01',
    [string]$ConfigPath = 'data\owned_us_asin_20260902_full_01\owned_us_full.toml',
    [string]$OutputDir = 'data\owned_us_asin_20260902_full_01',
    [int]$Port = 8765,
    [int]$ConsolePort = 8770,
    [string]$ConsoleTenantId = 'amazon_us_main',
    [string]$OperatorBaseUrl = 'http://127.0.0.1:8772'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$hostScript = Join-Path $PSScriptRoot 'crawler_process_host.py'
$serviceScript = Join-Path $PSScriptRoot 'agent_collection_service.py'
$workerScript = Join-Path $PSScriptRoot 'amazon_us_worker.py'
$proxyPoolScript = Join-Path $PSScriptRoot 'proxy_session_pool.py'
$proxyCanaryScript = Join-Path $PSScriptRoot 'proxy_canary.py'
$proxyCapacityGateScript = Join-Path $PSScriptRoot 'proxy_capacity_gate.py'
$proxyConnectRelayScript = Join-Path $PSScriptRoot 'proxy_connect_relay.py'
$collectionApiScript = Join-Path $PSScriptRoot 'collection_api.py'
$collectionStorageScript = Join-Path $PSScriptRoot 'collection_storage.py'
$postgresStorageScript = Join-Path $PSScriptRoot 'postgres_worker_storage.py'
$consoleScript = Join-Path $PSScriptRoot 'collection_console.py'
$consoleIndex = Join-Path $root 'console\index.html'
$consoleApp = Join-Path $root 'console\app.js'
$consoleStyles = Join-Path $root 'console\styles.css'
$credentialVault = Join-Path $root 'data\secure\amazon_us.secrets.dpapi'
$controlDir = Join-Path $root 'data\agent_service_control'
$lockPath = Join-Path $controlDir '.agent-service.lock.json'
$requestPath = Join-Path $controlDir 'agent-service.request.json'
$gatePath = Join-Path $controlDir 'agent-service.start.gate'
$cancelPath = Join-Path $controlDir 'agent-service.cancel'
$stdoutPath = Join-Path $controlDir 'agent-service.stdout.log'
$stderrPath = Join-Path $controlDir 'agent-service.stderr.log'
$url = "http://127.0.0.1:$Port"
$consoleUrl = "http://127.0.0.1:$ConsolePort"

function Resolve-ProjectPath([string]$Value) {
    $candidate = if ([IO.Path]::IsPathRooted($Value)) { [IO.Path]::GetFullPath($Value) } else { [IO.Path]::GetFullPath((Join-Path $root $Value)) }
    $prefix = [IO.Path]::GetFullPath($root).TrimEnd('\') + '\'
    if ($candidate -ne [IO.Path]::GetFullPath($root) -and -not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'agent_service_path_outside_project'
    }
    return $candidate
}

function Get-CredentialGeneration {
    if (-not (Test-Path -LiteralPath $credentialVault -PathType Leaf)) {
        throw 'agent_service_credential_vault_missing'
    }
    $encoded = ([IO.File]::ReadAllText((Resolve-Path -LiteralPath $credentialVault))).Trim()
    if ($encoded.StartsWith('{')) {
        $envelope = $encoded | ConvertFrom-Json
        if ($envelope.PSObject.Properties.Name -contains 'credential_generation') {
            $generation = [string]$envelope.credential_generation
            if ($generation -match '^[A-Za-z0-9_.:-]{8,100}$') { return $generation }
        }
        $encoded = [string]$envelope.ciphertext
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::ASCII.GetBytes($encoded))).Replace('-', '').ToLowerInvariant()
        return 'legacy-' + $hash.Substring(0, 32)
    }
    finally { $sha.Dispose() }
}

function Get-AgentRuntimeFingerprint {
    $resolvedConfig = Resolve-ProjectPath $ConfigPath
    $runtimeFiles = @(
        $serviceScript,
        $workerScript,
        $proxyPoolScript,
        $proxyCanaryScript,
        $proxyCapacityGateScript,
        $proxyConnectRelayScript,
        (Join-Path $PSScriptRoot 'recovery_scheduler.py'),
        (Join-Path $PSScriptRoot 'recovery_consumer.py'),
        (Join-Path $PSScriptRoot 'http_deadline.py'),
        (Join-Path $PSScriptRoot 'proxy_tunnel_auth.py'),
        $collectionApiScript,
        $collectionStorageScript,
        $postgresStorageScript,
        $consoleScript,
        $consoleIndex,
        $consoleApp,
        $consoleStyles,
        (Join-Path $PSScriptRoot 'product_watch\pipeline.py'),
        (Join-Path $PSScriptRoot 'product_watch\delivery_repository.py'),
        (Join-Path $PSScriptRoot 'product_watch\reporting.py'),
        (Join-Path $PSScriptRoot 'product_watch\delivery.py'),
        $resolvedConfig
    )
    $parts = foreach ($path in $runtimeFiles) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "agent_service_file_missing:$path"
        }
        $normalized = [IO.Path]::GetFullPath($path).ToLowerInvariant()
        $sha = [Security.Cryptography.SHA256]::Create()
        $stream = [IO.File]::OpenRead($path)
        try { $fileHash = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
        finally { $stream.Dispose(); $sha.Dispose() }
        "$normalized`n$fileHash"
    }
    $parts += "credential_generation`n$(Get-CredentialGeneration)"
    $parts += "console_tenant_id`n$ConsoleTenantId"
    $parts += "operator_base_url`n$OperatorBaseUrl"
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($parts -join "`n"))
        return [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Read-Lock {
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { return $null }
    try { return Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json } catch { return $null }
}

function Get-VerifiedHost($Lock) {
    if ($null -eq $Lock -or -not $Lock.pid -or -not $Lock.start_time) { return $null }
    try { $process = Get-Process -Id ([int]$Lock.pid) -ErrorAction Stop } catch { return $null }
    $lockStart = $Lock.start_time
    $expected = if ($lockStart -is [DateTime]) {
        $lockStart.ToUniversalTime().Ticks
    }
    else {
        ([DateTimeOffset]::Parse([string]$lockStart)).UtcDateTime.Ticks
    }
    $actual = ([DateTimeOffset]$process.StartTime).UtcDateTime.Ticks
    if ([Math]::Abs($actual - $expected) -gt 10000000) { return $null }
    return $process
}

function Test-CurrentRuntime($Lock) {
    if ($null -eq $Lock -or [string]$Lock.schema -ne 'amazon-us-agent-service-lock-v1') { return $false }
    if ([string]$Lock.tenant_id -ne $TenantId -or [string]$Lock.url -ne $url -or [string]$Lock.console_url -ne $consoleUrl) { return $false }
    try { $expectedFingerprint = Get-AgentRuntimeFingerprint } catch { return $false }
    return [string]$Lock.runtime_fingerprint -eq $expectedFingerprint
}

function Get-Ready {
    try {
        $response = Invoke-WebRequest -Uri "$url/readyz" -UseBasicParsing -TimeoutSec 3
        return [pscustomobject]@{ Ok = ($response.StatusCode -eq 200); Body = [string]$response.Content }
    }
    catch { return [pscustomobject]@{ Ok = $false; Body = $null } }
}

function Get-Live {
    try {
        $response = Invoke-WebRequest -Uri "$url/healthz" -UseBasicParsing -TimeoutSec 3
        return [pscustomobject]@{ Ok = ($response.StatusCode -eq 200); Body = [string]$response.Content }
    }
    catch { return [pscustomobject]@{ Ok = $false; Body = $null } }
}

function Get-ConsoleLive {
    try {
        $response = Invoke-WebRequest -Uri $consoleUrl -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -eq 200
    }
    catch { return $false }
}

function Get-ListenerOwner([int]$ListenerPort) {
    $connection = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $ListenerPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $connection) { return $null }
    return [int]$connection.OwningProcess
}

function Wait-AgentOffline([int]$TimeoutSeconds = 10) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if (-not (Get-Live).Ok -and -not (Get-ConsoleLive)) { return $true }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Remove-ControlFiles {
    foreach ($path in @($lockPath,$requestPath,$gatePath,$cancelPath)) {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
}

if ($Port -lt 1 -or $Port -gt 65535) { throw 'agent_service_invalid_port' }
if ($ConsolePort -lt 1 -or $ConsolePort -gt 65535 -or $ConsolePort -eq $Port) { throw 'agent_service_invalid_console_port' }
$operatorUri = [Uri]$OperatorBaseUrl
if ($operatorUri.Scheme -ne 'http' -or $operatorUri.Host -ne '127.0.0.1' -or $operatorUri.AbsolutePath -ne '/' -or $operatorUri.Query -or $operatorUri.Fragment) { throw 'agent_service_invalid_operator_base_url' }
$lock = Read-Lock
$hostProcess = Get-VerifiedHost $lock

if ($Mode -eq 'health') {
    $ready = Get-Ready
    if (-not $ready.Ok -or -not (Get-ConsoleLive)) { Write-Error 'Agent service or primary console is not ready.'; exit 1 }
    Write-Output $ready.Body
    exit 0
}

if ($Mode -eq 'status') {
    $live = Get-Live
    if ($null -eq $hostProcess) {
        if ($live.Ok) { Write-Error 'Agent service has an unmanaged listener.'; exit 2 }
        if (Get-ConsoleLive) { Write-Error 'Agent service primary console has an unmanaged listener.'; exit 2 }
        Write-Host 'Agent service: STOPPED'; exit 1
    }
    if ((Get-ListenerOwner $ConsolePort) -ne $hostProcess.Id) { Write-Error 'Agent service primary console has an unmanaged listener.'; exit 2 }
    if (-not (Test-CurrentRuntime $lock)) { Write-Error 'Agent service runtime identity is stale.'; exit 2 }
    if (-not $live.Ok) { Write-Error 'Agent service process is running but not live.'; exit 2 }
    if (-not (Get-ConsoleLive)) { Write-Error 'Agent service primary console is not live.'; exit 2 }
    Write-Host "Agent service: RUNNING pid=$($hostProcess.Id) tenant=$TenantId url=$url"
    Write-Output $live.Body
    exit 0
}

if ($Mode -eq 'stop') {
    if ($null -eq $hostProcess) {
        if ((Get-Live).Ok) { throw 'agent_service_unmanaged_listener' }
        if (Get-ConsoleLive) { throw 'agent_service_primary_console_unmanaged_listener' }
        Remove-ControlFiles
        Write-Host 'Agent service: already stopped'
        exit 0
    }
    Stop-Process -Id $hostProcess.Id -Force
    try { $hostProcess.WaitForExit(10000) | Out-Null } catch { }
    if (-not (Wait-AgentOffline)) { throw 'agent_service_stop_timeout' }
    Remove-ControlFiles
    Write-Host 'Agent service: STOPPED'
    exit 0
}

if ($null -ne $hostProcess) {
    $live = Get-Live
    $consoleLive = Get-ConsoleLive
    $consoleOwner = Get-ListenerOwner $ConsolePort
    if ($consoleLive -and $consoleOwner -ne $hostProcess.Id) { throw 'agent service primary console has an unmanaged listener' }
    if ($live.Ok -and (Test-CurrentRuntime $lock) -and $consoleLive) { Write-Host "Agent service: already running pid=$($hostProcess.Id) url=$url console=$consoleUrl"; exit 0 }
    if ($live.Ok) { $hasLiveManagedEndpoint = $true } else { $hasLiveManagedEndpoint = $consoleLive }
    if ($hasLiveManagedEndpoint) {
        Write-Host "Agent service: restarting stale controlled runtime pid=$($hostProcess.Id)"
        Stop-Process -Id $hostProcess.Id -Force
        try { $hostProcess.WaitForExit(10000) | Out-Null } catch { }
        if (-not (Wait-AgentOffline)) { throw 'agent_service_stale_runtime_stop_timeout' }
        Remove-ControlFiles
        $hostProcess = $null
    }
    else {
        throw 'agent_service_lock_is_live_but_not_ready'
    }
}
if ((Get-Live).Ok) { throw 'agent_service_port_has_unmanaged_listener' }
if (Get-ConsoleLive) { throw 'agent service primary console has an unmanaged listener' }

[IO.Directory]::CreateDirectory($controlDir) | Out-Null
Remove-ControlFiles
$resolvedConfig = Resolve-ProjectPath $ConfigPath
$resolvedOutput = Resolve-ProjectPath $OutputDir
foreach ($required in @($python,$hostScript,$serviceScript,$workerScript,$proxyPoolScript,$proxyCanaryScript,$proxyCapacityGateScript,$collectionApiScript,$collectionStorageScript,$postgresStorageScript,$consoleScript,$consoleIndex,$consoleApp,$consoleStyles,$resolvedConfig)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "agent_service_file_missing:$required" }
}
[IO.Directory]::CreateDirectory($resolvedOutput) | Out-Null

$request = [ordered]@{
    python = $python
    arguments = @(
        $serviceScript,'--config',$resolvedConfig,'--output-dir',$resolvedOutput,
        '--tenant-id',$TenantId,'--subject-type','own','--host','127.0.0.1','--port',[string]$Port,
        '--console-port',[string]$ConsolePort,'--console-tenant-id',$ConsoleTenantId,'--operator-base-url',$OperatorBaseUrl
    )
    working_directory = $root
    gate_path = $gatePath
    cancel_path = $cancelPath
}
$request | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $requestPath -Encoding UTF8
$hostArgumentLine = '"{0}" --request "{1}"' -f $hostScript,$requestPath
$hostProcess = Start-Process -FilePath $python -ArgumentList $hostArgumentLine `
    -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath -PassThru
$hostProcess.Refresh()
$fingerprint = Get-AgentRuntimeFingerprint
$newLock = [ordered]@{
    schema = 'amazon-us-agent-service-lock-v1'
    pid = $hostProcess.Id
    start_time = $hostProcess.StartTime.ToUniversalTime().ToString('o')
    tenant_id = $TenantId
    url = $url
    console_url = $consoleUrl
    runtime_fingerprint = $fingerprint
    stdout = $stdoutPath
    stderr = $stderrPath
}
$newLock | ConvertTo-Json | Set-Content -LiteralPath $lockPath -Encoding UTF8
[IO.File]::WriteAllText($gatePath, 'start')

$deadline = [DateTime]::UtcNow.AddSeconds(30)
do {
    Start-Sleep -Milliseconds 250
    if ($hostProcess.HasExited) { break }
    $ready = Get-Ready
    if ($ready.Ok -and (Get-ConsoleLive)) {
        Write-Host "Agent service: RUNNING pid=$($hostProcess.Id) tenant=$TenantId url=$url console=$consoleUrl"
        exit 0
    }
} while ([DateTime]::UtcNow -lt $deadline)

if (-not $hostProcess.HasExited) { Stop-Process -Id $hostProcess.Id -Force -ErrorAction SilentlyContinue }
Remove-ControlFiles
throw "agent_service_start_failed; inspect $stderrPath"
