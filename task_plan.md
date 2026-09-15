# Operator 状态链整体验收

长期状态协议代码`8c1d9d7`已随release`33fd636`上线，最终独立review P0=0/P1=0。生产三Worker与计划任务托管健康；3-ASIN门禁因2个代理transport失败而终止，用户随后明确要求直接开始全量，1093条单一campaign已于2026-09-14 22:06提交并开始领取。

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

用户要求由Agent操控全量采集并修复多Worker。凭据只经DPAPI注入且不输出；原3→20→1093自动放行约束在3条失败时已正确停止，用户随后以“开始全量爬取”明确发起全量campaign。

现役公共验收 seam：

1. `campaign_submit/status/wait/cancel` 作为 Agent 全量批次入口；
2. Operator `start/status/stop` 作为人工大批次入口；
3. 两者共享数据库批次成员、容量预约、租约、运行证据和终态，不允许各自计算一套进度。

修复顺序：先以隔离 PostgreSQL 和可控适配器跑出失败；再统一批次成员归属与容量预留；最后验证 1/2 Worker 的提交、状态、停止、崩溃恢复和无重复领取。用户确认 seam 前不写新测试或实现。

模块设计：新增一个深模块封装“readiness 校验 → campaign 提交 → 状态投影 → 取消 → 终态回执”。Operator 只调用其单一运行 interface；并发进程只由 Agent service pool 管理，容量降级在 campaign 提交前可见。

## 2026-09-14 生产发布合同

