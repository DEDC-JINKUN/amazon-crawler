param(
    [Parameter(Mandatory = $true)]
    [string]$TenantId,
    [Parameter(Mandatory = $true)]
    [string]$RawHtmlDir,
    [int]$Port = 8770,
    [switch]$RequireApiKey
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$consoleScript = Join-Path $projectRoot 'scripts\collection_console.py'
$launcher = Join-Path $projectRoot 'scripts\secure_dpapi_launcher.ps1'
$resolvedRawHtml = (Resolve-Path (Join-Path $projectRoot $RawHtmlDir)).Path
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Python virtual environment is unavailable.' }
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw 'DPAPI secure launcher is unavailable.' }

$secretNames = @('AMAZON_US_POSTGRES_DSN')
$arguments = @(
    $consoleScript,
    '--tenant-id', $TenantId,
    '--raw-html-dir', $resolvedRawHtml,
    '--host', '127.0.0.1',
    '--port', $Port
)
if ($RequireApiKey) {
    $secretNames += 'AMAZON_COLLECTION_API_KEY'
    $arguments += '--require-api-key'
}
# Secrets exist only in the child process. Console issues an HttpOnly local
# session, so neither the PowerShell user nor browser has to copy the main key.
& $launcher -SecretNames $secretNames -FilePath $python -ArgumentList $arguments
exit $LASTEXITCODE
