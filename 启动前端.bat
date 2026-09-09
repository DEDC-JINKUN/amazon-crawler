@echo off
REM ============ 启动前端（控制台网页）============
REM 作用：控制台网页 = 统一入口，上传清单、启动/停止采集、看进度、下载结果
REM 等价于 Java 的 mvn spring-boot:run，一条命令启动
REM 启动后浏览器打开 http://127.0.0.1:8770
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
echo   启动爬虫控制台：http://127.0.0.1:8770
echo   按 Ctrl+C 停止
echo ============================================
if not exist ".venv\Scripts\python.exe" (
    echo 错误: 未找到 .venv 虚拟环境，请先运行 setup_windows.bat
    pause
    exit /b 1
)
.venv\Scripts\python.exe scripts\collection_console.py --tenant-id amazon_us_local --raw-html-dir data\amazon_us\raw_html --host 127.0.0.1 --port 8770
pause