原授权要求按3 ASIN→20 ASIN→1,093 ASIN自动放行并在任一门禁失败时停止，该流程已在3条失败时正确停止；用户于2026-09-14 22:06另行明确启动1093条全量campaign。真实采集仍坚持无登录、无个人Cookie、无验证码绕过、无TLS指纹伪装。

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
- [x] 制作并部署不可变 release，保留旧 pointer 作为回滚点（`4021a56`已安装并曾激活；业务门禁失败后pointer已回滚）。
- [x] release`33fd636`三Worker容量与readiness持续通过：激活30样本、3条业务门禁及全量提交后均3/3、restart=0。
- [ ] 3条与20条真实验收通过（`33fd636`新3条为1 succeeded/2 transport failed，已取消；20条未提交）。
- [x] 提交1,093条单一全量campaign：`campaign-b14bef6a4b39509a9c92e1ff2629386b`；不得重提或拆分。
- [x] 第二轮独立复审新增3个P1，后续TDD与delta复核已关闭。
- [x] 第三轮独立复审的并发降级、cancel/completed竞态和成员投影问题均已关闭并通过后续全量回归。
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
- [x] 管理员运行`outputs/prod-full-rollout-20260914/activate_reviewed_release.ps1`完成`4021a56`安装与初始三Worker切换；3-ASIN业务门禁证伪后已回滚。
- [x] 真实候选启动复现Windows状态文件原子替换竞态并完成红绿修复。
- [x] 对状态文件修复跑全套、独立R3 review并制作新不可变release；`e345468`不得再次激活（最终代码`4021a56`、独立review通过）。
- [x] 长期运行时：机器级安装微软签名PowerShell 7.6.6 WiX/MSI。
- [x] 备份并迁移AmazonCollectionAgentService/Postgres任务到固定系统pwsh路径与`-NonInteractive`。
- [x] 用迁移后的计划任务重启旧release验证，再激活`4021a56`并取得初始三Worker readiness；业务门禁随后暴露独立状态协议故障。
- [x] 完成不可变Worker状态快照与Supervisor内存缓存；全套、隔离PG、Windows持久并发和独立review全部通过（最终P0=0/P1=0）。
- [x] 冻结长期状态协议代码提交`8c1d9d7`，未纳入工作区既有未跟踪数据/目录。
- [x] 制作release`33fd636`并恢复两项计划任务为系统PowerShell7托管Running；三Worker连续30样本和业务运行均3/3、restart=0。
- [ ] 监控全量campaign同一ID的终态、Worker健康和transport/blocked比例；只有用户明确要求停止才cancel。
- [x] 修复独立review发现的监督器重启预删锁竞态并补红绿测试。
- [x] 修复复核发现的PID复用旧状态冒认：每次spawn绑定launch token并补红绿测试。
- [x] 修复复核发现的旧failed/blocked跨launch污染，并覆盖三种旧状态。
- [x] 修复Windows venv启动器父PID与Worker子PID分层，并取得备用端口真实3/3/3 readiness。

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
| 新鲜30槽canary仅28个可用 | 1 | Gate以`replacement_capacity_insufficient`拒绝；增加4个批准替换槽后34/34通过，未放宽所需30槽。 |
| `4021a56`计划任务启动立即退出1 | 1 | 根因为激活脚本将含中文路径的wrapper写成UTF-8无BOM，Windows PowerShell5.1解码乱码；PowerShell7直接运行同wrapper已3/3/3通过。 |
| BOM续接管理员脚本未产生终态回执且回到旧pointer/服务未启动 | 1 | 立即显式恢复`592a65a`服务并验证ready；达到多次激活失败停止规则，不再自动重试。 |
| Phase 2扩大回归中真实HTTP Operator夹具仍使用旧`load_campaign(campaign_id)`签名 | 1 | 更新测试协议实现接受成员视图关键字后，真实loopback seam单跑1 passed。 |
| Windows Operator真实进程夹具在status和finally stop各10秒超时 | 1 | 检查确认无测试监听/进程残留；独立单跑1 passed，判为并行扩大组的环境抖动，未改产品代码。 |
| 批次上限5→3后Agent服务8项统一报`AssertionError` | 1 | 根因是共享测试Storage仍硬编码limit=5；同步协议夹具为3后Agent服务52 passed，产品代码无额外修复。 |
| 核心context正向测试初版复用了必然抛错的`PostalPartialAdapter.fetch_browser` | 1 | 在核心测试子类显式返回US/USD HTML；策略实现未改，核心/非核心对照2 passed。 |
| external metrics隔离PG首次重导把默认tuple row直接`dict()` | 1 | 默认连接改用psycopg `dict_row`；测试事务已finally清理且55444关闭，随后重跑完整导入/分层场景。 |
| 隔离PG Agent E2E在Worker仍`starting`时立即POST得到503 | 2 | 同时修正静态满批容量门禁为每Worker至少1条；测试在业务请求前显式等待并断言Worker running，保留API启动期fail-closed语义后定点重跑。 |
| Agent E2E新增显式ready等待后缺少`time`导入 | 1 | 仅补测试模块import，未改产品路径；55444已fast shutdown后重新定点运行。 |
| Phase 7复审后把不同PG合同的套件误放进同一库 | 1 | 改用全新UTF-8本地集群，分别向`operator_matrix`和带安全marker的recovery测试库运行；未连接生产库。 |
| PG套件默认临时目录ACL/长路径失败 | 2 | 改用仓库内短`--basetemp`重跑，排除Windows夹具目录假失败。 |
| 浏览器预算旧测试与核心分层/局部终态语义不一致 | 1 | 显式标记`own_core`；局部240次预算耗尽只终结该商品并返回批次处理数2；租约失效场景补`half_open=true`，三分支通过。 |
| 完整非PG套件连续两轮在Console 405处WinError 10053 | 2 | 根因是不支持的PUT/DELETE未读取小请求体；增加固定0.25秒/1MiB有界drain，同一loopback测试连续20/20及最终全套777项通过。 |
| neat-freak自带`audit-inventory.sh`无法运行 | 1 | 本机没有WSL/bash；改用PowerShell与`rg --files`等价盘点Markdown、规则链、Git状态和关键文件体量。 |
| 组合运行态探针命令被策略拒绝 | 1 | 移除动态临时文件函数与清理，拆成pointer、任务和三个简单`curl --max-time 5`只读探针。 |
| 递归hash盘点遇到pytest/data ACL拒绝 | 1 | 不遍历运行目录；改用source manifest明确allowlist逐文件计算SHA256，并排除`data/outputs/.test-tmp/__pycache__`。 |
| PowerShell `foreach`结果直接接管道出现empty pipe parser error | 1 | 先收集到任务专用`$rows`数组，再单独序列化；未产生文件写入。 |

## 2026-09-15 全量运行阻断修复

目标：不新建或拆分`campaign-b14bef6a4b39509a9c92e1ff2629386b`，修复3 Worker在真实访问阻断后留下Firefox孤儿、永久blocked及重启容量残留的问题；发布前保持campaign原成员与幂等键不变。

