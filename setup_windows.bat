@echo off
setlocal
cd /d "%~dp0"
py -3 -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11 or newer is required'" || exit /b 1
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt || exit /b 1
python scripts\validate_us_manifest.py || exit /b 1
python scripts\amazon_us_worker.py --config config\amazon_us.windows.toml --dry-run || exit /b 1
python scripts\amazon_us_verify.py --once || exit /b 1
python scripts\verify_agent_review.py || exit /b 1
echo Windows setup and offline verification completed.
endlocal
