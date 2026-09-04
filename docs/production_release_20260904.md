# 2026-09-04 商品采集发布操作包

目标时间：13:30候选冻结、14:00前交付；17:00 Go/No-Go，18:00目标上线。
这是时间目标，不是已上线声明。1093全量和独立评论不在当天上线完成定义内。

## 冻结与已知现场

- 代码候选目录：`C:\Users\PC\.codex\worktrees\13c0\woring\爬虫\Amazon Scraping`。
- 正式目录：`D:\woring\爬虫\Amazon Scraping`。只读核对其main为`c3af4b2b097bcf5ce3d7d4a6b356232f9459906e`。
- 正式目录`docs/main_report.md`有用户未提交改动，且候选也改了该文件；不得覆盖、reset或全库clean。
- 最终SHA/tree/测试结果以交付回执为准；此前951a2b5是已提交的基础恢复候选，不包含本次存活consumer补丁。
- 生产DB、data/raw及凭据未迁移/回填；测试使用独立`amazon_recovery_test_20260904_13c0`。
- 候选与正式venv均已只读核对：Python 3.13.14、Selenium 4.47.0、psycopg 3.3.4；没有为本补丁升级依赖。

## 1. 主控执行的安全集成（先停服务，保留用户文档）

以下动作由主控在部署窗口执行，本实现任务不执行。代码分支可追溯到正式c3基线，
但用户文档冲突必须单独合并；任何基线漂移或其他脏文件出现时停止。

```powershell
$Candidate = 'C:\Users\PC\.codex\worktrees\13c0\woring\爬虫\Amazon Scraping'
$Formal = 'D:\woring\爬虫\Amazon Scraping'
$ReleaseInfo = Get-Content -Raw 'C:\Users\PC\.codex\worktrees\13c0\woring\爬虫\outputs\amazon-production-release-20260904\release_receipt.json' | ConvertFrom-Json
$CandidateSha = $ReleaseInfo.head
function Formal-Git {
    & git -c "safe.directory=$Formal" -c "safe.directory=$Candidate" -C $Formal @args
    if ($LASTEXITCODE -ne 0) { throw 'Release Git step failed; stop and preserve worktree.' }
}
Formal-Git status --short
if ((Formal-Git rev-parse HEAD).Trim() -ne $ReleaseInfo.formal_base) { throw 'Formal baseline drift' }
Set-Location $Candidate
.\run_owned_full_secure.ps1 agent-stop
.\run_owned_full_secure.ps1 stop -Port 8771
```

只处理已核实的受管进程；未知listener不得强杀。先在发布输出目录保全用户文档patch，
再对**仅该文件**使用可恢复stash；以下`stash pop`发生冲突时必须人工合并，不用ours/theirs批量覆盖。

```powershell
$Release = 'D:\woring\爬虫\outputs\amazon-production-release-20260904'
New-Item -ItemType Directory -Force -Path $Release | Out-Null
if (Test-Path "$Release\main_report.user.original.md") { throw 'Backup already exists; inspect it before retrying.' }
Copy-Item -LiteralPath "$Formal\docs\main_report.md" -Destination "$Release\main_report.user.original.md"
Formal-Git diff --binary --output "$Release\main_report.user.patch" -- docs/main_report.md
Formal-Git stash push -m 'preserve-user-main-report-20260904' -- docs/main_report.md
Formal-Git fetch $Candidate codex/amazon-canary-capacity-gate
if ((Formal-Git rev-parse FETCH_HEAD).Trim() -ne $CandidateSha) { throw 'Candidate drift' }
Formal-Git merge --ff-only FETCH_HEAD
Formal-Git stash pop
```

safe.directory仅为本次命令信任两个明确路径，不修改全局Git配置。主控检查/合并文档后可提交单独文档commit。部署代码相对交付SHA必须无差异（允许审核后的
`docs/main_report.md`文档差异），再次记录部署SHA/tree和工作树。保留stash/patch到上线验收结束。
不push/tag；任何代码冲突不是“强制同步”的理由。

