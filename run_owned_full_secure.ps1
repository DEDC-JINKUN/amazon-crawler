[CmdletBinding()]
param(
    [ValidateSet('egress','probe','run','reviews','status','console','stop')][string]$Mode = 'status',
    [int]$Limit = 0,
    [switch]$ConfirmLargeBatch,
    [int]$Port = 8770
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$launcher = Join-Path $root 'scripts\secure_dpapi_launcher.ps1'
$controller = Join-Path $root 'crawler.ps1'
$shell = (Get-Process -Id $PID).Path
$tenant = 'owned_us_asin_20260902_full_01'
$output = 'data\owned_us_asin_20260902_full_01'

$arguments = [Collections.Generic.List[string]]::new()
foreach ($value in @(
    '-NoProfile','-ExecutionPolicy','Bypass','-File',$controller,$Mode,
    '-TenantId',$tenant,
    '-ManifestPath',"$output\manifest_1093.csv",
    '-ConfigPath',"$output\owned_us_full.toml",
    '-OutputDir',$output,
    '-Port',[string]$Port
)) { $arguments.Add([string]$value) }

if ($Limit -gt 0) {
    $arguments.Add('-Limit')
    $arguments.Add([string]$Limit)
}
if ($ConfirmLargeBatch) { $arguments.Add('-ConfirmLargeBatch') }
if ($Mode -eq 'stop') { $arguments.Add('-All') }

& $launcher -FilePath $shell -ArgumentList $arguments.ToArray()
exit $LASTEXITCODE
