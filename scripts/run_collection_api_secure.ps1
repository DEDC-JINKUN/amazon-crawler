[CmdletBinding()]
param(
    [string]$TenantId = 'owned_us_asin_20260902_full_01',
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$launcher = Join-Path $PSScriptRoot 'secure_dpapi_launcher.ps1'
if (-not (Test-Path -LiteralPath $python)) { throw 'Python virtual environment is unavailable.' }
& $launcher -SecretNames @('AMAZON_US_POSTGRES_DSN','AMAZON_COLLECTION_API_KEY') -FilePath $python -ArgumentList @(
    (Join-Path $PSScriptRoot 'collection_api.py'), '--backend', 'postgres', '--dsn-env', 'AMAZON_US_POSTGRES_DSN',
    '--tenant-id', $TenantId, '--host', '127.0.0.1', '--port', [string]$Port, '--require-api-key'
)
exit $LASTEXITCODE
