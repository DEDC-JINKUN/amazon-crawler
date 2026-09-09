# 一键切换到 ZooProxy 住宅代理（2026-09-09 晨用）
# 用法: powershell -File scripts\switch_to_proxy.ps1
# 前提: 协调器/worker 正在跑或已停均可；.env 里有 ZOOPROXY_* 三行凭证
param(
  [switch]$Revert  # 加 -Revert 恢复直连（清代理配置）
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Push-Location $root

# ---- 1. 读 .env 凭证 ----
$zooproxyHost = $null; $zooproxyUser = $null; $zooproxyPass = $null
foreach ($line in Get-Content (Join-Path $root ".env") -Encoding UTF8) {
  if ($line -match '^\s*ZOOPROXY_HOST\s*=\s*(.+)$') { $zooproxyHost = $Matches[1].Trim() }
  if ($line -match '^\s*ZOOPROXY_USERNAME\s*=\s*(.+)$') { $zooproxyUser = $Matches[1].Trim() }
  if ($line -match '^\s*ZOOPROXY_PASSWORD\s*=\s*(.+)$') { $zooproxyPass = $Matches[1].Trim() }
}
if (-not $Revert -and (-not $zooproxyHost -or -not $zooproxyUser -or -not $zooproxyPass)) {
  throw ".env 缺 ZOOPROXY_* 凭证，无法切换"
}

# ---- 2. 停协调器+worker（杀真实 python 进程；venv 启动器随之退出）----
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'batch_coordinator\.py|amazon_us_worker\.py' -and $_.CommandLine -match 'amazon-crawler-main' }
foreach ($p in $procs) {
  # 找子进程：真实 python 的 ParentProcessId 指向 venv 启动器；杀子进程即可
  $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($p.ProcessId)" |
    Where-Object { $_.CommandLine -match 'batch_coordinator|amazon_us_worker' }
  if ($children) { foreach ($c in $children) { Stop-Process -Id $c.ProcessId -Force -ErrorAction SilentlyContinue } }
  else { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
}
Write-Host "[1/5] 协调器/worker 已停止"
Start-Sleep -Seconds 3

# ---- 3. 改 human.toml ----
$toml = Join-Path $root "config\amazon_us.windows.human.toml"
$content = Get-Content $toml -Raw -Encoding UTF8
if ($Revert) {
  $content = $content -replace 'proxy_url = "[^"]*"', 'proxy_url = ""'
  $content = $content -replace 'proxy_username_env = "[^"]*"', 'proxy_username_env = ""'
  $content = $content -replace 'proxy_password_env = "[^"]*"', 'proxy_password_env = ""'
  $content = $content -replace 'block_images = \w+', 'block_images = false'
  $mode = "直连（VPN）"
} else {
  $content = $content -replace 'proxy_url = "[^"]*"', "proxy_url = `"http://$zooproxyHost`""
  $content = $content -replace 'proxy_username_env = "[^"]*"', 'proxy_username_env = "ZOO_PROXY_USERNAME"'
  $content = $content -replace 'proxy_password_env = "[^"]*"', 'proxy_password_env = "ZOO_PROXY_PASSWORD"'
  $content = $content -replace 'block_images = \w+', 'block_images = true'
  $mode = "ZooProxy 住宅代理"
}
# PS 5.1 的 Set-Content -Encoding UTF8 会写 BOM，tomllib/json 解析带 BOM 的文件直接失败
# （2026-09-09 事故：BOM 导致 worker 启动即崩、烧光重启上限、整批被取消）——必须无 BOM 写入
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($toml, $content, $utf8NoBom)
Write-Host "[2/5] human.toml 已切到 $mode"

# ---- 4. 清熔断门 ----
$gate = Join-Path $root "state\captcha_gate.json"
$today = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
$gateJson = @{ date = $today; count = 0; consecutive = 0; paused_until = $null } | ConvertTo-Json
[System.IO.File]::WriteAllText($gate, $gateJson, $utf8NoBom)
Write-Host "[3/5] 熔断门已清（新 worker 启动即见清门，立即开工）"

# ---- 5. 带代理凭证重启协调器 ----
$env:AMAZON_US_POSTGRES_DSN = 'host=127.0.0.1 port=5432 dbname=amazon_us user=postgres'
foreach ($line in Get-Content (Join-Path $root ".env") -Encoding UTF8) {
  if ($line -match '^\s*POSTGRES_PASSWORD\s*=\s*(.+)$') { $env:PGPASSWORD = $Matches[1].Trim() }
}
if (-not $Revert) {
  $env:ZOO_PROXY_USERNAME = $zooproxyUser
  $env:ZOO_PROXY_PASSWORD = $zooproxyPass
}
$python = Join-Path $root ".venv\Scripts\python.exe"
$coord = Start-Process -FilePath $python -ArgumentList @(
  "$root\scripts\batch_coordinator.py", "run",
  "--tenant-id", "amazon_us_main", "--subject-type", "own",
  "--workers-per-batch", "2",
  "--worker-config", "$root\config\amazon_us.windows.human.toml"
) -WorkingDirectory $root -WindowStyle Hidden -PassThru
Write-Host "[4/5] 协调器已重启（pid=$($coord.Id)，模式=$mode）"

# ---- 6. 验证出口 ----
if (-not $Revert) {
  Write-Host "[5/5] 验证代理出口（60s 超时）..."
  & curl.exe -s -x $zooproxyHost -U "${zooproxyUser}:${zooproxyPass}" https://ipinfo.io/json --max-time 60
  Write-Host ""
  Write-Host "切换完成。worker 将由协调器自动拉起并走代理续采。"
  Write-Host "监控: .venv\Scripts\python.exe scripts\batch_coordinator.py status --batch-id 03a9e3fd-a6d6-4ee9-afcd-d9701a2f100a --tenant-id amazon_us_main"
}
