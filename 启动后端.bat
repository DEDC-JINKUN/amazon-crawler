@echo off
REM ============ 启动后端（批次协调器）============
REM 作用：常驻协调器，自动领取网页上传的批次任务，每批拉 2 个 Worker 并发采集
REM 合规说明：与网页共用同一套批次系统（数据库领任务），不是独立测试入口
REM 网页上传清单后，本脚本启动的协调器会自动开始采集；停止/进度都在网页上看
REM 改并发数：改下面 --workers-per-batch 2 的数字
chcp 65001 >nul
cd /d "%~dp0"

REM 数据库连接串（密码 123456）
set "AMAZON_US_POSTGRES_DSN=postgresql://postgres:123456@localhost:5432/amazon_us"

REM ---- 数据库自检：没在跑就自动拉起（最多等 15 秒）----
REM 本机 PostgreSQL 17 安装在 D:\PostgresQL，服务名 postgresql-x64-17，开机自启
D:\PostgresQL\bin\pg_isready.exe -h 127.0.0.1 -p 5432 >nul 2>&1
if errorlevel 1 (
    echo 数据库未运行，正在自动启动...
    D:\PostgresQL\bin\pg_ctl.exe -D "D:\PostgresQL\data" -l "D:\PostgresQL\data\server.log" start >nul 2>&1
    set /a wait_count=0
    :wait_db
    if %wait_count% GEQ 15 goto db_ready
    D:\PostgresQL\bin\pg_isready.exe -h 127.0.0.1 -p 5432 >nul 2>&1
    if not errorlevel 1 goto db_ready
    timeout /t 1 /nobreak >nul
    set /a wait_count+=1
    goto wait_db
)
:db_ready
echo 数据库就绪

echo ============================================
echo   批次协调器启动（每批 2 个 Worker 并发）
echo   请打开控制台网页上传清单：http://127.0.0.1:8770
echo   本窗口保持开着 = 后端在运行，关闭窗口 = 后端停止
echo ============================================
if not exist ".venv\Scripts\python.exe" (
    echo 错误: 未找到 .venv 虚拟环境，请先运行 setup_windows.bat
    pause
    exit /b 1
)
.venv\Scripts\python.exe scripts\batch_coordinator.py run --dsn-env AMAZON_US_POSTGRES_DSN --tenant-id amazon_us_local --subject-type own --workers-per-batch 2
echo.
echo ============================================
echo   协调器已退出（退出码 %ERRORLEVEL%）
echo ============================================
pause
