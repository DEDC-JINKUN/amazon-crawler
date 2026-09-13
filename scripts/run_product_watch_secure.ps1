[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [int]$Iterations = 1,
    [double]$IntervalSeconds = 300
)

$ErrorActionPreference = 'Stop'
if ($Iterations -lt 1 -or $IntervalSeconds -le 0) { throw 'watch_schedule_limits_invalid' }
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$launcher = Join-Path $PSScriptRoot 'secure_dpapi_launcher.ps1'
$script = Join-Path $PSScriptRoot 'run_product_watch.py'
$config = if ([IO.Path]::IsPathRooted($ConfigPath)) { [IO.Path]::GetFullPath($ConfigPath) } else { [IO.Path]::GetFullPath((Join-Path $root $ConfigPath)) }
$prefix = [IO.Path]::GetFullPath($root).TrimEnd('\') + '\'
if (-not $config.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'watch_config_outside_project' }
foreach ($file in @($python,$launcher,$script,$config)) { if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "watch_required_file_missing:$file" } }

# This is a bounded runner suitable for a Windows scheduled task. It only reads
# committed database rows and outbox state; collection remains in its own worker.
& $launcher -SecretNames @('AMAZON_US_POSTGRES_DSN') -FilePath $python -ArgumentList @(
    $script, '--config', $config, '--iterations', [string]$Iterations, '--interval-seconds', [string]$IntervalSeconds
)
exit $LASTEXITCODE
