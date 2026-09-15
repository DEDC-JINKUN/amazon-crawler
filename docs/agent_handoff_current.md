# Amazon Collection 当前 Agent 交接

**观察时间：** 2026-09-15 14:28（Asia/Shanghai）

**用途：** 新 Agent 的五分钟事实入口。本文是当前快照，不是部署凭证；变化事实必须回到下列权威来源复核。

## 先说结论

生产 Collection/Operator pointer 仍是 `28d62ce`；14:28两项计划任务显示Running，但8765 health/ready和8770 Console均在5秒内0字节超时，Collection MCP也返回`backend_unavailable`。这只能证明任务登记仍在，不能证明服务或Worker健康。未做重启、cancel、重新提交或生产数据库查询。

开发候选已推进到`fc1a45f`且独立delta review为P0=0/P1=0、CONTINUE，但尚未制作或部署新release。用户随后将PDP、独立评论和统一编排合并为新的四阶段计划，权威目标见[`integrated_collection_program_goal_20260915.md`](integrated_collection_program_goal_20260915.md)。全量唯一campaign仍是`campaign-b14bef6a4b39509a9c92e1ff2629386b`，不得重建、拆分或取消；当前计数因API超时不可验证。外部手册记录的84终态是此前快照，不得当作14:28当前值。

## 事实矩阵

| 事实面 | 状态 | 当前证据 |
|---|---|---|
| 候选代码 | `reviewed-not-deployed` | commit `fc1a45f`；效率修复独立delta复核P0=0/P1=0、CONTINUE，完整非PG 777 passed/59 skipped，Operator PG 29 passed，恢复/Worker等PG 57 passed |
| 旧候选release | `rejected-by-live-gate` | `4021a56`初始ready，但3-ASIN运行后Worker在`os.replace` WinError 5上耗尽重启；禁止复用 |
| 线上 Collection | `pending-runtime-recovery` | current pointer `28d62ce`；14:28 healthz/readyz均5秒0字节超时，MCP `backend_unavailable`，未自动重启 |
| 线上 Operator | `pending-runtime-recovery` | operator pointer `28d62ce`；8770 healthz 5秒0字节超时 |
| 计划任务运行时 | `registered-running-not-serving` | Collection/Postgres任务均显示Running，但对应HTTP服务未在5秒内响应；不能把任务状态等同于服务健康 |
| 失败3-ASIN门禁 | `terminal-failed` | `campaign-bd26b05e4b8055a691adc80d42c22561`已取消且3/3终态：1 succeeded、2 transport failed |
| 20条campaign | `not-submitted` | 未提交独立20条门禁 |
| 全量campaign | `preserved-current-count-unavailable` | `campaign-b14bef6a4b39509a9c92e1ff2629386b`，稳定幂等键`prod-33fd636-full1093-20260914-v1`；API超时，当前计数未验证，绝不重提/拆分/cancel |
| 目标清单 | `verified` | `Amazon Scraping/data/owned_us_asin_20260902_full_01/manifest_1093.csv`共1,093个唯一合法ASIN |
| 代理容量 | `capacity-allowed/amazon-risk` | 激活canary为33/34可用且capacity allowed；非Amazon可用仍不证明Amazon稳定，历史Amazon门禁2/3 transport failed |
| 默认非PG回归 | `verified-current` | 777 passed、59 skipped、0 failed |
| 隔离PostgreSQL门禁 | `verified-current` | 全新UTF-8本地集群：Operator 29 passed；恢复/Worker/Campaign/指标等57 passed；随后fast shutdown |
| Windows进程生命周期 | `verified-current` | 真实Windows父进程退出后owned `firefox.exe`按PID+exe+creation ticks回收；PID复用/身份缺失fail closed；11:38存活geckodriver均有活Worker父进程、孤儿为0 |

## 当前长期修复

- Worker只创建`worker-N.<launch-token>.<20位正整数sequence>.json`，不覆盖固定目标。
- Supervisor监控线程只接收当前launch token的最新完整快照，并把它缓存到内存。
- `/readyz`只读Supervisor缓存；最新文件若在写入中或损坏，保留上一份完整快照直到心跳超时。
- 每个launch通常保留3份快照；跨launch持续清理并设置每Worker 64文件/1 MiB总上限。清理遇到Windows锁只做best-effort，不允许单次清理失败杀死健康Worker；无法为完整3份、每份最大4 KiB的滚动窗口预留容量时必须在领取任务前fail closed。
- 目录枚举或剩余文件大小统计失败视为容量未知并fail closed；多Worker启动中后续成员失败时会终止已启动成员，不保留部分进程池。
- `/readyz`子Worker项公开`status_snapshot_backlog_files`、`status_snapshot_backlog_bytes`和`status_snapshot_cleanup_failures`，便于对锁定旧快照告警。
- 旧`worker-N.json`仅作迁移兼容读取；子进程存活仍由受管`Popen`判断，状态PID仅供诊断。
- Firefox adapter关闭前捕获geckodriver后代的PID、allowlist可执行名和创建时间；关闭超时后只有三项身份仍一致才终止，身份未知不强杀个人Firefox。
- Supervisor在spawn前生成`agent-refresh-<launch-token>` owner并传给child；首次心跳前崩溃也能按owner回队claimed任务、释放item lease和active reservation。
- stop先安装shutdown fence，禁止新的refresh claim和capacity reservation，再等待线程并清理owner；多Worker父进程做幂等二次清理。
- 生产`deploy/.../bin/service_control.ps1`还在停止前捕获本部署geckodriver树的PID/创建时间，停止后只清理身份仍一致的残留；这是部署控制层事实，不在release archive内。

