param(
  [string]$PsqlPath = "D:\PostgreSQL\bin\psql.exe",
  [string]$HostName = "127.0.0.1",
  [int]$Port = 5432,
  [string]$User = "postgres",
  [string]$Database = "postgres",
  [string]$SchemaPath = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $SchemaPath) {
  $SchemaPath = Join-Path $root "schema\postgres_schema.sql"
}
if (-not (Test-Path -LiteralPath $PsqlPath)) {
  throw "psql.exe not found: $PsqlPath"
}
if (-not (Test-Path -LiteralPath $SchemaPath)) {
  throw "schema file not found: $SchemaPath"
}

$securePassword = Read-Host "Enter PostgreSQL password for $User@$HostName`:$Port/$Database" -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
try {
  $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
  $env:PGPASSWORD = $plainPassword
  & $PsqlPath -h $HostName -p $Port -U $User -d $Database -w -v ON_ERROR_STOP=1 -f $SchemaPath
  if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL schema execution failed with exit code $LASTEXITCODE"
  }
  Write-Output "PostgreSQL schema applied successfully to $Database@$HostName`:$Port"
}
finally {
  if ($pointer -ne [IntPtr]::Zero) {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
  }
  Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
  $plainPassword = $null
}
