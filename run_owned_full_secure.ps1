[CmdletBinding()]
param(
    [ValidateSet('egress','canary','probe','run','reviews','status','console','stop','configure','verify-secrets','rotate',
        'agent-service','agent-status','agent-health','agent-stop','agent-get','agent-batch','agent-refresh','agent-job')]
    [string]$Mode = 'status',
    [int]$Limit = 0,
    [switch]$ConfirmLargeBatch,
    [int]$Port = 8770,
    [int]$AgentPort = 8765,
    [string[]]$Asins = @(),
    [string]$JobId,
    [string]$Reason = 'on_demand',
    [switch]$Wait,
    [int]$TimeoutSeconds = 300,
    [string]$ManifestPath = '',
    [ValidateRange(1,5400)][int]$RecoveryMaxSeconds = 5400
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$launcher = Join-Path $root 'scripts\secure_dpapi_launcher.ps1'
$credentialTool = Join-Path $root 'configure_owned_full.ps1'
$controller = Join-Path $root 'crawler.ps1'
$agentControl = Join-Path $root 'scripts\agent_service_control.ps1'
$agentClient = Join-Path $root 'scripts\amazon_collection_client.py'
$python = Join-Path $root '.venv\Scripts\python.exe'
$shell = (Get-Process -Id $PID).Path
$tenant = 'owned_us_asin_20260902_full_01'
$output = 'data\owned_us_asin_20260902_full_01'

function Get-AgentControlArguments([string]$ControlMode) {
    return @(
        '-NoProfile','-ExecutionPolicy','Bypass','-File',$agentControl,$ControlMode,
        '-TenantId',$tenant,'-ConfigPath',"$output\owned_us_full.toml",
        '-OutputDir',$output,'-Port',[string]$AgentPort
    )
}

if ($Mode -eq 'configure') {
    & $credentialTool -Mode Configure
    exit $LASTEXITCODE
}
if ($Mode -eq 'verify-secrets') {
    & $credentialTool -Mode Verify
    exit $LASTEXITCODE
}
if ($Mode -eq 'rotate') {
    & $credentialTool -Mode Rotate
    exit $LASTEXITCODE
}

if ($Mode -in @('agent-service','agent-status','agent-health','agent-stop')) {
    $controlMode = @{
        'agent-service' = 'start'; 'agent-status' = 'status';
        'agent-health' = 'health'; 'agent-stop' = 'stop'
    }[$Mode]
    $controlArguments = Get-AgentControlArguments $controlMode
    if ($Mode -eq 'agent-service') {
        & $launcher -FilePath $shell -ArgumentList $controlArguments
    }
    else {
        & $shell @controlArguments
    }
    exit $LASTEXITCODE
}

if ($Mode -in @('agent-get','agent-batch','agent-refresh','agent-job')) {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Python virtual environment is unavailable.' }
    $statusArguments = Get-AgentControlArguments 'status'
    & $shell @statusArguments *> $null
    if ($LASTEXITCODE -ne 0) {
        $ensureArguments = Get-AgentControlArguments 'start'
        & $launcher -FilePath $shell -ArgumentList $ensureArguments
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    $baseUrl = "http://127.0.0.1:$AgentPort"
    $clientArguments = [Collections.Generic.List[string]]::new()
    foreach ($value in @($agentClient,'--base-url',$baseUrl)) { $clientArguments.Add([string]$value) }
    $agentId = 'read-agent'
    switch ($Mode) {
        'agent-get' {
            if ($Asins.Count -ne 1) { throw 'agent-get requires exactly one -Asins value' }
            foreach ($value in @('get',$Asins[0])) { $clientArguments.Add([string]$value) }
        }
        'agent-batch' {
            if ($Asins.Count -lt 1 -or $Asins.Count -gt 100) { throw 'agent-batch requires 1 to 100 -Asins values' }
            $clientArguments.Add('batch'); foreach ($asin in $Asins) { $clientArguments.Add([string]$asin) }
        }
        'agent-refresh' {
            if ($Asins.Count -lt 1 -or $Asins.Count -gt 5) { throw 'agent-refresh requires 1 to 5 -Asins values' }
            $agentId = 'refresh-agent'
            $clientArguments.Add('refresh'); foreach ($asin in $Asins) { $clientArguments.Add([string]$asin) }
            foreach ($value in @('--reason',$Reason,'--timeout-seconds',[string]$TimeoutSeconds)) { $clientArguments.Add([string]$value) }
            if ($Wait) { $clientArguments.Add('--wait') }
        }
        'agent-job' {
            if ([string]::IsNullOrWhiteSpace($JobId)) { throw 'agent-job requires -JobId' }
            foreach ($value in @('job',$JobId)) { $clientArguments.Add([string]$value) }
        }
    }
    & $launcher -AgentId $agentId -FilePath $python -ArgumentList $clientArguments.ToArray()
    exit $LASTEXITCODE
}

$arguments = [Collections.Generic.List[string]]::new()
foreach ($value in @(
    '-NoProfile','-ExecutionPolicy','Bypass','-File',$controller,$Mode,
    '-TenantId',$tenant,
    '-ManifestPath',$(if ($ManifestPath) { $ManifestPath } else { "$output\manifest_1093.csv" }),
    '-ConfigPath',"$output\owned_us_full.toml",
    '-OutputDir',$output,
    '-Port',[string]$Port,'-RecoveryMaxSeconds',[string]$RecoveryMaxSeconds
)) { $arguments.Add([string]$value) }

if ($Limit -gt 0) {
    $arguments.Add('-Limit')
    $arguments.Add([string]$Limit)
}
if ($ConfirmLargeBatch) { $arguments.Add('-ConfirmLargeBatch') }
if ($Mode -eq 'stop') { $arguments.Add('-All') }

& $launcher -FilePath $shell -ArgumentList $arguments.ToArray()
exit $LASTEXITCODE
