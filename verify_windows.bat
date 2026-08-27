@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_windows.bat first.
  exit /b 1
)
call .venv\Scripts\activate.bat
python scripts\amazon_us_worker.py --config config\amazon_us.windows.toml --materialize-only || exit /b 1
python scripts\amazon_us_verify.py --once || exit /b 1
python scripts\verify_agent_review.py
endlocal
