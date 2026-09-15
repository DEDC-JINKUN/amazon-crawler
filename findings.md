# 隔离重构证据

冻结基线 71db26e；生产不改、不发布、不使用真实 ASIN 调试。

初始基础检查 15 失败：3 个终态覆盖、8 个清理前过早终态（后精简为 4 种退出条件）、4 个历史重复/重叠结果污染。这里不是八个端到端场景，不能计入发布轮次。

统一校验 operator_lifecycle 负责终态权限、递归授权覆盖和本批 run 身份；operator_export 基于本批 run 的 collection_evidence 计算进度，历史数据分开。

候选 PostgresBatchStore 与增量 migration 建立批次、成员、run 关系；发布幂等，领取有租约，两个 Worker 通过 SKIP LOCKED 领取不同成员；证据归属必须匹配 batch、run、subject、ASIN；重叠活动批次不能同时领取同一商品。

尚需：将新仓储接入启动/Worker/页面全链路；统一容量协调与进程清理；真实 OS 故障恢复矩阵；三轮同指纹验收。不能部署当前开发状态。

隔离 PostgreSQL 独立目录 .test-tmp/operator-matrix-postgres，端口 55444，数据库 operator_matrix。生产端口55443未访问。

## 自测新增发现

- 清理延迟后重启会丢失原始退出码，成功变失败、错误文案残留。已用持久化 `_pending_finalization` 保留当时的结果；恢复严格复用该记录。
- 已提交终态在重复收尾时被改写。终态提交现在幂等，重复调用不再写回。
- 历史字段 `history/evidence_scope` 被写入固定列 CSV，触发 ValueError；导出现在只写表头允许的字段，JSON 保留历史结构。
- 调用点检索确认 `PostgresBatchStore` 仍只有模块定义，未被实际启动器/Worker 调用；因此数据库模型测试通过不能证明真实任务链路已改造。
- 主大屏仍按 tenant/run 读取，未接入数据库 batch 提交事件；完整页面验收仍不能通过。

## 2026-09-13 运行态核对

- MCP `collection_capabilities` 最终恢复并报告 campaign 上限 5,000，具备 submit/status/wait/cancel；这证明 Agent 控制协议存在，不证明全量生产就绪。
- 当前 `127.0.0.1:8765` 与 `8770` 都在监听；复测 `/healthz`、`/readyz` 返回 200。Agent readiness 展示的是单 `refresh_worker`，当前运行态不是双 Worker。
- 已部署历史 2W 批次 `batch-dd1b4efb6ad14c70acfd6e94`：Worker 1 先预约 10 个槽，Worker 2 随后因 `capacity_reserved_elsewhere` 失败。根因位于父控制器按子 Worker 分别预约容量，启动前没有做整个批次的原子容量规划或安全降级。
- 已部署历史 2W 批次 `batch-6e0a9221d21d4d86ad67dd96`：Operator 投影显示 2 completed + 1 variant，但本轮两个 child run 都是 `recorded_actions=0`、`unrequested_actions=3`，父回执 exit 4。根因是页面/批次投影可以读取历史商品结果，而 Controller 完成门按本轮 run evidence 判定，两套事实源发生冲突。
- `D:/woring/爬虫/Amazon Scraping` 主仓库只包含单 Worker Controller；数据库租约与 `SKIP LOCKED` 支持并发，但启动层没有多 Worker 编排。
- `D:/woring/爬虫/.worktrees/amazon-integrated-deploy` 包含针对批次证据归属的隔离重构和 722 passed 的现有回归记录，但 Worker/启动器/页面尚未接入新模型，发布矩阵仍为 0/3；其 `.git` 管理目录已丢失，不能直接作为可审计提交源。

当前裁决：可以操控 campaign，但不应启动全量；多 Worker 问题需要在一个权威源码基线上完成统一批次、统一容量和统一终态后再发布。

