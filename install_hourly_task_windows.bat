@echo off
setlocal
cd /d "%~dp0"
set TASK_NAME=Amazon US Collection Hourly
set SCRIPT=%CD%\run_scheduled_windows.bat
schtasks /Create /F /SC HOURLY /MO 1 /TN "%TASK_NAME%" /TR "cmd.exe /c \"%SCRIPT%\""
if errorlevel 1 exit /b 1
echo Created Windows scheduled task: %TASK_NAME%
echo To remove it: schtasks /Delete /F /TN "%TASK_NAME%"
endlocal
