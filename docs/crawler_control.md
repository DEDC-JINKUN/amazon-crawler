# Amazon Crawler Control

`crawler.ps1` 是Windows本机的统一控制入口。它只编排现有PostgreSQL Worker和只读Console，不包含第二套爬虫实现。

## 最常用命令

```powershell
.\crawler.ps1 canary -Limit 3
.\crawler.ps1 probe
.\crawler.ps1 run -Limit 10
.\crawler.ps1 status
.\crawler.ps1 console
.\crawler.ps1 stop
.\crawler.ps1 stop -All
```

- `canary`：测试全部计划会话的非Amazon HTTPS认证、CONNECT/TLS、延迟和运行内出口去重；`-Limit`表示下一阶段计划action数；
- `probe`：默认3个纯商品action，只用于出口恢复或上线前验证，可用 `-Limit 1..5` 覆盖；缺少同配置、未过期且容量足够的canary时不领取任务；
- `run`：默认10个纯商品action，可用 `-Limit 1..500`；
- `status`：不需要数据库密码，读取已运行的本机Console，显示任务和最近run；
- `console`：复用已有Console；未运行时隐藏输入数据库密码并启动；
- `stop`：停止当前Worker，不解除blocked、不删除断点；
- `stop -All`：同时停止由控制脚本管理的Console。

超过100个action必须显式增加：

```powershell
.\crawler.ps1 run -Limit 500 -ConfirmLargeBatch
```

这只是误操作保护，不代表500批次已经通过出口和质量门。

## 默认批次

默认指向：

```text
tenant       real_batch_20260828_500_04
manifest     data\postgres_real_batch_20260828_500_04\manifest_500.csv
config       data\postgres_real_batch_20260828_500_04\batch500.toml
output       data\postgres_real_batch_20260828_500_04
console      http://127.0.0.1:8770
```

可以通过 `-TenantId`、`-ManifestPath`、`-ConfigPath`、`-OutputDir` 和 `-Port` 切换经过批准的隔离批次。路径必须位于项目目录内。

## 一次run的流程

1. 获取按项目和tenant命名的Windows named mutex，原子保证单控制器；
2. 隐藏输入PostgreSQL密码；
3. 复用或启动loopback-only只读Console；
4. 使用非Amazon端点执行live preflight；
5. 读取PostgreSQL最新canary operation，验证配置指纹、新鲜度、计划规模和唯一槽容量；拒绝时不创建collection run、不领取任务、不访问Amazon；
6. 生成唯一run_id和worker_id；
7. 启动Windows Job Object宿主；宿主先等待gate，控制器登记真实宿主PID后才允许启动Worker；
8. 当前控制台每5秒显示run进度；
9. Worker结束后写receipt，打印stdout/stderr尾部并清除Worker锁；
10. 控制脚本清除自己创建的密码环境变量。

所有任务固定使用 `--product-only`、`--once` 和PostgreSQL事实源。遇CAPTCHA/403/429/WAF时，Worker仍按配置立即停止；控制脚本不会自动换IP、重排blocked或继续剩余任务。

## 运行产物

每次run写入：

```text
data/<batch>/control/runs/<run_id>/
├── worker.stdout.log
├── worker.stderr.log
└── receipt.json
```

receipt记录run_id、worker_id、tenant、命令、请求上限、状态、退出码、起止时间、耗时、日志和Console URL，不记录密码或DSN。

锁文件：

```text
data/<batch>/control/.worker.lock.json
data/<batch>/control/.console.lock.json
```

`stop`在终止前同时校验PID与进程StartTime，避免PID复用导致误杀。崩溃留下的失效锁会在下一次命令自动清理。

Worker和Console都运行在 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的Windows Job Object中。`stop`终止宿主后，Windows内核同时清理其Python、Firefox和geckodriver子进程；控制器意外退出时，实际宿主PID仍保留在锁中，可由另一个终端执行 `stop`。

Console复用依据 `/readyz`，必须同时匹配数据库可用、tenant和raw HTML目录；只返回health但身份不匹配的服务不会被复用。由控制脚本管理的错误tenant Console会先安全停止再重启；未知进程占用端口时fail-closed。

preflight完整输出保存为每个run目录的 `preflight.log`，receipt同时记录该路径。即使Worker尚未启动，preflight失败也会留下可审计receipt并清除Worker锁。

## 退出码

- `0`：命令完成；
- `2`：参数、路径、preflight、凭据或启动失败；
- `3`：canary/容量Gate拒绝，或Worker遇到blocked并按设计停止；

实际run结果以Console的“本次运行结果”和对应receipt为准，不能只看总任务状态。