- 用户已确认公共 seam。采用外置 Git 目录 `D:/woring/爬虫/.codex/recovered-git/amazon-integrated-deploy-20260913` 将当前隔离快照登记为基线，未移动或覆盖损坏的原 `.git` 文件。
- 第一条纵向修复完成：`agent_service_control.ps1` 和 `run_owned_full_secure.ps1` 可显式传入有界 Worker 数；该值进入 runtime fingerprint 与 lock，避免请求 Worker 数变化后误复用旧单 Worker 服务。
- 共享模块决策：以既有 `collection_campaign`/`collection_campaign_member`/`refresh_request`/本轮 evidence 作为唯一批次事实源。Operator 不再自行启动 ordinary two-worker Controller；它通过 loopback Collection campaign 接口提交、查询和取消，实际并发由 Agent service 的容量感知 Worker pool 管理。
- 该 seam 有两个 Adapter：生产使用 `AmazonCollectionClient` 的 loopback HTTP Adapter，测试使用内存 Fake Adapter。Operator 本地 JSON/CSV 仅是 campaign 快照的可下载投影，不再决定终态。
- Operator runner 在提交前必须校验 readiness 的 tenant 与 `effective_workers/alive_workers`；请求 2W 但服务因容量降为 1W 时拒绝提交，不再让 Worker 1 占槽后才使 Worker 2 失败。
- Agent 控制脚本使用 lock 中已记录的 WorkerCount 验证既有运行指纹；普通读/状态命令不会因调用方默认值为 1 而把健康的 2W 服务重启成 1W。只有显式 start 才要求当前 WorkerCount 与请求一致。
- Operator runner 连续三次无法读取 campaign 时尝试取消同一 campaign，持久化固定脱敏错误并失败退出；外部取消/过期不再被标成成功。
- campaign 成员 `job_status=failed` 但 `item_status=blocked` 或 recovery outcome 为 access control 时，Operator 继续显示 blocked，不合并进普通 failed。
- `/readyz` 现在公开当前 repository 的 `subject_type`；Operator 与 tenant 一起核对，避免 own/competitor/candidate 误路由。
- Windows 上认证拒绝前关闭仍有未读请求体的 socket 会偶发 RST；服务端有界 drain 与客户端对 10053/10054 的一次有限重试分别覆盖拒绝响应和幂等 campaign 调用。

## 2026-09-14 第四轮三个P1根因

- Worker降级根因：`effective_worker_count`允许按容量降级，但`worker_pool_status`只把降级记录为`capacity_backpressure=true`，仍保留`state=running`；因此`/readyz`和Collection提交门继续返回可用。生产合同要求requested/effective不一致即fail-closed。
- 停止竞态根因：`_project_campaign`在确认campaign终态后先判断本地`stopping`，后判断后端`campaign_status=completed`；本地停止意图错误覆盖了权威后端完成事实。
- 完成投影根因：`BatchManager._finish_record`只在`campaign_status=completed AND members_terminal`时完成，否则落入通用失败分支。后端campaign是批次权威，成员计数是本地投影；权威完成状态不应被暂缺投影改写为失败。

修复假设：保持现有campaign权威架构，只调整三处优先级/门禁，不新增事实源；通过Agent readiness、Operator runner、BatchManager三个已确认公共seam各加一条红测验证。

## 2026-09-14 第四轮独立复审新增P1

- 部分Worker计数字段会fail-open：`refresh_worker_ready`在缺少`requested_workers`时直接兼容放行，即使已经报告`effective_workers=3/alive_workers=1`。兼容只应允许“全部计数字段都未提供”的旧单Worker状态；一旦提供任一计数字段，三者必须齐全且满足requested/effective/alive。
- POST拒绝drain只有1 MiB字节上限，没有墙钟时间上限；客户端可声明合法长度后缓慢发送，占住ThreadingHTTPServer线程。需要固定总deadline而不是每次读取重新续期。
- 最终delta复核发现完整计数字段仍只校验`effective/alive >= requested`，与文档的三项严格相等合同不一致；任何计数矛盾都必须fail closed。

