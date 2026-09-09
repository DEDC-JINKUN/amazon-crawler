@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_windows.bat first.
  exit /b 1
)
call .venv\Scripts\activate.bat
if "%AMAZON_US_POSTGRES_DSN%"=="" (
  echo Set AMAZON_US_POSTGRES_DSN before running the production worker.
  exit /b 1
)
python scripts\amazon_us_worker.py --config config\amazon_us.windows.toml --backend postgres --tenant-id amazon_us_local --subject-type own --live --once
set WORKER_EXIT=%ERRORLEVEL%
python scripts\verify_postgres.py --dsn-env AMAZON_US_POSTGRES_DSN --tenant-id amazon_us_local
set VERIFY_EXIT=%ERRORLEVEL%
if not "%WORKER_EXIT%"=="0" exit /b %WORKER_EXIT%
exit /b %VERIFY_EXIT%
