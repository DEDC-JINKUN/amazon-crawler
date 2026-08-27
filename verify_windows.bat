@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_windows.bat first.
  exit /b 1
)
call .venv\Scripts\activate.bat
python scripts\amazon_us_worker.py --config config\amazon_us.windows.toml --materialize-only
set MATERIALIZE_EXIT=%ERRORLEVEL%
python scripts\amazon_us_verify.py --once
set VERIFY_EXIT=%ERRORLEVEL%
python scripts\verify_agent_review.py
python scripts\run_receipt.py --manifest amazon_us_asin_manifest.csv --state state\amazon_us.sqlite3 --output-dir data\amazon_us --raw-html-dir data\amazon_us\raw_html --output data\amazon_us\run_receipt.json
set RECEIPT_EXIT=%ERRORLEVEL%
if not "%MATERIALIZE_EXIT%"=="0" exit /b %MATERIALIZE_EXIT%
if not "%VERIFY_EXIT%"=="0" exit /b %VERIFY_EXIT%
if not "%RECEIPT_EXIT%"=="0" exit /b %RECEIPT_EXIT%
endlocal
