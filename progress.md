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

## 2026-09-14

- 用户确认授权生产发布、DPAPI受控启动、会话槽扩容和 3→20→1093 自动放行；任一门禁失败停止。
- 直接前台启动已有隔离 PostgreSQL 数据目录成功绑定 `127.0.0.1:55444`；完整 Operator 矩阵 29 passed，其中包含此前缺失的17项PG引擎测试。
- 测试后以 Ctrl+C 关闭隔离 PostgreSQL；`pg_ctl status`确认无服务，55444无监听。
- 已派发一次只读独立 R3 差异审查；未访问生产库/代理凭据，尚未部署或提交 campaign。
- 经用户授权用 DPAPI 向一次性子进程只注入生产 DSN，执行只读探针：schema无缺表、目标 tenant 当前0条、旧 local tenant 3条；两个 tenant 均无活动 campaign/job/item lease/capacity reservation，目标 tenant 尚无 canary。
- 独立R3审查：P0=0，P1=3，发布继续保持停止。P1为多进程readiness未汇总内部健康/持续容量、子Worker无监督恢复、Runner丢失后Operator不能独立取消campaign。
- 纠正容量口径：当前per-ASIN策略下每 Worker 5-action pass需要10槽，三 Worker需要30个唯一槽；12槽结论作废。
- 第一轮P1修复后聚焦回归114 passed；完整非PG回归719 passed、56 skipped；隔离PG矩阵再次29 passed，测试PostgreSQL已用fast shutdown关闭。
- 第二轮独立delta复审仍为STOP：业务心跳不能证明Worker线程未卡死；cancel失败仍可能被BatchManager记为stopped；BatchManager可能把已完成campaign被晚到stop改写。按用户门禁停止，未部署、未改生产配置、未提交campaign。
- 用户授权继续第三轮代码修复。新增Worker线程业务心跳与超时有限重启；取消未确认保持`cancel_pending`且阻止下一批；后端completed优先于晚到stop。第三轮聚焦回归117 passed，尚待全套、PG和独立审查。
- 第三轮最终非PG套件724 passed、56 skipped、0 failed；隔离PG矩阵29 passed并fast shutdown。一次中间聚焦运行出现旧loopback job查询超时，单测复跑通过，最终全套无复现。
- 第三轮修复冻结提交`6b84fa1`。独立R3审查仍STOP：requested 3降为effective 1/2时仍可能ready；cancel调用返回completed时Runner仍可能写stopped；campaign_status completed但成员投影暂缺时BatchManager会写failed。另有Supervisor强杀/Job Object/控制文件竞态P2。未发布、未改生产配置、未提交campaign。
- 用户要求先保持上线爬虫运行再继续修复。核对release `592a65a`：`/healthz`与`/readyz`均200，单Worker为running。1093条清单校验为1093个唯一合法ASIN；使用稳定幂等键提交完整campaign时生产API以`asin_not_found`原子拒绝，未创建campaign。服务继续运行，等待核对现行ASIN导入入口。
- 未改变生产租户边界；改为对上线版已登记的3个ASIN提交后台campaign `campaign-579718cdd550503aba7e5ea996179b80`。API返回202，状态active，3项queued；release仍为`592a65a`、单Worker，未使用候选代码。
- 生产只读诊断确认3个refresh/recovery job预算均为new且未暂停；当前唯一容量事实已过期。随后按门禁对10个批准槽执行一次非Amazon canary：10/10为`network_error`，available/unique=0，Gate=`denied:unique_capacity_insufficient`。未访问Amazon、未重复探测；campaign保留queued，不能宣称正在实际抓取。
- 第四轮针对独立复审3个P1完成TDD：requested/effective/alive降级时API与`/readyz`阻断；cancel返回completed时后端终态优先；BatchManager不再把权威completed因本地成员投影暂缺改成failed。Windows 503拒绝路径补齐有界请求体读取，相关循环20轮稳定。
- 第四轮验证：相关模块122 passed；完整非PG套件727 passed、56 skipped；专用55444两文件29 passed并fast shutdown，确认55444无监听；Python编译与`git diff --check`通过。待冻结提交与独立R3 delta review。
- 第四轮冻结提交`328e8f9`。独立R3 delta review确认原3个P1已关闭，但新增2个P1：部分Worker计数字段fail-open；拒绝POST drain缺固定总时间上限。按用户“继续修”授权进入定点TDD，仍未部署。
- 两个复审P1均完成红绿：Worker计数字段全无时才允许旧单Worker兼容，部分出现即503；拒绝body drain使用固定总deadline。新增场景2 passed，Collection API文件25 passed；文档与CHANGELOG同步。
- 复审修复后的新鲜验证：相关模块124 passed；完整非PG 729 passed、56 skipped；专用55444矩阵29 passed并fast shutdown；两个新增边界连续10轮均2/2通过。待冻结delta并由原独立reviewer复核。
- delta提交`55fc6ce`复核确认部分字段与固定drain deadline两个P1均关闭；遗留1个P1：完整计数使用`>=`而非严格相等，发布继续阻断。
- 最后一个P1完成红绿：完整计数字段改为`requested == effective == alive`。四个readiness/drain边界4 passed；修复后完整非PG 730 passed、56 skipped；专用55444矩阵29 passed并fast shutdown；待最终只读复核。
- 最终冻结代码提交`b714556`经独立R3复核：P0=0、P1=0，可继续发布门禁。未部署；生产仍为`592a65a`单Worker，3-ASIN campaign因当天10槽非Amazon canary 0/10可用而queued，`/readyz`为503 `canary_fact_denied`。
- 继续诊断生产容量阻断：DNS、代理TCP和直连非Amazon目标正常；部署vault与旧源码vault分别进行一次脱敏CONNECT，均返回407。根因确定为供应商拒绝现有凭据。已在outputs准备仅替换代理字段的交互式DPAPI轮换入口，尚未运行、未改vault。
