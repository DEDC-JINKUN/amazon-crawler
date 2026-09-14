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
