# Amazon Collection 当前 Agent 交接

**观察时间：** 2026-09-13 14:19（Asia/Shanghai）  
**用途：** 新 Agent 的五分钟事实入口。本文是当前快照，不是部署凭证；变化事实必须回到下列权威来源复核。

## 先说结论

当前已经有一个修复候选，解决了 Operator 与 Agent 批次分叉、历史结果冒充本轮完成和双 Worker 分别抢容量的问题；**候选尚未部署，线上仍不是三 Worker，也不具备现在直接全量开跑的条件。**

## 事实矩阵

| 事实面 | 状态 | 当前证据 |
|---|---|---|
| 候选代码 | `locally verified` | 分支 `codex/multiworker-fix-20260913`，提交 `30d4787` |
| 线上 Collection | `verified-current` | deploy pointer `592a65a`；`127.0.0.1:8765/readyz` 返回旧版单 `refresh_worker`，tenant 为 `local_agent_deployment_20260906` |
| 线上 Operator | `verified-current` | operator pointer `71db26e`；不是候选提交 |
| 三 Worker | `pending` | 候选服务允许请求 1–8 Worker，但当前线上未部署；网页 Operator 仍只接受 1/2 |
| 全量 campaign | `protocol available` | MCP/Collection capability 上限 5,000 ASIN；没有因此获得本轮真实采集授权 |
| PostgreSQL 引擎验收 | `pending` | 17 个隔离 55444 测试未运行；当前执行环境拒绝绑定该测试端口 |
| 文档 | `changed-and-verified` | `README.md`、`docs/collection_api.md`、`docs/collection_operator.md`、`CHANGELOG.md` 已与候选代码同步 |
| 生产部署与 live 验证 | `pending` | 本轮没有发布、迁移、访问生产库或启动 Amazon 采集 |
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

- 主非 PG 套件：713 passed、56 skipped、0 failed。
- Operator 非 PG 状态矩阵：12 passed。
- 未运行：17 个必须连接专用 `127.0.0.1:55444/operator_matrix` 的 PostgreSQL 测试。
- 鉴权 Windows loopback 重复：20/20 通过。
- campaign 提交/取消重复：10/10 通过。
- Python 编译、PowerShell AST、`git diff --check`：通过。

详细过程和未完成项见 [`progress.md`](../progress.md) 与 [`task_plan.md`](../task_plan.md)。

## 三 Worker 的现实容量

当前部署配置只有 10 个会话端口，`proxy_session_max_asins=3`、`proxy_session_retry_per_asin=1`；按候选当前默认的 per-ASIN 计算，每个 Worker 的 5-action pass 需要 4 个槽，因此三 Worker 至少需要 12 个通过同配置 canary 的唯一槽。这个数字是基于 2026-09-13 配置和代码的推导，配置变化后必须重新计算。

即使请求 `AgentWorkerCount=3`，容量不够时 effective 仍会降到 2 或 1。生产判断只看 `/readyz` 的实际值，不能只看启动参数。

## 下一步顺序

1. 在允许绑定 55444 的隔离环境运行剩余 17 个 PostgreSQL 测试；完成标准是 0 failed、0 error，不能用生产 55443/5432 替代。
2. 对提交 `30d4787` 做独立差异审查，修复阻断项后重新跑门禁。
3. 如确需三 Worker，配置至少 12 个获批会话槽并完成同配置非 Amazon canary；不要通过放宽验证码、TLS、Cookie 或登录规则换容量。
4. 从审查后的提交制作不可变 release，更新 deploy pointer；部署与服务重启需要用户明确授权。
5. live verify 必须看到目标 tenant/subject 且 `requested_workers=3`、`effective_workers=3`、`alive_workers=3`、`capacity_backpressure=false`。
6. 依次做 3 ASIN、20 ASIN 受控验收；通过后才讨论全量 campaign。真实 Amazon 请求需要单独明确授权。

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
