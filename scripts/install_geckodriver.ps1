param(
  [string]$Version = "0.37.1"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$tools = Join-Path $root "tools"
$archive = Join-Path $tools "geckodriver-v$Version-win64.zip"
$extract = Join-Path $tools "geckodriver-v$Version"
$driver = Join-Path $extract "geckodriver.exe"
$url = "https://github.com/mozilla/geckodriver/releases/download/v$Version/geckodriver-v$Version-win64.zip"
$expectedSha256 = "DFED9315ABE8D2FBC1B6161A2EE8002452E79CF05EE92FDC653A4E26BC35EDD8"

New-Item -ItemType Directory -Path $tools -Force | Out-Null
if (-not (Test-Path -LiteralPath $driver)) {
  Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
  $actualSha256 = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
  if ($actualSha256 -ne $expectedSha256) {
    throw "geckodriver archive SHA256 mismatch: $actualSha256"
  }
  Expand-Archive -LiteralPath $archive -DestinationPath $extract -Force
}

if (-not (Test-Path -LiteralPath $driver)) {
  throw "geckodriver.exe was not found after extraction: $driver"
}

Write-Output "Installed geckodriver $Version at $driver"
Get-FileHash -LiteralPath $archive -Algorithm SHA256 | Format-List