- [x] 删除Amazon定时监控，改为当前任务直接修复。
- [x] 生产证据：全量20/1093终态后3 Worker全部blocked，readyz=503；发现6棵/33个孤儿Firefox，清理后页面立即从超时恢复到75ms。
- [x] 生产证据：普通restart残留旧Worker 10槽reservation，使新第三Worker`capacity_reserved_elsewhere`；受控停Agent、释放3个死owner reservation/claimed lease后可恢复3/3。
- [x] 追踪浏览器关闭、访问阻断传播和Worker supervisor退出调用链，形成单一根因假设。
- [x] TDD：浏览器异常/驱动丢失后完整进程树可回收，不遗留headless Firefox。
- [x] 核对阻断边界：单个ASIN blocked本来会继续，只有连续/滑窗阈值触发全局熔断；保持fail closed，不自动换出口绕验证码。恢复授权或租约无法证明仍为全局拒绝。
- [x] TDD：服务重启前释放受管Worker reservation与claimed lease，避免第三Worker启动竞态。
- [x] 聚焦回归、完整非PG、隔离55444、Windows真实进程验收、独立P0/P1 review：210 focused、762/56 non-PG、29 PG，最终P0=0/P1=0 CONTINUE。
- [x] 制作不可变release`28d62ce`并恢复同一campaign；初始3 Worker 3/3、终态20→53持续推进，未重提/拆分/取消。11:38一个Worker按`access_blocked`全局熔断停止，剩余2个继续；生命周期无孤儿/脏owner，Amazon访问质量为剩余外部风险。

## 本轮自测

恢复/重复收尾新增检查先得到 4 个失败；CSV finalize 检查再得到 4 个失败，均在候选代码修复。运行全部现有测试时移除生产 DSN/代理/API key 环境变量，只向新增矩阵提供独立 55444 数据库。
完整矩阵尚缺真实提交→数据库成员→Worker→页面贯通。单元/接口检查及既有 fake runner 测试不计发布轮数，仍为 0/3。

## 2026-09-15 采集效率优化 Goal（active）

目标：保留且不重提/拆分/取消`campaign-b14bef6a4b39509a9c92e1ff2629386b`，在不绕验证码、不降低fail-closed门禁的前提下，将固定20-ASIN受控业务门禁提升到可信成功率≥90%、access-block≤3%、browser fallback≤10%、持续可信终态≥100/小时，并将1093规模campaign状态查询p95降到≤2秒。

- [complete] Phase 1：冻结当前吞吐/成功率/Worker/blocked/deferred/API响应基线，定位七个优化面的代码与测试路由。
- [complete] Phase 2：TDD实现campaign默认汇总与成员分页，建立1093规模隔离PostgreSQL p95≤2秒门禁。
- [complete] Phase 3：TDD实现1–3条领取批量，并锁定Worker独立进程/slot reservation/熔断故障域；blocked组不自动换出口。
- [complete] Phase 4：TDD实现非核心HTTP优先、transport/访问阻断/字段与上下文缺口不启Firefox，核心字段/身份才允许浏览器补全。
- [complete] Phase 5：接入销量×价格≥2000美元分层、竞品继承与按采集面时效；非核心PDP统一7天，静态字段不单独制造请求、评论独立增量。
- [complete] Phase 6：Woot仅作`source=woot_ajax`实验适配器；403/非JSON失败、稳定复合键、门禁未过不进生产。
- [in_progress] Phase 7：全套非PG、隔离PG、性能基准、独立review与固定20-ASIN显式业务门禁；首轮独立review的4个P1已修复并通过777项非PG、29项Operator PG、57项恢复/Worker/指标PG组合证据，待同一reviewer delta复核后才允许live门禁。
- [pending] Phase 8：制作可回滚release；只恢复同一campaign，不创建新全量campaign。

## 2026-09-15 综合采集平台 Goal 重置

用户将《PDP与独立评论融合上线及操作手册》并入目标。独立source map证明PDP恢复仓、公司Git、独立Woot和云端v5工件不在同一可发布分支，故原单体目标拆为四个顺序 Goal，权威文本见[`docs/integrated_collection_program_goal_20260915.md`](docs/integrated_collection_program_goal_20260915.md)。

- [in_progress] Goal 1：只做源码归一与可重建基线；当前建立source manifest、tracked核心文件审计和clean rebuild边界，不开发新API、不访问生产、不启动Worker。
- [pending] Goal 2：评论只读API、历史数据隔离导入演练与统一ASIN只读投影。
- [pending] Goal 3：Review Campaign、graceful stop、统一analysis_run编排与权限矩阵。
- [pending] Goal 4：分阶段生产发布、原1093 campaign固定20 pending成员cohort门禁、达标后恢复剩余成员。
- [complete] 用户已停止旧PDP效率Goal；新Goal 1已登记为active。
- [in_progress] 已从`company/main@08468db`创建`codex/integrated-source-20260915`独立worktree；移植前基线535 passed/35 skipped/4 failed，先保留为基线证据并继续有界Woot切片。
