param(
  [string]$HostName = "127.0.0.1",
  [int]$Port = 5432,
  [string]$User = "postgres",
  [string]$Database = "postgres",
  [ValidateSet("own", "competitor", "candidate")]
  [string]$SubjectType = "candidate"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $root ".venv\Scripts\python.exe"
$sqlite = Join-Path $root "state\amazon_us.sqlite3"
$schema = Join-Path $root "schema\postgres_schema.sql"
$dsn = "host=$HostName port=$Port dbname=$Database user=$User"

if (-not (Test-Path -LiteralPath $python)) { throw "Project venv not found: $python" }
if (-not (Test-Path -LiteralPath $sqlite)) { throw "SQLite state not found: $sqlite" }

$securePassword = Read-Host "Enter PostgreSQL password for $User@$HostName`:$Port/$Database" -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
try {
  $env:PGPASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
  & $python (Join-Path $root "scripts\migrate_sqlite_to_postgres.py") --sqlite $sqlite --dsn $dsn --schema $schema --subject-type $SubjectType
  if ($LASTEXITCODE -ne 0) { throw "SQLite to PostgreSQL replay failed with exit code $LASTEXITCODE" }
  & $python (Join-Path $root "scripts\verify_postgres.py") --dsn $dsn
  if ($LASTEXITCODE -ne 0) { throw "PostgreSQL verification failed with exit code $LASTEXITCODE" }
}
finally {
  if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
  Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}