## 2. 迁移预检、dry-run与回滚

迁移顺序：`schema/migrations/20260904_recovery.sql`，然后`20260904_bounded_consumer.sql`。
二者均为增量表/列，不重写历史evidence。迁移已在独立测试DB应用并回归；生产只读预检如下：

```powershell
Set-Location $Formal
& .\scripts\secure_dpapi_launcher.ps1 -FilePath (Resolve-Path '.\.venv\Scripts\python.exe').Path -SecretNames @('AMAZON_US_POSTGRES_DSN') -ArgumentList @('scripts/release_readiness.py','--tenant-id','owned_us_asin_20260902_full_01','--limit','20')
```

预期缺新表时输出`schema_migration_required`，不是网络故障。主控确认生产迁移授权后，
通过同一DPAPI入口在单事务执行上述两份固定SQL；禁止拼明文DSN、修改角色/全局PG配置、运行全库清理。
迁移dry-run应在独立测试库进行并rollback，不能把生产DDL锁称作“只读检查”。

```powershell
# dry-run强制定向已标记的独立测试数据库并rollback，不会在生产执行DDL。
& .\scripts\secure_dpapi_launcher.ps1 -FilePath (Resolve-Path '.\.venv\Scripts\python.exe').Path -SecretNames @('AMAZON_US_POSTGRES_DSN') -ArgumentList @('scripts/apply_recovery_migrations.py','--mode','dry-run')
# 仅在主控核实代码差异、文档合并和生产审批后执行：
$DeploySha = (git rev-parse HEAD).Trim()
& .\scripts\secure_dpapi_launcher.ps1 -FilePath (Resolve-Path '.\.venv\Scripts\python.exe').Path -SecretNames @('AMAZON_US_POSTGRES_DSN') -ArgumentList @('scripts/apply_recovery_migrations.py','--mode','apply','--confirm-production','--expected-head',$DeploySha)
```

迁移输出固定SQL文件SHA256、实际HEAD和是否commit，无DSN/口令；锁等待5秒、语句30秒，失败不继续部署。

回滚：先停采集服务；保留恢复表/历史/预算，不DROP，不回填旧失败。可回滚只读控制面；
旧Worker忽略新预算，不能直接重启旧采集版本。若需生产写者回滚，重新审批预算/兼容方案。

## 3. 冻结20样本与真实验收命令

迁移后只读选择新的自有未采样本，输出文件用exclusive-create，已有文件拒绝覆盖。
主控核对ASIN列表、manifest SHA256、代码SHA、TOML哈希、余额、时间/字节/重试预算后批准测试；
一旦开始不替换失败ASIN。配置`max_actions_per_run`必须至少为20（100阶段至少100），不足会拒绝。

```powershell
Set-Location $Formal
& .\scripts\secure_dpapi_launcher.ps1 -FilePath (Resolve-Path '.\.venv\Scripts\python.exe').Path -SecretNames @('AMAZON_US_POSTGRES_DSN') -ArgumentList @('scripts/release_readiness.py','--tenant-id','owned_us_asin_20260902_full_01','--limit','20','--manifest-out','data/release_20260904/cohort20.csv')
.\.venv\Scripts\python.exe scripts\validate_us_manifest.py --manifest data\release_20260904\cohort20.csv --expected-count 20
.\run_owned_full_secure.ps1 canary -Limit 5 -Port 8771
.\run_owned_full_secure.ps1 run -Limit 20 -ManifestPath data\release_20260904\cohort20.csv -RecoveryMaxSeconds 5400 -Port 8771
```

canary的5是**瞬时批次容量**，不是20/100的总任务量；每批最多5 ASIN并预留其有界替换槽。
100同样分批，不能`canary -Limit 100`后要求200个槽。canary只是代理连通性，不能等同Amazon成功。

