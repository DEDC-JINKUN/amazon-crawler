# Collection API 与 Agent 刷新服务

Collection API 是 Agent 查询采集结果和提交小批按需刷新的统一入口。生产模式只使用 PostgreSQL，并由同一服务内的 `refresh-only` Worker 消费刷新队列；SQLite 入口只保留给离线兼容测试。服务不暴露到公网。

## 启动

普通 Agent 直接执行 `agent-get` / `agent-batch` / `agent-refresh -Wait` / `agent-job`；这些命令会先幂等确保固定 tenant 的本机服务已启动。管理员才需要显式使用以下生命周期命令：

```powershell
.\run_owned_full_secure.ps1 agent-service -AgentWorkerCount 2
.\run_owned_full_secure.ps1 agent-status
.\run_owned_full_secure.ps1 agent-health
.\run_owned_full_secure.ps1 agent-stop
```

锁中的运行时指纹覆盖服务、Worker、代理池、API、存储模块和当前配置文件。只有 PID、StartTime、tenant、URL 均已验证且 `/healthz` 正常的旧版本进程会被受控替换；未知监听器继续 fail closed。

进程身份与业务 readiness 分开：自动确保服务使用 `/healthz`，所以 refresh Worker blocked 后只读查询和 job 回查仍可用；`agent-health` 使用 `/readyz`，新 refresh 也由服务端 readiness Gate 拒绝。readiness 返回 `requested_workers`、`effective_workers`、`alive_workers`、`healthy_workers` 和 `capacity_backpressure`。服务持续重读 canary fact，并核对新鲜度、配置哈希和容量 Gate；只有状态严格为 `running` 才接受 refresh/campaign。多进程 Worker 还要求子进程心跳和 Worker线程业务心跳同时新鲜；异常退出或业务线程停滞会有限重启，blocked 不会通过重启绕过。Windows子Worker每次把心跳写为`worker-N.<launch-token>.<sequence>.json`不可变快照，不再替换可能被读取的固定目标；sequence只接受正的JSON integer，拒绝boolean、float和0。监督器监控线程只接受当前launch的最新完整快照并缓存到内存，`/readyz`只读内存状态。若最新快照在写入中或损坏则保留上一份完整缓存；同launch通常保留3份，跨launch反复清理，总积压硬限制为每Worker 64个文件或1 MiB。删除被锁旧文件只增加`status_snapshot_cleanup_failures`并通过`status_snapshot_backlog_files/status_snapshot_backlog_bytes`公开当前积压，不杀死健康Worker；目录无法枚举、剩余文件大小无法统计或不能预留完整的3份、每份最大4 KiB滚动窗口时，都在Worker领取任务前fail closed。若多Worker启动在后续成员失败，Supervisor会终止已启动成员，不留下半个进程池。旧`worker-N.json`只作迁移兼容。状态PID仅供诊断，因为Windows venv启动器父PID与真正写心跳的Python子进程PID可以不同；进程存活仍由受管`Popen`生命周期判断。持续心跳失联仍由监督器按原有超时和有限重启处理。refresh Worker健康值分别累计`processed_actions`、`succeeded_actions`、`failed_actions`和`blocked_actions`；兼容字段`completed_actions`只表示成功，不能据此把失败处理误认成商品成功。

服务停止与版本替换先写入受控 shutdown marker；三个子 Worker 收到各自 stop marker 后，先安装shutdown fence，使refresh claim返回空、任何新capacity reservation以`worker_stopping`拒绝，再等待工作线程并按 Supervisor 在spawn前生成的精确 `agent-refresh-<launch-token>` owner 幂等回队 claimed refresh、释放 item lease 和 active capacity reservation。该 owner 不依赖Windows venv启动器PID或首次心跳，因此首次状态发布前崩溃也能清理；单Worker服务使用同一退出清理函数，stop超时返回失败。只有在有界等待后仍未退出才强制结束，Supervisor 随后再执行一次同 owner 清理。Firefox adapter 记录 geckodriver PID，以及关闭前每个 Firefox 后代的 PID、可执行名和进程创建时间；`driver.quit()` 或 service stop 超时、失败、或初始化中断时，三项身份必须仍完全一致才允许终止，PID复用或身份读取失败一律放弃强杀。该修复只处理本机生命周期，不改变“连续2个或20窗口3个最终blocked即全局熔断”的访问控制边界。

`agent-refresh -Wait`采用单job轮转、至少1.1秒的轮询间隔，并在本机API返回429时遵守`Retry-After`，因此五个持续等待job也不会自行超过默认60次/分钟限制。客户端强制UTF-8 JSON输出，Windows GBK控制台不再导致国际字符编码异常。

