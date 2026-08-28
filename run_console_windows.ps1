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
$resolvedRawHtml = (Resolve-Path (Join-Path $projectRoot $RawHtmlDir)).Path

$env:AMAZON_US_POSTGRES_DSN = 'host=127.0.0.1 port=5432 dbname=postgres user=postgres'
$securePassword = Read-Host 'Enter local PostgreSQL postgres password (input hidden)' -AsSecureString
$passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
try {
    $env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    $securePassword.Dispose()
}

try {
    $arguments = @(
        $consoleScript,
        '--tenant-id', $TenantId,
        '--raw-html-dir', $resolvedRawHtml,
        '--host', '127.0.0.1',
        '--port', $Port
    )
    if ($RequireApiKey) {
        $arguments += '--require-api-key'
    }
    & $python @arguments
}
finally {
    Remove-Item Env:PGPASSWORD,Env:AMAZON_US_POSTGRES_DSN -ErrorAction SilentlyContinue
}
