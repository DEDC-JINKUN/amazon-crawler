# Amazon Collection 当前 Agent 交接

**观察时间：** 2026-09-14 22:07（Asia/Shanghai）

**用途：** 新 Agent 的五分钟事实入口。本文是当前快照，不是部署凭证；变化事实必须回到下列权威来源复核。

## 先说结论

系统级 PowerShell 7.6.6、两项生产计划任务迁移和长期状态协议均已上线。当前 Collection/Operator pointer 是新release `33fd636`，reviewed code为`8c1d9d7`；两项任务均由系统pwsh托管为Running，三Worker在激活30次连续样本及真实业务门禁期间保持3/3健康、restart=0、快照清理失败=0。旧`4021a56`不得再激活。

3-ASIN门禁`campaign-bd26b05e4b8055a691adc80d42c22561`没有通过：1 succeeded、2 failed，已取消并全部终态。失败均为代理会话到Amazon的transport/fetch_error，不是Worker状态文件故障。用户随后于2026-09-14 22:06明确要求开始全量采集；1093条单一后台campaign已提交并开始领取，不能再按本文件此前的“保持空闲”结论操作。

## 事实矩阵

| 事实面 | 状态 | 当前证据 |
|---|---|---|
| 候选代码 | `deployed-reviewed` | release `33fd636`，reviewed code `8c1d9d7`；最终独立复核P0=0/P1=0 |
| 旧候选release | `rejected-by-live-gate` | `4021a56`初始ready，但3-ASIN运行后Worker在`os.replace` WinError 5上耗尽重启；禁止复用 |
| 线上 Collection | `verified-current` | current pointer `33fd636`；`/readyz` 200，目标tenant，requested/effective/alive/healthy=3/3/3/3，restart=0 |
| 线上 Operator | `verified-current` | operator pointer `33fd636` |
| 计划任务运行时 | `verified-current` | Collection/Postgres任务均为Running，Action固定`C:/Program Files/PowerShell/7/pwsh.exe` |
| 失败3-ASIN门禁 | `terminal-failed` | `campaign-bd26b05e4b8055a691adc80d42c22561`已取消且3/3终态：1 succeeded、2 transport failed |
| 20条campaign | `not-submitted` | 未提交独立20条门禁 |
| 全量campaign | `active` | `campaign-b14bef6a4b39509a9c92e1ff2629386b`，稳定幂等键`prod-33fd636-full1093-20260914-v1`；初始快照total=1093、pending=1092、running=1、terminal=0 |
| 目标清单 | `verified` | `Amazon Scraping/data/owned_us_asin_20260902_full_01/manifest_1093.csv`共1,093个唯一合法ASIN |
| 代理容量 | `business-gate-failed` | 最新34槽非Amazon canary为31可用/唯一并allowed；实际Amazon门禁中4个canary可用会话在连接/响应头阶段9–12秒无字节失败，另1个会话成功 |
| 默认非PG回归 | `verified-current` | 746 passed、56 skipped、0 failed |
| 隔离PostgreSQL门禁 | `verified-current` | `127.0.0.1:55444/operator_matrix`两文件29 passed；随后fast shutdown确认关闭 |
| Windows并发压力 | `verified-current` | 最终P1修复后3个真实子进程、3个被长期读锁的旧固定文件、4,606次并发status调用、持续10秒：alive/healthy=3/3、restart=0、每Worker保留3份快照 |

## 当前长期修复

- Worker只创建`worker-N.<launch-token>.<20位正整数sequence>.json`，不覆盖固定目标。
- Supervisor监控线程只接收当前launch token的最新完整快照，并把它缓存到内存。
- `/readyz`只读Supervisor缓存；最新文件若在写入中或损坏，保留上一份完整快照直到心跳超时。
- 每个launch通常保留3份快照；跨launch持续清理并设置每Worker 64文件/1 MiB总上限。清理遇到Windows锁只做best-effort，不允许单次清理失败杀死健康Worker；无法为完整3份、每份最大4 KiB的滚动窗口预留容量时必须在领取任务前fail closed。
- 目录枚举或剩余文件大小统计失败视为容量未知并fail closed；多Worker启动中后续成员失败时会终止已启动成员，不保留部分进程池。
- `/readyz`子Worker项公开`status_snapshot_backlog_files`、`status_snapshot_backlog_bytes`和`status_snapshot_cleanup_failures`，便于对锁定旧快照告警。
- 旧`worker-N.json`仅作迁移兼容读取；子进程存活仍由受管`Popen`判断，状态PID仅供诊断。

关键文件：

- [`scripts/campaign_worker_status.py`](../scripts/campaign_worker_status.py)
- [`scripts/campaign_worker_process.py`](../scripts/campaign_worker_process.py)
- [`scripts/agent_collection_service.py`](../scripts/agent_collection_service.py)
- [`tests/test_agent_collection_service.py`](../tests/test_agent_collection_service.py)
- [`docs/collection_api.md`](collection_api.md)

## 已取得的验证证据

- Windows真实拒绝delete sharing句柄复现旧实现：20次重试后约0.22秒稳定抛`WinError 5`。
- 新Windows锁回归和Supervisor损坏快照/内存缓存回归：通过。
- Agent服务文件：44 passed。
- Agent服务、Collection API、MCP、Windows入口：98 passed。
- 默认非PG套件：746 passed、56 skipped。
- 专用55444数据库门禁：29 passed并关闭实例。
- Python编译与`git diff --check`：通过。

详细过程见 [`progress.md`](../progress.md)、[`findings.md`](../findings.md) 和 [`task_plan.md`](../task_plan.md)。

## 下一步严格顺序

1. 不得重新提交、拆分或更换幂等键；全量唯一权威是campaign`campaign-b14bef6a4b39509a9c92e1ff2629386b`。
2. 用户只问进度时调用campaign status一次；用户要求等待结果时对同一campaign反复wait直到全部终态。只有用户明确要求停止时才cancel未来排队成员。
3. 监控三Worker readiness、restart、capacity backpressure及transport/blocked比例。此前3条门禁2/3 transport失败是当前全量的已知高失败风险，不能在报告中隐藏。
4. 后续仍建议解决住宅代理到Amazon的会话质量，或另行设计跨run的Amazon transport失败会话隔离；不得把非Amazon canary改成隐式Amazon探测。

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

> 在`D:\woring\爬虫\.worktrees\amazon-integrated-deploy`接手Amazon Collection。先完整读取`AGENTS.md`和`docs/agent_handoff_current.md`，再只读核对分支、提交、工作区、deploy pointers、计划任务状态与`127.0.0.1:8765/readyz`。当前release为`33fd636`，全量唯一campaign为`campaign-b14bef6a4b39509a9c92e1ff2629386b`，共1093条；不得重新提交或换幂等键。3-ASIN门禁曾2个transport失败，因此全量存在高失败风险。请用四项回复：线上状态、campaign计数、Worker健康、代理风险。不要输出凭据；只有用户明确要求停止时才取消排队成员。