## 2026-09-14 生产代理阻断根因

- 本机DNS解析正常、`gw.dataimpulse.com:10000` TCP可达、直连非Amazon健康目标返回200。
- 当前部署vault与旧源码vault各做一次脱敏原始CONNECT检查，网关均明确返回HTTP 407；故障位于代理凭据被供应商拒绝，不是Amazon、DNS、本机网络或TLS证书。
- 不再重复canary。恢复需要用户在本机隐藏输入新的DataImpulse账号/密码；更新只替换部署vault的代理字段，保留生产DSN/API Key并先生成加密回滚副本。
- 用户提供的新值已通过DPAPI原子轮换并保留旧加密vault，但脱敏CONNECT仍返回`407 NO_USER`。DataImpulse官方说明：代理login/password是在购买套餐后由Dashboard的`Proxy Access`生成；`NO_USER`表示该代理用户的套餐不存在。普通站点登录名不能替代代理login。

## 2026-09-14 候选三Worker真实启动失败

- `e345468`首次激活未通过45秒readiness并自动回滚到`592a65a`；目标tenant无任务被提交。
- 在备用8875/8870端口复现：三个子Worker均在`campaign_worker_process._write_status -> os.replace`遇到WinError 5。父监督器并发读取`worker-N.json`时Windows短暂锁住目标，子进程将单次锁竞争当成致命错误，重启预算耗尽。
- 遗留的唯一命名临时文件与堆栈共同证明故障在状态文件原子替换边界，而非PostgreSQL、代理容量、Worker业务线程或Console端口。
- 修复只对`PermissionError`做20×10ms有界重试并在退出时清理本次临时文件；不扩大重启预算、不吞持续权限错误。
- 独立delta review发现监督器`_spawn_locked`仍在重启前预删`worker-N.json`；目标被Windows读锁占用时该unlink同样会失败。删除预删是最小修复：新子进程本来就用临时文件原子覆盖，监督器又以PID匹配防止旧状态冒充健康。
- 后续复核指出PID可能被操作系统复用；仅保留旧状态并核对PID仍可能短暂fail-open。最终采用每次spawn生成32位随机launch token，子进程写入状态，监督器同时匹配token与PID；新子进程首次状态前保持starting。
- 再次复核发现`status()`虽计算launch匹配，却在匹配判断外传播旧文件的failed/blocked到内部状态。最终把running/failed/blocked的读取与传播都置于当前launch匹配之后；旧running/failed/blocked三种PID复用场景均保持starting。
- 冻结release备用端口真实启动显示Windows venv的`Popen.pid`属于启动器父进程，状态文件PID属于真正执行Worker的子进程；强制PID相等会使健康Worker永久starting。launch token已足以绑定本次spawn，Popen本身负责生命周期；放宽PID等值后备用端口严格readiness为requested/effective/alive/healthy=3/3/3/3、restart=0。

## 2026-09-14 PowerShell 7长期运行时

- 官方稳定WiX/MSI包为`Microsoft.PowerShell 7.6.6.0`；固定机器路径应为`C:/Program Files/PowerShell/7/pwsh.exe`。
- 已安装微软签名的系统级PowerShell 7.6.6，固定路径为`C:/Program Files/PowerShell/7/pwsh.exe`。
- 两个生产任务均已备份XML并迁移Action到系统PowerShell7与`-NonInteractive`，原参数、触发器、Principal与Settings保留；旧release和`4021a56`都曾通过该任务动作启动。

## 2026-09-14 生产门禁推翻固定状态文件方案

