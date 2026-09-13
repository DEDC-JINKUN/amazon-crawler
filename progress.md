# 当前进展

- 冻结干净基线 71db26e。
- 隔离 PostgreSQL 初始化成功；沙箱启动 pg_ctl 受限，按审批提升后仅启动测试端口55444。
- 基础状态/历史隔离红测明确失败，修复后通过。
- 候选数据库批次仓储和成员租约测试已加入。
- 最新隔离基础检查加既有 Operator 回归：83 passed，包含重叠并发和嵌套清理检查。没有计为完整八场景通过。
- 完整八场景尚未全部贯通，连续发布通过轮数 0/3。
- 生产服务、指针、清单及展示均未修改。

## 用户要求自行测试

- 对恢复/重复终态先跑出 4 个失败，并修复原始结果持久化与终态幂等。
- 对历史隔离后的 CSV finalize 跑出 4 个失败，并修复输出列投影。
- 修改后运行全套现有检查（包含26个隔离批次基础检查）：722 passed，55 skipped，2 deselected。JUnit：D:/woring/爬虫/.test-tmp/operator-independent-full.xml。
- 55项未运行与2项排除均不计通过；没有把上述结果计入三轮完整场景验收。
- 部署门仍关闭：实际启动器/Worker/大屏尚未接入新数据库批次模型，完整矩阵轮数0/3。

## 2026-09-13

- 只读核对主仓库、现行部署、MCP capability、loopback health、历史 Operator 批次回执和 2W child receipt。
- 未启动 campaign、未访问 Amazon、未读取代理凭据、未访问生产数据库、未修改已部署 release。
- 确认 Agent campaign API 支持最多 5,000 ASIN，但当前服务运行态为单 Worker。
- 用历史回执复现出两个独立根因：分子 Worker 容量预约导致第二个 Worker 被拒；Operator 历史投影与本轮 run evidence 不一致导致“页面完成、运行失败”。
- 发现当前隔离重构目录的 Git worktree 管理路径已丢失；在用户确认公共 seam 和权威源码前暂停产品代码/测试变更。
- 用户确认 Agent campaign 与 Operator start/status/stop 为公共验收 seam。
- 建立外置 Git 索引基线；未提交、未部署。
- TDD slice 1：WorkerCount 控制入口红测 1 failed；实现后与容量降级、Console 启动失败保护合计 3 passed。
- 完成共享模块设计：campaign 是权威事实源，Operator artifacts 是投影；Agent service pool 是唯一 Worker 编排者。
- TDD slice 2–7 完成：容量不足前置拒绝、campaign-only 进度、停止取消、loopback readiness、Operator/Agent 端口传播、Worker pool readiness、外部过期失败、轮询失败取消、blocked 分类均有红绿证据。
- 聚焦回归：106 passed；随后 Operator runner 当前测试 7 passed。PowerShell AST 检查四个改动入口均为 0 error。
- README、Collection API、Operator 手册与 CHANGELOG 已同步为共享 campaign 路线。
- 尝试启动既有隔离 PostgreSQL 55444：第一次命令引号错误；第二次被环境拒绝绑定 loopback。没有连接 55443/5432，也没有用生产库替代测试库。
- 最终非 PG 套件在补齐本地 raw 目录和 geckodriver 测试夹具后达到 713 passed、56 skipped；另有 12 个不依赖 PG 的 Operator 状态矩阵通过。17 个必须使用隔离 55444 的测试未执行。
- 修复 Windows loopback RST：鉴权用例 20/20 通过；campaign API 幂等提交/取消用例 10/10 通过；Client/API/Operator campaign 聚焦回归 18 passed。
