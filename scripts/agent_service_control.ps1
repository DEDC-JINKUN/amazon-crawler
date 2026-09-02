[CmdletBinding()]
param(
    [ValidateSet('start','status','health','stop')][string]$Mode = 'status',
    [string]$TenantId = 'owned_us_asin_20260902_full_01',
    [string]$ConfigPath = 'data\owned_us_asin_20260902_full_01\owned_us_full.toml',
    [string]$OutputDir = 'data\owned_us_asin_20260902_full_01',
    [int]$Port = 8765
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$hostScript = Join-Path $PSScriptRoot 'crawler_process_host.py'
$serviceScript = Join-Path $PSScriptRoot 'agent_collection_service.py'
$workerScript = Join-Path $PSScriptRoot 'amazon_us_worker.py'
$proxyPoolScript = Join-Path $PSScriptRoot 'proxy_session_pool.py'
$collectionApiScript = Join-Path $PSScriptRoot 'collection_api.py'
$collectionStorageScript = Join-Path $PSScriptRoot 'collection_storage.py'
$postgresStorageScript = Join-Path $PSScriptRoot 'postgres_worker_storage.py'
$controlDir = Join-Path $root 'data\agent_service_control'
$lockPath = Join-Path $controlDir '.agent-service.lock.json'
$requestPath = Join-Path $controlDir 'agent-service.request.json'
$gatePath = Join-Path $controlDir 'agent-service.start.gate'
$cancelPath = Join-Path $controlDir 'agent-service.cancel'
$stdoutPath = Join-Path $controlDir 'agent-service.stdout.log'
$stderrPath = Join-Path $controlDir 'agent-service.stderr.log'
$url = "http://127.0.0.1:$Port"

function Resolve-ProjectPath([string]$Value) {
    $candidate = if ([IO.Path]::IsPathRooted($Value)) { [IO.Path]::GetFullPath($Value) } else { [IO.Path]::GetFullPath((Join-Path $root $Value)) }
    $prefix = [IO.Path]::GetFullPath($root).TrimEnd('\') + '\'
    if ($candidate -ne [IO.Path]::GetFullPath($root) -and -not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'agent_service_path_outside_project'
    }
    return $candidate
}

function Get-AgentRuntimeFingerprint {
    $resolvedConfig = Resolve-ProjectPath $ConfigPath
    $runtimeFiles = @(
        $serviceScript,
        $workerScript,
        $proxyPoolScript,
        $collectionApiScript,
        $collectionStorageScript,
        $postgresStorageScript,
        $resolvedConfig
    )
    $parts = foreach ($path in $runtimeFiles) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "agent_service_file_missing:$path"
        }
        $normalized = [IO.Path]::GetFullPath($path).ToLowerInvariant()
        $fileHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        "$normalized`n$fileHash"
    }
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
    if ([string]$Lock.tenant_id -ne $TenantId -or [string]$Lock.url -ne $url) { return $false }
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

function Wait-AgentOffline([int]$TimeoutSeconds = 10) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if (-not (Get-Live).Ok) { return $true }
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
$lock = Read-Lock
$hostProcess = Get-VerifiedHost $lock

if ($Mode -eq 'health') {
    $ready = Get-Ready
    if (-not $ready.Ok) { Write-Error 'Agent service is not ready.'; exit 1 }
    Write-Output $ready.Body
    exit 0
}

if ($Mode -eq 'status') {
    $live = Get-Live
    if ($null -eq $hostProcess) {
        if ($live.Ok) { Write-Error 'Agent service has an unmanaged listener.'; exit 2 }
        Write-Host 'Agent service: STOPPED'; exit 1
    }
    if (-not (Test-CurrentRuntime $lock)) { Write-Error 'Agent service runtime identity is stale.'; exit 2 }
    if (-not $live.Ok) { Write-Error 'Agent service process is running but not live.'; exit 2 }
    Write-Host "Agent service: RUNNING pid=$($hostProcess.Id) tenant=$TenantId url=$url"
    Write-Output $live.Body
    exit 0
}

if ($Mode -eq 'stop') {
    if ($null -eq $hostProcess) {
        if ((Get-Live).Ok) { throw 'agent_service_unmanaged_listener' }
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
    if ($live.Ok -and (Test-CurrentRuntime $lock)) { Write-Host "Agent service: already running pid=$($hostProcess.Id) url=$url"; exit 0 }
    if ($live.Ok) {
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

[IO.Directory]::CreateDirectory($controlDir) | Out-Null
Remove-ControlFiles
$resolvedConfig = Resolve-ProjectPath $ConfigPath
$resolvedOutput = Resolve-ProjectPath $OutputDir
foreach ($required in @($python,$hostScript,$serviceScript,$workerScript,$proxyPoolScript,$collectionApiScript,$collectionStorageScript,$postgresStorageScript,$resolvedConfig)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "agent_service_file_missing:$required" }
}
[IO.Directory]::CreateDirectory($resolvedOutput) | Out-Null

$request = [ordered]@{
    python = $python
    arguments = @(
        $serviceScript,'--config',$resolvedConfig,'--output-dir',$resolvedOutput,
        '--tenant-id',$TenantId,'--subject-type','own','--host','127.0.0.1','--port',[string]$Port
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
    if ($ready.Ok) {
        Write-Host "Agent service: RUNNING pid=$($hostProcess.Id) tenant=$TenantId url=$url"
        exit 0
    }
} while ([DateTime]::UtcNow -lt $deadline)

if (-not $hostProcess.HasExited) { Stop-Process -Id $hostProcess.Id -Force -ErrorAction SilentlyContinue }
Remove-ControlFiles
throw "agent_service_start_failed; inspect $stderrPath"
