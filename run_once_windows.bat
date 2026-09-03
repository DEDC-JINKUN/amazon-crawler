@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_windows.bat first.
  exit /b 1
)
if "%AMAZON_US_POSTGRES_DSN%"=="" (
  echo Set AMAZON_US_POSTGRES_DSN before running the production worker.
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File crawler.ps1 run -Limit 10 -TenantId amazon_us_local -ManifestPath amazon_us_asin_manifest.csv -ConfigPath config\amazon_us.windows.toml -OutputDir data\amazon_us
set WORKER_EXIT=%ERRORLEVEL%
.venv\Scripts\python.exe scripts\verify_postgres.py --dsn-env AMAZON_US_POSTGRES_DSN --tenant-id amazon_us_local
set VERIFY_EXIT=%ERRORLEVEL%
if not "%WORKER_EXIT%"=="0" exit /b %WORKER_EXIT%
exit /b %VERIFY_EXIT%