Controller现在保持本次冻结cohort的consumer存活到全部终态或5400秒deadline；冷却期间释放容量预约，
只轮询PG不访问Amazon。跨run任务预算保持，重新打开同run不会延长cohort deadline。
如果1小时冷却后canary已过期，consumer等待新fact而不发请求；主控可在仍存活的consumer期间
**另执行一次同上canary命令**刷新授权，不重新入队ASIN。凭据/配置不匹配等不可恢复Gate原因立即拒绝。

验收看run API/receipt：`requested_actions=recorded_actions=20`按唯一ASIN；`attempt_actions`另计，
每个item的`attempt_evidence`保留全部尝试raw/hash/错误，`capacity_authorizations`绑定各次预约/canary。
variant单列，partial位置字段不当full，HTTP/Firefox未知字节保持unknown；预算耗尽/访问控制不冒充商品问题。

## 4. 服务、Agent与100阶段

```powershell
.\run_owned_full_secure.ps1 console -Port 8771
.\run_owned_full_secure.ps1 status -Port 8771
.\run_owned_full_secure.ps1 agent-service -AgentPort 8765
.\run_owned_full_secure.ps1 agent-health -AgentPort 8765
.\run_owned_full_secure.ps1 agent-get -Asins '<固定ASIN>' -AgentPort 8765
.\run_owned_full_secure.ps1 agent-refresh -Asins '<固定ASIN>' -Reason 'release-fixed-cohort' -Wait -TimeoutSeconds 5400 -AgentPort 8765
.\run_owned_full_secure.ps1 agent-job -JobId '<返回job_id>' -AgentPort 8765
.\run_owned_full_secure.ps1 agent-stop -AgentPort 8765
```

Agent仅消费显式refresh，不领取普通pending；冷却到期自动恢复，服务启动后才过期的旧lease也持续回收。
重启验证使用`agent-stop`→`agent-service`，核验job预算/next_retry_at没有清零。不同时对Controller的
固定cohort发送额外refresh，否则会产生额外操作者请求并使归因复杂；容量/lease仍共享，不会超卖。

20通过后，主控另冻结`cohort100.csv`并记录新预算，再执行同样`canary -Limit 5`及
`run -Limit 100 -ManifestPath data\release_20260904\cohort100.csv -RecoveryMaxSeconds 5400`。
20未通过不得扩量；不触发1093全量或评论。

## 5. 17:00 Go/No-Go与18:00目标

Go要求：代码/配置冻结、生产迁移与重启通过、20及获准100的原始证据/预算/恢复闭环、
无P0/P1、无凭据/IP泄漏、无Gate/lease逃逸、requested/recorded可解释且按固定分母报告。
不满足则17:00明确No-Go及唯一阻塞，不到18:00再模糊宣布“基本上线”。

计费事实更正：用户2026-09-04提供的DataImpulse dashboard窗口8/28–9/4为295.35MB、1773请求、$0.29，
不是本次20成本，也不是剩余额度；旧103MB推算作废。部署窗口前后独立记录计费读数并注明延迟/其他消费者。
媒体域名11.79MB vs主站1.77MB只是核验线索，不临时封禁域名或改UA/TLS指纹来美化样本。

## 最终离线发布证据

- 验证代码tree：`b559b2ff4072441c280de45da2bc744b465897cd`；其后仅补发布记录/操作说明。
- 权威tests全量：528 passed、28 skipped；全部28项PG测试在明确独立库补跑28 passed。
- 普通存活consumer与Agent存活refresh均证明冷却后自动恢复，无人工重新入队；stale Gate先零fetch等待，deadline终止。
- 100条队列只产生20个最多5条的容量批次，不要求一次200槽。
- Node行为4 passed，Node语法、Python compileall、四个PowerShell入口AST、diff-check与凭据扫描通过。
- 独立delta审查APPROVE；5400秒已在Python/Controller/安全入口统一硬限制，已确认P0/P1为0。
- 迁移dry-run只在标记的独立库执行并rollback；生产迁移和真实Amazon仍未执行。
