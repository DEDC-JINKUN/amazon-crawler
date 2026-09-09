param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$CommandArgs
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $root

if (-not $env:AMAZON_US_POSTGRES_DSN) {
  $envFile = Join-Path $root ".env"
  if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile -Encoding UTF8) {
      if ($line -match '^\s*AMAZON_US_POSTGRES_DSN\s*=\s*(.+)$') {
        $env:AMAZON_US_POSTGRES_DSN = $Matches[1].Trim()
      }
      if ($line -match '^\s*POSTGRES_PASSWORD\s*=\s*(.+)$') {
        $env:PGPASSWORD = $Matches[1].Trim()
      }
    }
  }
}

if (-not $env:AMAZON_US_POSTGRES_DSN) {
  throw "AMAZON_US_POSTGRES_DSN not set and not found in .env"
}

& (Join-Path $root ".venv\Scripts\python.exe") @CommandArgs
exit $LASTEXITCODE