- `4021a56`生产激活本身通过3/3/3/3 readiness，但3-ASIN业务门禁开始后，三个Worker仍分别在`_write_status -> os.replace(temp, worker-N.json)`遇到WinError 5；20次×10ms重试只延后崩溃，不能消除持续读锁。
- Windows最小复现以允许读写但不允许delete sharing的真实`CreateFileW`句柄持有`worker-1.json`，旧实现稳定在约0.22秒后抛WinError 5。故根因是覆盖固定目标所依赖的Windows共享语义，不是Amazon页面、代理、PostgreSQL或Worker业务状态。
- Supervisor监控线程和`/readyz`都曾读取同一个固定文件；外部诊断、杀毒或索引程序也可延长锁。继续增大重试预算仍无法给出上界，且会把状态通道故障升级成采集Worker退出，因此原架构作废。
- 长期协议使用`worker-N.<launch-token>.<20位sequence>.json`不可变快照。Writer只创建新文件；Supervisor监控线程选择当前token的最新完整快照并缓存，API不再直接读文件。最新文件写入中/损坏时沿用上一完整缓存；旧快照删除失败为非致命best-effort，进程存活仍以受管Popen为权威。
- 独立review发现两项P1：Python相等语义会让JSON boolean/float sequence冒充integer；锁定旧launch快照缺跨launch总上限。修复后读写两侧都严格要求原生`int`，并对每Worker设置64文件/1 MiB总配额、4 KiB单快照上限；启动前必须为3份滚动快照预留容量，否则fail closed。清理失败与积压数进入readiness子项。
- delta复核继续发现两个P1：配额目录枚举/文件大小统计失败时把未知当0，以及读取端接受sequence=0。最终把inventory完整性纳入配额判定，glob/stat失败均拒绝spawn/publish；读写两端统一要求正整数。另补启动事务性清理，后续Worker预检失败时终止先前已启动成员。
- `4021a56`当时停止在3条且未提交20条或1093条，随后恢复旧pointer。该历史门禁已由下节的新release流程取代，但`4021a56`仍永久禁止复用。

## 2026-09-14 release 33fd636与真实业务门禁

- release`33fd636`（reviewed code`8c1d9d7`，archive SHA256`98867ce15c695b979dd05b557eee6b462bd2a76fab042cde15fe13e84aa1efa8`）已激活；两项计划任务均由系统PowerShell7托管Running。激活连续30次ready样本和后续业务运行都保持三Worker alive/healthy=3/3、restart=0；每Worker 3份快照、约1 KiB、cleanup failures=0。长期状态协议的生产故障已关闭。
- 最新34槽非Amazon canary为31 available/unique、3 connect_failed，容量Gate allowed。新3-ASIN campaign`campaign-bd26b05e4b8055a691adc80d42c22561`最终1 succeeded、2 failed并取消；目标tenant随后active campaign/job/lease/reservation均为0。
- 两个失败ASIN分别使用session-13/14与session-15/17；这些会话在ipify canary都返回200，但访问Amazon时连接/响应头阶段约9–12秒后无HTTP状态、无传输字节，均记录`transport_error`并隔离。session-23成功返回Amazon 200与426001 bytes。根因面转为“非Amazon可用的住宅出口不保证Amazon稳定”，不是状态文件、Worker重启、PostgreSQL或API。
- 截至2026-09-14 22:00，门禁规则已阻止20条与1093条提交，`33fd636`当时保持上线空闲；该状态随后被用户22:06的全量启动指令取代。
- 上述“未提交”是22:00前的历史状态。用户于2026-09-14 22:06再次明确要求开始全量；即时预检确认release`33fd636`、三Worker3/3、restart=0、1093条清单唯一合法、目标active campaign/job/lease/reservation均为0后，使用稳定幂等键一次提交全量campaign`campaign-b14bef6a4b39509a9c92e1ff2629386b`。初始状态1093 total、1092 pending、1 running、0 terminal；未创建独立20条campaign。

## 2026-09-15 全量运行阻断