以下直接启动方式仅用于离线开发：

```powershell
python scripts/collection_api.py --db state/amazon_us.sqlite3 --host 127.0.0.1 --port 8765
```

默认只监听回环地址 `127.0.0.1`。如需部署到其他机器，必须先增加认证、网络隔离和权限控制，不能直接修改 host 绕过限制。

健康检查不要求 Key。生产业务路由只接受 DPAPI 主密钥派生的 `read-agent` 或 `refresh-agent` scoped key；主密钥保留给本机运维兼容入口，不交给 Agent：

```powershell
$env:AMAZON_COLLECTION_API_KEY = "local-dev-key"
python scripts/collection_api.py --api-key-env AMAZON_COLLECTION_API_KEY
```

API Key 不写入仓库、命令行参数或日志。

生产启动应显式使用 `--require-api-key`；如果 `--api-key-env` 指定的环境变量不存在或为空，服务会拒绝启动：

```powershell
$env:AMAZON_COLLECTION_API_KEY = "use-secret-manager-value"
python scripts/collection_api.py --require-api-key
```

本机 QA 实测结果：缺少 Key 时启动退出码为 1；有 Key 时 `/healthz` 返回 200，未携带 Key 的业务路由返回 401，携带正确 Key 返回 200。

PostgreSQL 环境准备好后，安装可选依赖并切换后端：

```powershell
python -m pip install -r requirements-postgres.txt
$env:AMAZON_POSTGRES_DSN = "host=127.0.0.1 port=5432 dbname=amazon_us_qa user=postgres"
python scripts/collection_api.py --backend postgres --dsn "$env:AMAZON_POSTGRES_DSN" --tenant-id qa_latest_20260827
```

DSN 不写入仓库、日志或配置提交；生产环境通过受控环境变量或密钥管理注入。PostgreSQL 后端必须显式确认 `--tenant-id`，避免 Agent 读到其他回放批次或业务租户的数据。

本机 PostgreSQL 已安装但不知道 CLI 密码时，可先运行 `scripts\bootstrap_postgres.ps1`，交互输入密码执行 schema；脚本默认连接 `127.0.0.1:5432/postgres`，不保存密码。

本机 `amazon_us_qa` 已实际启动 tenant-scoped API 验证：`/v1/jobs/status` 返回 `pending=1891`、`reviews_pending=1`，`/v1/asin/US/B00RCPDCQU` 返回 `tenant_id=qa_latest_20260827`。
使用不存在的租户访问同一 ASIN 时返回 HTTP 404 `asin_not_found`，不会泄露其他租户数据。
本机 `amazon_us_qa` 的 tenant-scoped API 实际访问 `/readyz` 返回 HTTP 200 `ok=true`，证明数据库和 schema 就绪检查已经过 HTTP 监控路径生效。

schema 初始化后运行 `scripts\replay_postgres.ps1`，可交互输入密码完成 SQLite 回放并调用 PostgreSQL repository 验证；JSONB 字段由迁移工具自动适配，密码仅存在于当前 PowerShell 进程。

PowerShell 回放支持 `-TenantId qa_latest_20260827`，并会同时将该租户传给迁移和 PostgreSQL 验收；生产不应使用不明确的 `default` 租户。

## 路由

### Agent能力描述

```http
GET /v1/capabilities
```

使用`read-agent`凭据返回稳定的工具、路径、异步job模型和1–5条刷新上限；不返回tenant、DSN、代理、Cookie或主密钥。

### 就绪检查

```http
GET /readyz
```

`/healthz` 只证明进程存活；`/readyz` 会读取当前租户数据库并检查 PostgreSQL 关键 schema。数据库不可用或 schema 不完整时返回 HTTP 503 `database_unavailable` 或 `schema_not_ready`。PostgreSQL 后端正常时额外返回 `tenant_id` 和 `subject_type`，SQLite 不返回这两个字段。Operator 提交前同时核对两者，防止把竞品批次交给自有商品 Worker pool。若出现任一 Worker计数字段，则 `requested_workers/effective_workers/alive_workers` 必须全部存在，且只有三者相等、没有 `capacity_backpressure` 时才返回200；字段部分缺失或任何容量降级都返回503并拒绝新的refresh/campaign。拒绝POST前会在固定短deadline内有界读取已声明的小请求体，保证Windows正常客户端稳定收到结构化503，并让不完整或慢请求及时断开而不能长期占用服务线程。

### 健康检查

```http
GET /healthz
```

### 查询商品快照

```http
GET /v1/asin/US/{asin}
```

