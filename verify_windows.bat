@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_windows.bat first.
  exit /b 1
)
call .venv\Scripts\activate.bat
if "%AMAZON_US_POSTGRES_DSN%"=="" (
  echo Set AMAZON_US_POSTGRES_DSN before verifying production storage.
  exit /b 1
)
python scripts\verify_postgres.py --dsn-env AMAZON_US_POSTGRES_DSN --tenant-id amazon_us_local
set VERIFY_EXIT=%ERRORLEVEL%
exit /b %VERIFY_EXIT%