- 10:03紧凑只读快照：campaign active，20/1093终态（13 succeeded、2 variant、5 failed），1063 pending、10 deferred、0 running；3个Worker均blocked，readyz 503。
- Worker 1/3终态为`access_blocked`，Worker 2为`recovery_budget_or_lease_denied`；子状态心跳正常写到退出，说明不可变状态协议未复发，故障在业务状态传播与浏览器清理。
- 首次页面/API整体超时同时存在8棵共88个headless marionette Firefox、父geckodriver均已退出；清理经进程归属验证的孤儿树后，Console health 3ms、首页75ms恢复。第二次运行又出现6棵/33个孤儿且全部geckodriver已退出，证明Firefox泄漏可重复。
- fresh canary为34/34。普通服务restart期间旧Worker在canary完成后抢到10槽reservation；强杀未释放它，导致新服务只能2/3并以`capacity_reserved_elsewhere`拒绝第三Worker。受控停止Agent并调用仓储requeue/release后，3个claimed任务回队、3个active reservation清零，3 Worker可重新初始ready。
- 因新3 Worker随后再次在访问阻断中全部blocked，重复重启不是修复；必须同时修复浏览器树生命周期、per-item阻断与Worker级熔断边界、停止时容量/租约释放。
- 源码核对纠正了“单个ASIN直接杀Worker”的初步表述：单个blocked本来会继续，只有连续2个或20窗口3个blocked让`ProxySessionPool.circuit_open_reason`成立后才返回全局`-1`，这是现有测试与合规文档共同锁定的fail-closed边界，不能改成自动换出口继续。`recovery_budget_or_lease_denied`不是普通重试结果，而是恢复授权/租约无法证明时的全局拒绝，也保持blocked。
- 确定的代码缺陷有两条：Selenium service原为局部变量，`driver.quit()`异常后无owned进程树兜底；子Worker daemon线程最多等10秒就被主进程放弃，且Supervisor/服务控制器直接terminate/force stop时只依赖Python finally释放reservation。修复因此只加强生命周期和精确owner回收，不弱化访问控制。
- 独立delta复核补出两个有效所有权缺口：仅保存Firefox PID会在关闭窗口遇到PID复用误杀同名个人进程；用首次心跳中的真实PID构造Worker owner会漏掉首个心跳前崩溃。最终边界改为Firefox `PID+可执行名+创建时间`必须三项一致、身份未知即不强杀；Worker owner由Supervisor在spawn前以launch token生成并传给child，父子清理都不依赖心跳。复核另报Operator取消失败误标stopped，但该文件不在本delta且本次保留campaign、不调用取消，单独留作后续任务。

## 2026-09-15 效率优化基线

