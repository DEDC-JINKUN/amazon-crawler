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
   非DPAPI入口还必须显式提供非秘密`AMAZON_PROXY_CREDENTIAL_GENERATION`，并在代理凭据轮换时更新；
3. 复用或启动loopback-only只读Console；
4. 使用非Amazon端点执行live preflight；
5. 读取PostgreSQL最新canary operation，验证credential generation、配置指纹、事实状态/计数、新鲜度和计划规模；按实际action数计算最低生产槽，并额外预约最多两个有界隔离替换槽；所有物理槽身份在PostgreSQL advisory lock内跨tenant/进程原子互斥；
6. 把canary operation、reservation ID、事实/过期时间及安全容量快照绑定到控制operation和collection run；拒绝时不创建collection run、不领取任务、不访问Amazon；
7. 启动Windows Job Object宿主；宿主先等待gate，控制器登记真实宿主PID后才允许启动Worker；
8. 当前控制台每5秒显示run进度；
9. Worker结束后写receipt，打印stdout/stderr尾部并清除Worker锁；
10. Worker与Controller幂等释放reservation；异常终止依赖同一释放路径或TTL回收；控制脚本清除自己创建的密码环境变量。

商品任务使用 `--product-only`、`--once` 和PostgreSQL事实源；评论入口仍单独限定stage。一次run可包含多个最多5-ASIN的容量批次，不能按整个清单一次预约2N槽。每次claim和实际请求仍检查租约与持久化预算。单ASIN最终阻断进入冷却；全局暂停跨run/进程保持，不因新建run清零。HTTP429尊重Retry-After，等待前不启动Firefox。

生产出口仅接受付费`proxy_sessions`配置；VPN/直连只可作人工诊断对照，不能通过正式Gate。商品广度每ASIN使用独立会话，评论分页对同ASIN保持粘滞。HTTP challenge先允许同槽stock Firefox验证；失败时最多换一个已预约槽做最后一次Firefox验证，不再在该新槽先打HTTP；仍为challenge则隔离。连续2个最终blocked ASIN或20窗口内3个最终blocked触发共享暂停；半开只允许一个逻辑任务，成功才恢复。Firefox CONNECT中继不解密TLS且不保存凭据。

持久化恢复需要显式迁移 `schema/migrations/20260904_recovery.sql`（本轮只在独立测试库执行，生产尚未迁移）。默认每ASIN/stage总claim预算3、HTTP尝试/浏览器导航许可12、Firefox获准资源请求240、deadline24小时；总预算不会被内部重试、换槽、重启或重复refresh清零。初次发现无预算合同的历史evidence保持`legacy_budget_unknown`，必须走独立审计/审批，不能自动给空预算。ZIP partial与variant为不同可解释终态，不归为CAPTCHA。HTTP尝试共享至少5秒间隔（可以配置得更慢），已有抖动保留。恢复状态通过Console `/api/recovery`、Agent `/v1/recovery/status` 和新receipt中的`recovery`查询；它是当前tenant投影，不改写历史run。

## 运行产物

每次run写入：

```text
data/<batch>/control/runs/<run_id>/
├── worker.stdout.log
├── worker.stderr.log
└── receipt.json
```

receipt记录run_id、worker_id、tenant、命令、请求上限、状态、退出码、起止时间、耗时、日志和Console URL，不记录密码或DSN。
receipt还记录具体authorizing canary、capacity reservation、事实完成/过期时间、预约槽数和安全容量快照，不记录端口映射或出口IP。

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
