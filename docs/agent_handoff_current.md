# Amazon Collection 当前 Agent 交接

**观察时间：** 2026-09-14 20:04（Asia/Shanghai）

**用途：** 新 Agent 的五分钟事实入口。本文是当前快照，不是部署凭证；变化事实必须回到下列权威来源复核。

## 先说结论

系统级 PowerShell 7.6.6 和两项生产计划任务迁移已经完成。`4021a56`曾成功激活并取得三 Worker 初始3/3/3/3 readiness，但新的3-ASIN业务门禁证明其固定状态文件方案在 Windows 持续读锁下仍会让三个 Worker崩溃；因此20条和1,093条都没有提交，生产pointer已回滚到旧版`592a65a`。

当前工作树已把状态通道重构为不可变快照加Supervisor内存缓存，完整验证与最终独立review已通过，但尚未提交、制作release或重新部署。不得再次激活`4021a56`。

## 事实矩阵

| 事实面 | 状态 | 当前证据 |
|---|---|---|
| 候选代码 | `reviewed-ready-to-freeze` | 新增`campaign_worker_status.py`；固定目标覆盖已替换为launch-token/sequence不可变快照；最终独立复核P0=0/P1=0 |
| 旧候选release | `rejected-by-live-gate` | `4021a56`初始ready，但3-ASIN运行后Worker在`os.replace` WinError 5上耗尽重启；禁止复用 |
| 线上 Collection | `rolled-back-and-live` | current pointer `592a65a`；`/readyz`最近返回200、tenant `local_agent_deployment_20260906`、单Worker running |
| 线上 Operator | `rolled-back` | operator pointer `71db26e` |
| 计划任务运行时 | `migrated-with-ownership-gap` | 两项Action均为`C:/Program Files/PowerShell/7/pwsh.exe`；XML备份和迁移回执存在；回滚后端口进程存活但任务状态显示Ready，下一次发布前须恢复托管为Running |
| 失败3-ASIN门禁 | `stopped` | `campaign-0fa7b5ae1eb658c58e9ef8bae81ec2c6`曾2 succeeded、1 claimed；回滚后目标tenant状态尚未重新对账，不得当作通过或复用 |
| 20条与全量campaign | `not-submitted` | 未提交20条；未提交1,093条 |
| 目标清单 | `verified` | `Amazon Scraping/data/owned_us_asin_20260902_full_01/manifest_1093.csv`共1,093个唯一合法ASIN |
| 代理容量 | `must-refresh` | 34槽配置曾有32个唯一可用并放行三Worker；该事实现在已过期，下一次发布前必须按同配置重跑非Amazon canary |
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

1. 冻结已通过独立P0/P1 review的当前diff并制作新的不可变release；不得覆盖或复用`4021a56`。
2. 以管理员权限恢复两项计划任务为真正Running托管状态，同时保持系统PowerShell7 Action。
3. 刷新34槽非Amazon canary，并确认当前配置哈希、有效期和至少30个唯一槽。
4. 在目标tenant对账并取消/终结失败门禁遗留的claimed成员，确认没有活动campaign/job/lease/reservation。
5. 激活新release，持续观察三Worker而不是只看一次ready；必须requested/effective/alive/healthy=3/3/3/3、restart=0。
6. 使用新的幂等键重新执行3-ASIN门禁；全部终态且无failed/blocked后再执行20条，之后才提交1,093条单一campaign。

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

开场只汇报：候选diff状态、线上pointer/readiness、失败门禁遗留、下一道放行条件。不得把测试通过等同于已部署，不得复用失败campaign或旧幂等键，不得输出代理凭据。

## 可直接复制给新 Agent 的提示词

> 在`D:\woring\爬虫\.worktrees\amazon-integrated-deploy`接手Amazon Collection。先完整读取`AGENTS.md`和`docs/agent_handoff_current.md`，再只读核对分支、提交、工作区、deploy pointers、计划任务状态与`127.0.0.1:8765/readyz`。当前`4021a56`已被3-ASIN生产门禁否决，不得再次激活；先审查未提交的不可变Worker状态快照方案。请用四项回复：候选diff、线上状态、失败门禁遗留、下一放行条件。不要输出凭据，不要跳过3→20→1093门禁。