关键文件：

- [`scripts/campaign_worker_status.py`](../scripts/campaign_worker_status.py)
- [`scripts/campaign_worker_process.py`](../scripts/campaign_worker_process.py)
- [`scripts/agent_collection_service.py`](../scripts/agent_collection_service.py)
- [`tests/test_agent_collection_service.py`](../tests/test_agent_collection_service.py)
- [`docs/collection_api.md`](collection_api.md)

## 已取得的验证证据

- `fc1a45f`效率修复：独立delta review P0=0/P1=0、CONTINUE；定向74 passed、Console loopback 20/20、完整非PG 777 passed/59 skipped、Operator PG 29 passed、恢复/Worker/Campaign/指标等PG 57 passed；测试库均关闭。
- 14:28只读运行态：Collection/Operator pointer均`28d62ce`；两项计划任务显示Running；8765 health/ready、8770 health及Collection MCP均未完成请求。
- Windows真实拒绝delete sharing句柄复现旧实现：20次重试后约0.22秒稳定抛`WinError 5`。
- 新Windows锁回归和Supervisor损坏快照/内存缓存回归：通过。
- 生命周期相关聚焦套件：210 passed。
- 历史release `28d62ce`默认非PG套件：762 passed、56 skipped。
- 历史release `28d62ce`专用55444数据库门禁：29 passed并关闭实例。
- Python编译、PowerShell AST与`git diff --check`：通过。
- 激活回执：`D:/woring/爬虫/outputs/prod-full-rollout-20260914/activation-28d62ce.json`；`ok=true`、`campaign_preserved=true`。

详细过程见 [`progress.md`](../progress.md)、[`findings.md`](../findings.md) 和 [`task_plan.md`](../task_plan.md)。

## 下一步严格顺序

1. 按[`integrated_collection_program_goal_20260915.md`](integrated_collection_program_goal_20260915.md)从Goal 1的source manifest与clean rebuild审计重新开始；PDP、独立评论、云端v5和本机部署代码未建立来源证据前不得直接合并。
2. 不得重新提交、拆分或更换幂等键；全量唯一权威是campaign`campaign-b14bef6a4b39509a9c92e1ff2629386b`。只有用户明确要求停止时才cancel未来排队成员。
3. 当前计划任务虽显示Running但HTTP/MCP不可用。Goal 1只做共同的只读盘点，不自动重启、切release、访问生产库或启动Worker。
4. 旧Codex PDP效率Goal仍为active且未完成；用户在界面停止旧Goal后，才可把综合方案的Goal 1登记为新的active Goal。

## 新 Agent 开场检查

```powershell
Set-Location 'D:\woring\爬虫\.worktrees\amazon-integrated-deploy'
git branch --show-current
git log -3 --oneline
git status --short
Get-Content 'D:\woring\爬虫\deploy\amazon-collection\current-release.txt'
Get-Content 'D:\woring\爬虫\deploy\amazon-collection\operator-release.txt'
curl.exe --silent --show-error --max-time 5 http://127.0.0.1:8765/readyz
```

开场只汇报：线上release/readiness、全量campaign当前计数、3-ASIN已知代理风险、是否需要用户动作。不得再次提交1093、不得更换幂等键或拆分campaign，不得输出代理凭据。

## 可直接复制给新 Agent 的提示词

> 在`D:\woring\爬虫\.worktrees\amazon-integrated-deploy`接手PDP与独立评论融合项目。先完整读取`AGENTS.md`、`docs/agent_handoff_current.md`和`docs/integrated_collection_program_goal_20260915.md`。开发候选`fc1a45f`已评审但未部署；生产pointer仍为`28d62ce`，14:28计划任务显示Running但8765/8770与Collection MCP均超时。全量唯一campaign为`campaign-b14bef6a4b39509a9c92e1ff2629386b`，不得重提、换幂等键、拆分或cancel。当前从Goal 1的source manifest和clean rebuild审计开始，不访问生产、不启动Worker、不创建定时任务；所有“当前”状态必须重新只读验证，旧快照不能沿用。
