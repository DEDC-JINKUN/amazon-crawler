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

模块设计：新增一个深模块封装“readiness 校验 → campaign 提交 → 状态投影 → 取消 → 终态回执”。Operator 只调用其单一运行 interface；并发进程只由 Agent service pool 管理，容量降级在 campaign 提交前可见。

## 2026-09-14 生产发布合同

用户已明确授权：发布候选、重启生产服务、使用 DPAPI 凭据、扩充获批会话槽，并按 3 ASIN → 20 ASIN → 1,093 ASIN 自动放行；任一门禁失败立即停止。真实采集仍坚持无登录、无个人 Cookie、无验证码绕过、无 TLS 指纹伪装。

R3 顺序：隔离 PG 门禁 → 独立 diff review → 不可变 release/回滚点 → 30槽非 Amazon canary → 三 Worker readiness → 3条 live → 20条 live → 全量单一 campaign。

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
- [x] 用户确认 Agent campaign 与 Operator start/status/stop 为本轮公共验收 seam。
- [x] 恢复外置 Git 索引基线，保留损坏的原 `.git` 指针不动。
- [x] Agent 服务安全入口可配置 1–8 个请求 Worker，并把 Worker 数纳入运行指纹。
- [x] 修复多 Worker 容量被首个 Worker独占以及历史结果冒充本轮完成。
- [x] 将 Operator runner 切换为 Collection campaign Adapter，删除生产路径对 ordinary two-worker Controller 的依赖。
- [x] Operator 状态投影保留 campaign ID、请求/实际/存活 Worker 数以及 blocked/failed 区分。
- [x] Operator 提交前核对 Collection tenant 与 subject scope；campaign 截止时间独立配置。
- [x] Windows loopback 鉴权响应与幂等 campaign 调用具备有界抗重置处理。
- [x] 完整非 PG 测试、差异复核与文档一致性验收。
- [x] 隔离 PostgreSQL 55444 的 17 个引擎测试；连同非 PG 矩阵共 29 passed。
- [x] 独立 R3 差异审查无 P0/P1（最终冻结代码提交`b714556`）。
- [x] 第三轮修复第二次复审的3个P1，并完成724项非PG与29项隔离PG回归。
- [ ] 制作并部署不可变 release，保留旧 pointer 作为回滚点（ZIP与激活脚本已预检，待管理员执行）。
- [ ] 三 Worker容量与 readiness 通过（30槽canary已通过，服务readiness待激活）。
- [ ] 3条与20条真实验收通过（旧上线版3条已通过，20条待候选激活）。
- [ ] 提交1,093条单一全量 campaign。
- [ ] 第二轮独立复审新增3个P1；按用户“任一门禁失败立即停止”要求，发布流程已停止，等待是否授权第三轮代码修复。
- [ ] 第三轮独立复审仍有3个P1；自动修复循环再次停止：并发降级仍ready、cancel返回completed竞态、completed但成员投影暂缺被记failed。
- [x] 第四轮TDD：requested/effective不一致时Agent readiness阻断。
- [x] 第四轮TDD：cancel返回completed时Operator保持completed。
- [x] 第四轮TDD：权威campaign completed不被本地暂缺成员投影改写为failed。
- [x] 第四轮聚焦/全套/隔离PG验证、文档同步和独立R3 delta review。
- [x] 第四轮独立复审新增P1：Worker计数字段部分缺失必须fail closed。
- [x] 第四轮独立复审新增P1：拒绝POST的请求体drain必须有固定总时间上限。
- [x] 最终delta复核遗留P1：完整Worker计数字段必须严格相等而不是仅大于等于requested。
- [ ] 在隔离环境完成 1W/2W 对照；不执行 Amazon live 验证。
- [x] 用户从DataImpulse Proxy Access取得实际代理凭据；10000与823均CONNECT 200/TLS1.3，10槽与30槽canary通过。
- [x] 目标tenant事务登记1093条，且无活动campaign/job/lease/reservation。
- [ ] 管理员运行`outputs/prod-full-rollout-20260914/activate_reviewed_release.ps1`完成release安装与三Worker切换。
- [x] 真实候选启动复现Windows状态文件原子替换竞态并完成红绿修复。
- [ ] 对状态文件修复跑全套、独立R3 review并制作新不可变release；`e345468`不得再次激活（两轮全套已过，待最终review/release）。
- [x] 修复独立review发现的监督器重启预删锁竞态并补红绿测试。

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| Collection capability 首次返回 `backend_unavailable` | 1 | 只读检查 loopback 服务；稍后健康检查与 capability 重试均返回 200，记录为短暂不可用，未重启服务。 |
| 工作区根目录 `rg` 遇到若干无权限测试临时目录 | 1 | 后续检索限定到目标仓库并排除 `.worktrees`、pytest 与 outputs。 |
| 一次 PowerShell 状态组合命令出现 empty pipe parser error | 1 | 拆成简单的 `Test-Path` 命令，未重复原命令。 |
| 本目录 `.git` 指向已丢失的 worktree 管理目录 | 1 | 将其视为隔离重构快照；暂不做产品改动，先请用户确认公共 seam 和权威源码位置。 |
| pytest 默认临时目录 `C:/Users/PC/AppData/Local/Temp/pytest-of-PC` 无权限 | 1 | 改用工作区内新的 `--basetemp` 与 cache 目录；同一组测试 3 passed。 |
| 新增取消测试时把基类方法误插入子类，导致一个完成用例缺少 `submit_campaign` | 1 | 读取测试文件后恢复正确类层级；Operator campaign 测试全部通过。 |
| 隔离 PostgreSQL 首次启动把 `-o` 参数错误组合成一个 port 值 | 1 | 读取日志确认纯命令参数问题；第二次改为使用集群既有配置。 |
| 隔离 PostgreSQL 第二次启动被当前执行环境拒绝绑定 `127.0.0.1:55444` | 2 | 日志为 `could not create any TCP/IP sockets: Permission denied`；未尝试生产 55443/5432，改跑无数据库网络的完整回归并把 PG 引擎验收列为未执行。 |
| 全套回归中的 Collection API 无权限 POST 偶发 WinError 10053 | 1/10 | 根因为拒绝前未读取小请求体；认证拒绝前有界丢弃后连续 20 次通过。 |
| 第一次请求体丢弃补丁产生 Python 缩进错误 | 1 | 立即读取报错行、修正缩进并先做 `py_compile`；未叠加其他修复。 |
| campaign cancel 在 Windows loopback 偶发 WinError 10053 | 1 | 在 loopback Client Adapter 对 10053/10054 做一次有限重试；campaign API 场景连续 10 次通过。 |
| 生产只读探针首次把 `dict_row` 当数组读取 | 1 | 修正为取 mapping 首值后重跑；确认探针只读且生产 schema 完整。 |
| 先前把三 Worker容量写成12槽 | 1 | 重新按 `per_asin + retry=1 + 5-action pass` 调用链核对：每 Worker 10槽，三 Worker需30个唯一槽；修正文档并以 canary/readiness 实值为放行依据。 |
| 第二轮独立复审仍有3个P1 | 2 | 按R3与用户门禁立即停止自动发布；保留未提交修复现场，等待用户决定是否继续第三轮代码修复。 |
| 第三轮中间聚焦测试一次loopback job查询超时并锁住临时SQLite | 1 | 单测复跑通过；最终完整非PG套件724 passed，未修改本轮无关的GET job路径。 |
| 第三轮独立复审仍有3个P1 | 3 | 按R3收敛规则停止继续修改与发布；保留提交`6b84fa1`和审查证据，等待用户决定是否再开一轮定点修复。 |
| 当前上线版提交1093条完整campaign返回`asin_not_found` | 1 | 服务与Worker保持运行；请求在建批前原子拒绝，未重试。先查明生产租户ASIN登记/导入入口，再决定是否提交。 |
| 第四轮完整回归在专用PG门禁文件处出现fixture errors/长等待 | 1–3 | 首次误继承已关闭55444 DSN；随后确认`test_operator_acceptance_matrix.py`与`test_operator_batch_store_v2.py`都按设计主动要求专用PG而非skip。中止无效运行，最终按既有门禁排除这两个文件跑非PG套件，再启动55444单跑二者共29项。 |
| 候选目录下未找到专用PG数据目录 | 1 | 未启动任何数据库；按既有记录改为核对工作区根`D:/woring/爬虫/.test-tmp/operator-matrix-postgres`，继续确认端口配置后再启动。 |
| 生产只读Worker诊断首次查询假设了不存在的recovery列 | 1 | PostgreSQL返回`UndefinedColumn/42703`且未执行写操作；改为`to_jsonb(row)`读取实际行结构，再在Python中筛选非敏感状态字段。 |
| 当前上线版10槽非Amazon canary全部`network_error` | 1 | 10/10 connect/TLS失败、可用/唯一槽均0，容量Gate按设计拒绝；不重试、不访问Amazon，3-ASIN campaign保持queued等待有效容量事实。 |
| PowerShell 7的HTTP异常对象不支持`GetResponseStream` | 1 | 只读readyz读取失败；改用`curl.exe`同时记录状态码与JSON正文。 |
| 冻结提交缺少本仓库本地Git作者配置 | 1 | 未生成提交；沿用现有提交作者`Codex <codex@local>`写入仅本仓库配置后重试。 |
| 交接文档观察时间行保留Markdown双空格导致`git diff --check`失败 | 1 | 删除行尾空格后重跑，未生成不合格提交。 |
| 生成PowerShell凭据轮换入口时JS模板误解释`${...}` | 1 | 文件未写入；改用PowerShell字符串拼接后由`apply_patch`成功创建。 |
| 交互终端首次指定了不存在的`C:/Program Files/PowerShell/7/pwsh.exe` | 1 | 脚本未运行、vault未修改；改由当前Codex PowerShell宿主直接执行脚本。 |
| 代理凭据轮换在PowerShell 7缺少`[IO.File]::SetAccessControl` | 1 | 失败发生在临时vault ACL阶段、原vault未替换；改用`icacls.exe`限制当前账户和SYSTEM后重试。 |
| 三Worker配置离线hash验证缺少credential generation | 1 | 未改生产；对子进程注入固定离线generation后确认30个唯一端口、15 action需要30槽。 |
| 当前有限令牌不能写受保护的`deploy/releases` | 1 | archive已生成但release目录无半成品；保留管理员/SYSTEM只写ACL，不放宽权限，改用提升权限且自动回滚的激活脚本。 |
| 从Codex进程触发RunAs未显示可操作UAC且长期等待 | 1 | current pointer仍为`592a65a`、候选目录不存在；中止等待，改由用户在管理员PowerShell手工执行已预检脚本。 |
| 首个管理员CMD启动器被`cmd.exe`按LF/UTF-8中文路径错误解析 | 1 | 激活脚本未执行；改成ASCII临时路径、CRLF和PowerShell 7启动器。 |
| `e345468`三Worker激活45秒内readiness失败 | 1 | 自动回滚成功；备用端口复现子Worker心跳`os.replace` WinError 5，禁止重试旧release。 |
| 一次PowerShell诊断命令在try/catch后直接管道导致empty pipe parser error | 1 | 拆成变量赋值与最终单次JSON输出，取得真实503状态。 |

## 本轮自测

恢复/重复收尾新增检查先得到 4 个失败；CSV finalize 检查再得到 4 个失败，均在候选代码修复。运行全部现有测试时移除生产 DSN/代理/API key 环境变量，只向新增矩阵提供独立 55444 数据库。
完整矩阵尚缺真实提交→数据库成员→Worker→页面贯通。单元/接口检查及既有 fake runner 测试不计发布轮数，仍为 0/3。