返回商品当前快照、任务状态、最近一次采集证据、媒体数量和内容模块数量。响应带有 `schema_version`、`retrieved_at`、`freshness`、`quality_status` 和 `source`；`freshness.age_seconds` 表示数据距当前的秒数，Agent 根据业务策略判断是否过期。

### 查询商品历史

```http
GET /v1/asin/US/{asin}/history
```

返回商品历史快照。SQLite 返回历史采集记录，PostgreSQL 返回 `product_snapshot` 历史记录；两者均按最新时间倒序，最多返回 20 条。

可按字段组判断过期：

```http
GET /v1/asin/US/{asin}?fields=price,availability
```

返回的 `freshness.stale_groups` 只列出已超过对应策略的字段组。

默认 freshness 策略由 [freshness_policy.py](/D:/woring/爬虫/Amazon%20Scraping/scripts/freshness_policy.py) 提供：价格、可售和 Offer 默认 4 小时；评分和评论默认 24 小时；内容和媒体默认 7 天；身份信息默认 30 天。调度器可按业务需要覆盖这些时间，不把它们当作 Amazon 的固定规则。

### 查询任务汇总

```http
GET /v1/jobs/status
```

返回各任务状态数量以及 `refresh_request` 队列状态，用于运营查看采集积压、按需刷新积压和失败情况。

本机测试验证使用 10 条美国 VPN 测试结果：健康检查通过，任务状态和单 ASIN 查询可读，批量查询 2 条返回正常。测试服务仅绑定 `127.0.0.1`，不作为生产服务暴露。

旧 SQLite 库若尚未执行 `context_json` 或 `transfer_bytes` schema 变更，API 会兼容读取并分别返回 `context_json: null` 或不提供传输字节；新采集证据会写入实际上下文和 HTTP 响应体 `transfer_bytes`。升级不要求 API 先写库。

本机 PostgreSQL 已执行幂等 schema 升级，`collection_evidence.context_json` 为 `jsonb`；公司数据库需在正式迁移窗口执行同一 schema，不能直接假设已完成。

旧 SQLite evidence 没有上下文时迁移为 JSONB `{}`，表示历史未知；新采集记录应包含实际 ZIP/国家/币种，不应把 `{}` 解读为美国默认区域。

### 查询刷新任务

```http
GET /v1/jobs/{job_id}?view=summary
```

这是Agent客户端的默认视图。返回刷新请求的`queued`、`claimed`、`completed`、`failed`或`cancelled`状态；终态只保留商品名称、价格、库存、评分、质量、证据来源/时间/状态、耗时和流量摘要。`evidence_after_request=false`时不得把历史证据当作本次结果。

排障时可显式使用`?view=detailed`取得完整商品和证据结构；默认不把raw路径、大型context或内部字段塞给Agent。命令行`agent-job`默认summary，只有本机运维显式传`--detailed`才请求详细视图。

### 提交按需刷新

Agent 推荐使用 1 至 5 条批量入口：

```http
POST /v1/asin/refresh
Content-Type: application/json

{"marketplace":"US","asins":["B00RCPDCQU","B00RCPDI50"],"reason":"price_is_stale"}
```

服务端去重并硬限制 1 至 5 个 ASIN。只有 `refresh-agent` 可以提交；`read-agent` 返回 403。PostgreSQL仓储先在同一事务内校验全部ASIN，再原子提交整批；相同 ASIN 已有 `queued/claimed` job 时复用活跃 job。请求接受后会唤醒 refresh Worker，Worker 使用 PostgreSQL lease 原子领取，且不会领取普通全量队列。

同一ASIN上一次已成功形成`completed/partial/variant`终态后，新的显式刷新属于新逻辑周期，会原子获得新的有界job预算并留下状态历史。活动job不会重置；预算耗尽、身份终态和历史预算未知也不能通过重复调用API获得新预算。

### Agent推荐调用顺序

1. `GET /v1/capabilities`读取能力和上限；
2. `POST /v1/asin/refresh`提交1–5个ASIN，只接收job ID；
3. 轮询`GET /v1/jobs/{job_id}?view=summary`直至终态；
4. 只有排障时请求`view=detailed`。

Agent不得要求manifest路径、DSN、代理凭据或Cookie。SSH仅用于部署和运维；同机Agent访问loopback，远程Agent经受控SSH隧道或位于公司电脑上的Agent网关调用，不能直接把8765暴露公网。

### 提交自然批次 campaign