- 11:47 campaign状态：61/1093 terminal（43 succeeded、9 variant、9 failed）、1 running、27 deferred、1004 pending；完成率5.58%，商品成功率按terminal为70.49%，可信记录终态（succeeded+variant）为85.25%。
- 总墙钟从2026-09-14 22:06到11:47约13.68小时，仅4.46 terminal/小时；该口径包含多次blocked、修复和发布停机。
- 修复上线后的短窗口从11:32的47 terminal到11:47的61 terminal约14条/15.5分钟，约54 terminal/小时；短时速度仍低于≥100/小时目标且无法抵消Worker陆续熔断风险。
- Worker 1/2状态仍running、Worker 3为`access_blocked`；campaign快照只有1条running，说明名义2个活Worker没有形成稳定2路有效并发。
- Woot开源项目存在，但2026-06公开issue显示端点403且脚本把失败伪装成0评论；2026-07另有缺review_id问题，主分支最后提交为2026-03-04。只能作为fail-closed实验源，不能直接接生产，也不能改善当前商品页吞吐。
- Woot源码核对：`GET https://www.woot.com/review/Reviews/{ASIN}`使用`filter/isVerified/sort/page|pagingNext`，JSON键为`Reviews`和`PagingNext`；原脚本用`Author+Title+Text前80字`去重且用伪装Chrome UA。我们的实验实现必须保留`Agent/<name>`透明UA，检查HTTP状态与Content-Type，使用包含完整正文/日期/评分的SHA256稳定复合键，并将403/HTML/结构错误设为非零失败，不能沿用“捕获异常后0评论成功”。
- Campaign状态慢的直接原因是`PostgresCollectionRepository.load_campaign()`每次读取都对全部成员JOIN后`fetchall`，API/MCP再返回1093项；一次MCP状态响应超过12万tokens。默认汇总现改为PostgreSQL`COUNT(*) FILTER`聚合，成员分页上限100；Operator客户端显式`members=all`保持兼容。
- 新增独立55444真实数据库门禁：构造1093成员，连续20次summary并读取offset=1000/limit=100的末页；p95断言≤2秒、末页93条，测试通过且实例fast shutdown。
- 多Worker故障域已由现有架构满足：每个Worker是独立进程并各建adapter，capacity reservation分配不重叠slot；新增测试确认一个child返回全局blocked code3后，其余健康child不被terminate或restart。优化改动只把每Worker容量批次5→3，三Worker名义slot需求从最多30降到18。
- 生产`ProxySessionPool`的普通transport失败本来就只隔离/换槽后再试一次HTTP，不会启动Firefox；真正缺口是非核心的access验证、缺字段、上下文和空评论仍可浏览器回退。claim现在从`asin_master`携带`priority/refresh_policy`，普通/候选任务全部禁止浏览器，只有`own_core/competitor_core`保留一次有界补全；未知policy仅为旧fixture兼容。
- 外部指标实现使用独立`external_metric_batch/external_metrics/external_metric_relationship`；同batch绑定source+SHA256+row_count，完全相同重导0新增、内容变化冲突。隔离PG验证`100×$25`为own_core、`10×$50`为own_normal、映射竞品为competitor_core，product_snapshot仍2行未被改写。
- 独立字段TTL按“采集面”实现以避免反向增加请求：核心PDP 4/6小时、非核心PDP 168小时；静态字段随已发生的PDP顺带更新而不另发请求；评论是独立来源/增量面。逐字段各发一次PDP会降低效率，因此没有采用。
- Woot适配器当前仅离线可调用且未接Worker：已验证透明UA、稳定来源键、403/HTML/坏结构全失败；`review_record`增加`source_type/source_record_id`以避免把Woot证据冒充Amazon HTML。真实端点门禁仍待Phase 7。
- Phase 7首轮独立review给出4个P1并STOP：Worker readiness与真实3条预约不一致；同批次并发导入存在空行锁竞态；竞品多owner继承不确定；Operator仍以`members=all`高频轮询。
- 容量修复恢复为按`MAX_AGENT_REFRESH_BATCH × workers`核验事实与替换槽。`per_asin + retry=1`下三Worker至少需要18个唯一槽；3/6/9槽均不得误报3W可用，18槽允许，生产34槽设计仍满足。
- 外部指标在读取批次前按`tenant+batch_id`取得事务级advisory lock；并发相同文件一写一重放，并发不同文件同批次一写一`ExternalMetricBatchConflict`。竞品继承先按竞品聚合`bool_or(owner core)`，任一owner核心即确定性核心。
- Operator客户端默认summary；实际HTTP Runner连续active轮询只请求无成员汇总，终态才以100条分页读取成员。分页数量不完整或持续失败会写脱敏`campaign_members_fetch_failed`回执，避免无回执崩溃。
- 复审修复后证据：74项Agent/Client/Operator定向通过；外部指标并发3项真实PG通过；Console 405真实loopback连续20/20；完整非PG 777 passed/59 skipped；全新UTF-8本地集群Operator 29 passed；恢复/Worker/Campaign/External Metrics等组合57 passed。所有55444实例均fast shutdown。
