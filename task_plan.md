# Operator 状态链整体验收

冻结基线：71db26e。生产展示继续运行；禁止使用生产清单、凭据、数据库及端口做调试。部署门关闭，连续通过轮数 0/3。

## 状态契约

上传预览不发布采集；确认提交后即持久化 batch_id、成员 ASIN、subject_type、提交时间和采集策略，并立即可查询。

queued → canary → reserving → collecting → finalizing → releasing → completed / failed / stopped。
任意中间阶段的取消或异常都进入清理；只有确认所有子进程退出、全部容量释放才能退出 releasing。清理失败保留 cleanup_pending，禁止下一批。

历史商品是独立只读视图；本批次进度仅来自本 batch_id 绑定的 run/成员证据。重新采集不能隐式复用历史完成数，也不能自动清零访问控制预算。

## 隔离场景矩阵

正常完成、canary 失败、CAPTCHA、canary 停止、采集停止、异常退出、停止后立即下一批、服务重启恢复。
交叉覆盖 workers=1/2、members=1/3/10/20、全新/完全重复/部分重叠；检查页面、任务归属、证据、进程、租约与回执一致。
使用真实生产接口配合本机可控 HTTP/故障适配器，单独 PostgreSQL 数据目录和端口；不能仅测试 fake runner 的自造状态。

## 2026-09-13 续接目标

用户要求判断是否可由 Agent 操控全量采集，并修复多 Worker 问题。当前仍遵守：不启动真实采集、不读取代理凭据、不访问生产数据库、不修改已部署 release。

拟议的公共验收 seam（待用户确认）：

1. `campaign_submit/status/wait/cancel` 作为 Agent 全量批次入口；
2. Operator `start/status/stop` 作为人工大批次入口；
3. 两者共享数据库批次成员、容量预约、租约、运行证据和终态，不允许各自计算一套进度。

修复顺序：先以隔离 PostgreSQL 和可控适配器跑出失败；再统一批次成员归属与容量预留；最后验证 1/2 Worker 的提交、状态、停止、崩溃恢复和无重复领取。用户确认 seam 前不写新测试或实现。

## 进度

- [x] 冻结基线，工作树干净。
- [x] 梳理状态写入者，记录统一协调器与数据库成员模型决策。
- [x] 建立独立数据库；现有基础检查均不使用网络采集，完整场景网络隔离待接入。
- [ ] 先跑完整矩阵得到基线失败证据。
- [ ] 统一批次模型、证据归属、容量与停止清理。
- [ ] 每次修改重跑完整矩阵；同类失败第三次改设计。
- [ ] 同一代码指纹连续三轮全矩阵通过（干净、历史、恢复）。
- [ ] 审查后部署与非采集运行态验证。
- [ ] 用户确认 Agent campaign 与 Operator start/status/stop 为本轮公共验收 seam。
- [ ] 修复多 Worker 容量被首个 Worker独占以及历史结果冒充本轮完成。
- [ ] 在隔离环境完成 1W/2W 对照；不执行 Amazon live 验证。

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| Collection capability 首次返回 `backend_unavailable` | 1 | 只读检查 loopback 服务；稍后健康检查与 capability 重试均返回 200，记录为短暂不可用，未重启服务。 |
| 工作区根目录 `rg` 遇到若干无权限测试临时目录 | 1 | 后续检索限定到目标仓库并排除 `.worktrees`、pytest 与 outputs。 |
| 一次 PowerShell 状态组合命令出现 empty pipe parser error | 1 | 拆成简单的 `Test-Path` 命令，未重复原命令。 |
| 本目录 `.git` 指向已丢失的 worktree 管理目录 | 1 | 将其视为隔离重构快照；暂不做产品改动，先请用户确认公共 seam 和权威源码位置。 |

## 本轮自测

恢复/重复收尾新增检查先得到 4 个失败；CSV finalize 检查再得到 4 个失败，均在候选代码修复。运行全部现有测试时移除生产 DSN/代理/API key 环境变量，只向新增矩阵提供独立 55444 数据库。
完整矩阵尚缺真实提交→数据库成员→Worker→页面贯通。单元/接口检查及既有 fake runner 测试不计发布轮数，仍为 0/3。
