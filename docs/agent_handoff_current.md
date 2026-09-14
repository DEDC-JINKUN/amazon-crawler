# Amazon Collection 当前 Agent 交接

**观察时间：** 2026-09-14 16:52（Asia/Shanghai）
**用途：** 新 Agent 的五分钟事实入口。本文是当前快照，不是部署凭证；变化事实必须回到下列权威来源复核。

## 先说结论

最终代码`4021a56`已完成独立R3审查，备用端口真实三Worker达到3/3/3/3；**生产激活仍失败并已恢复旧版`592a65a`单Worker。** 根因转到计划任务wrapper编码：Windows PowerShell5.1无法读取UTF-8无BOM中的中文路径。目标tenant已有1093条、最新34槽canary 34/34通过，但活动campaign/job/lease/reservation均为0，未开始全量。

## 事实矩阵

| 事实面 | 状态 | 当前证据 |
|---|---|---|
| 候选代码 | `reviewed-current` | 最终提交与release `4021a56`；独立R3复核P0=0/P1=0 |
| 线上 Collection | `verified-current` | deploy pointer `592a65a`；旧版单Worker，tenant `local_agent_deployment_20260906`；新凭据下10槽canary通过 |
| 线上 Operator | `verified-current` | operator pointer `71db26e`；不是候选提交 |
| 三 Worker | `production-blocked` | 备用端口3/3/3/3通过，34槽canary 34/34通过；计划任务wrapper编码/管理员续接未稳定完成 |
| 全量 campaign | `protocol available` | MCP/Collection capability 上限 5,000 ASIN；没有因此获得本轮真实采集授权 |
| PostgreSQL 引擎验收 | `verified-current` | 2026-09-14 直接启动专用55444测试实例，完整矩阵29 passed；随后fast shutdown确认关闭 |
| 文档 | `changed-and-verified` | `README.md`、`docs/collection_api.md`、`docs/collection_operator.md`、`CHANGELOG.md` 已与候选代码同步 |
| 生产部署与 live 验证 | `rolled-back` | `4021a56`已安装但pointer恢复`592a65a`；多次激活失败后停止自动重试 |
| 上线后台campaign | `completed` | `campaign-579718cdd550503aba7e5ea996179b80`：2 succeeded、1 variant、0 failed，证据均晚于请求 |
| Agent 记忆 | `generated-read-only` | 不直接编辑宿主生成记忆；以本文件作为项目内交接入口 |

## 候选实现了什么

- Agent 服务通过 `-AgentWorkerCount 1..8` 请求 Worker 数，按新鲜 canary 容量计算 effective Worker，并在 `/readyz` 返回 requested/effective/alive。
- Operator 和 MCP 都使用 `collection_campaign`、campaign member、refresh job 和本轮 evidence；Operator JSON/CSV 只是投影。
- Operator 提交前核对 tenant、`subject_type` 和 Worker pool。容量不足时不创建 campaign。
- campaign 取消、外部过期、连续轮询失败、blocked/failed 分类均有明确终态。
- Windows loopback 10053/10054 抖动有有界处理；没有无限重试。

关键实现：

- [`scripts/operator_campaign_runner.py`](../scripts/operator_campaign_runner.py)
- [`scripts/agent_collection_service.py`](../scripts/agent_collection_service.py)
- [`scripts/agent_service_control.ps1`](../scripts/agent_service_control.ps1)
- [`scripts/collection_api.py`](../scripts/collection_api.py)
- [`scripts/operator_core.py`](../scripts/operator_core.py)

## 验证证据

- 主非 PG 套件：730 passed、56 skipped、0 failed。
- 相关模块：124 passed。
- 专用 `127.0.0.1:55444/operator_matrix` 完整矩阵：29 passed。
- 新增Worker计数与慢请求拒绝边界：连续10轮均2/2通过。
- Python 编译、PowerShell AST、`git diff --check`：通过。

详细过程和未完成项见 [`progress.md`](../progress.md) 与 [`task_plan.md`](../task_plan.md)。

## 三 Worker 的现实容量

当前部署配置只有 10 个会话端口，`proxy_session_retry_per_asin=1`，正式商品会话范围是 `per_asin`。该范围下 `proxy_session_max_asins` 不会合并不同 ASIN；每个 Worker 的 5-action pass需要5个生产槽和5个有界替换槽，共10槽。因此三 Worker需要30个通过同配置 canary 的唯一槽。此前文档写成12槽是错误的；配置变化后仍必须调用容量函数重新计算，不能只抄文档。

即使请求 `AgentWorkerCount=3`，容量不够时 effective 仍会降到 2 或 1。生产判断只看 `/readyz` 的实际值，不能只看启动参数。

## 下一步顺序

1. 在管理员PowerShell执行`D:/woring/爬虫/outputs/prod-full-rollout-20260914/activate_reviewed_release.ps1`；脚本会安装`e345468`、备份pointer/wrapper、激活三Worker并在失败时回滚。
2. 激活后复核目标tenant/subject且`requested_workers=3`、`effective_workers=3`、`alive_workers=3`、`capacity_backpressure=false`。
3. 候选3-ASIN与20-ASIN受控验收通过后，才提交1,093条单一campaign。

当前网页只允许 1/2 Worker。三 Worker在发布候选后应走 MCP campaign；若业务要求网页选择 3，先扩展 Operator 的 1/2 校验和页面选项并补测试。不要使用旧 `crawler.ps1 run -Limit 1093` 代替 campaign。

## 新 Agent 开场检查

```powershell
Set-Location 'D:\woring\爬虫\.worktrees\amazon-integrated-deploy'
git branch --show-current
git log -2 --oneline
git status --short
```

只有任务涉及部署或线上判断时，才只读核对：

```powershell
Get-Content 'D:\woring\爬虫\deploy\amazon-collection\current-release.txt'
Get-Content 'D:\woring\爬虫\deploy\amazon-collection\operator-release.txt'
curl.exe --silent --show-error --max-time 5 http://127.0.0.1:8765/readyz
```

开场回复用户四项即可：候选代码状态、线上状态、剩余门禁、建议的下一步。未获得新授权前，不部署、不提交 campaign、不访问生产库或代理凭据。

## 可直接复制给新 Agent 的提示词

> 在 `D:\woring\爬虫\.worktrees\amazon-integrated-deploy` 接手 Amazon Collection。先完整读取 `AGENTS.md` 和 `docs/agent_handoff_current.md`，再只读核对当前分支、提交、工作区状态；如果任务涉及线上，再核对 deploy pointers 和 `127.0.0.1:8765/readyz`。请先用四项回复我：候选代码状态、线上状态、剩余门禁、建议下一步。不要把提交当成已部署；本轮未明确授权前，不部署、不启动真实采集、不访问生产数据库或代理凭据。
