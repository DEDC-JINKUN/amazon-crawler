@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_windows.bat first.
  exit /b 1
)
call .venv\Scripts\activate.bat
python scripts\amazon_us_worker.py --config config\amazon_us.windows.toml --live --once
set WORKER_EXIT=%ERRORLEVEL%
python scripts\amazon_us_worker.py --config config\amazon_us.windows.toml --materialize-only
python scripts\amazon_us_verify.py --once
set VERIFY_EXIT=%ERRORLEVEL%
python scripts\verify_agent_review.py
if not "%WORKER_EXIT%"=="0" exit /b %WORKER_EXIT%
exit /b %VERIFY_EXIT%