```http
POST /v1/campaigns
Content-Type: application/json
Idempotency-Key: owned-full-20260913-v1

{
  "asins": ["B00RCPDCQU", "B00RCPDI50"],
  "subject_type": "own",
  "priority": 50,
  "deadline_seconds": 86400
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `asins` | 是 | 去重后的已登记 ASIN；部署默认最多 5,000 条 |
| `subject_type` | 否 | `own`、`competitor` 或 `candidate`，必须与服务 Worker scope 一致 |
| `priority` | 否 | 0–100，默认 50 |
| `deadline_seconds` | 否 | 60–604800，默认 86400 |
| `fields` | 否 | 希望采集的字段标签；不改变 Worker 的合规边界 |
| `freshness_seconds` | 否 | 可选的新鲜度要求 |

同一个幂等键和同一请求返回同一个 `campaign_id`；同键不同请求返回 409。进度使用 `GET /v1/campaigns/{campaign_id}`，默认只返回`total/terminal/counts`等数据库聚合汇总，不携带全部成员。需要成员时使用`?members=page&offset=0&limit=100`，`offset`必须为非负整数、`limit`为1–100，响应增加`member_page`；受控Operator兼容入口显式使用`?members=all`，普通Agent和MCP不得用它轮询大campaign。取消使用 `POST /v1/campaigns/{campaign_id}/cancel`：只取消尚未开始的成员，已经持有租约的成员继续报告真实终态。

Operator 上传 Excel 后也走这组接口。Operator 先读取 `/readyz`，确认 tenant 以及请求的 Worker 数均已生效，再提交 campaign；本地进度和结果文件只投影该 campaign 的成员状态，不读取历史商品状态推断本批完成。

### 流量口径

- HTTP compressed response：urllib收到的压缩响应体字节；存在缺失时同时返回`known_bytes`和`unknown_records`，不能把部分已知值称作总量。
- Proxy tunnel：本机认证CONNECT中继成功转发的TLS上传、下载与合计字节，是未来Firefox请求的新鲜本地计量。
- Firefox BiDi：保留主文档/子资源观测值；无法取得字节时明确unavailable。
- 供应商计费：只能以DataImpulse账单事实为准。Proxy tunnel不包含代理商计费口径、协议开销和未到达本机的字节，不能替代账单。

旧evidence没有Proxy tunnel字段，继续显示未测量；不回填或伪造为0。

兼容的单条入口仍可使用：

```http
POST /v1/asin/US/{asin}/refresh
Content-Type: application/json

{"requested_by":"agent-name","reason":"price_is_stale"}
```

HTTP线程只负责登记队列并返回 `202 Accepted`；真实采集由后台受控 Worker 执行。API仍可原子提交最多5条，但每个后台Worker的容量批次最多领取3条，容量按1–3条实际待领取数预约，减小坏会话的连带损失。每个Worker在独立子进程中创建自己的adapter/代理子池，数据库reservation保证slot不重叠；一个子Worker全局熔断不会终止其他健康子进程，但服务整体readiness仍fail closed并拒绝新campaign。PostgreSQL claim携带`priority/refresh_policy`：只有`own_core`或`competitor_core`可使用一次有界Firefox补全；普通/候选任务的transport、访问阻断、字段缺口或上下文不一致直接记录并进入既有恢复/终态，不启动Firefox。商品广度每ASIN新会话，同ASIN评论分页粘滞。

若Worker在领取后发生未预期异常，服务按固定脱敏原因将该Worker仍持有的refresh job置为`failed`，释放对应item lease并写状态历史；异常正文不进入API响应或健康状态。Agent客户端只允许HTTP loopback基址，拒绝外部主机、URL内嵌凭据、路径、query、fragment和HTTP重定向，避免scoped key外发。

### 批量查询

```http
POST /v1/asin/batch
Content-Type: application/json

{"marketplace":"US","asins":["B00RCPDCQU","B000000001"]}
```

每次最多查询 100 个去重后的 ASIN，只读取已有快照，不会因为批量查询而触发采集；不存在的 ASIN 会以 `found=false` 返回。

外部销量导入、`销量×价格 >= $2000`分层、168小时非核心cadence及Woot评论实验的字段和门禁见[`collection_efficiency.md`](collection_efficiency.md)。这些通道不把外部销量写入`product_snapshot`，Woot在固定样本门禁通过前也不接入生产Worker。

## 当前不支持

- 直接在 API 请求线程中运行爬虫；
- Agent 直接触发普通 pending 队列；超过 5 条必须使用有幂等键、截止时间和取消能力的 campaign；
- 远程公网访问；
- 无限代理轮换、CAPTCHA/WAF绕过、登录或个人Cookie；已批准粘滞会话仅在配置的有界上限内切换；
- 直接查询个人 Cookie、Token 或代理凭证。

其他业务路由遇到数据库异常时返回 HTTP 500 `database_unavailable`，不返回底层驱动或连接详情。
