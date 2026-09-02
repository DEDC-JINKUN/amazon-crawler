# Collection API 与 Agent 刷新服务

Collection API 是 Agent 查询采集结果和提交小批按需刷新的统一入口。生产模式只使用 PostgreSQL，并由同一服务内的 `refresh-only` Worker 消费刷新队列；SQLite 入口只保留给离线兼容测试。服务不暴露到公网。

## 启动

生产启动、状态、健康检查和停止均使用固定批次包装器，不需要重复传 tenant、manifest 或目录：

```powershell
.\run_owned_full_secure.ps1 agent-service
.\run_owned_full_secure.ps1 agent-status
.\run_owned_full_secure.ps1 agent-health
.\run_owned_full_secure.ps1 agent-stop
```

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

### 就绪检查

```http
GET /readyz
```

`/healthz` 只证明进程存活；`/readyz` 会读取当前租户数据库并检查 PostgreSQL 关键 schema。数据库不可用或 schema 不完整时返回 HTTP 503 `database_unavailable` 或 `schema_not_ready`。PostgreSQL 后端正常时额外返回 `tenant_id`，SQLite 不返回该字段。

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
GET /v1/jobs/{job_id}
```

返回刷新请求的 `queued`、`claimed`、`completed`、`failed` 或 `cancelled` 状态。终态响应额外包含最新商品快照、最新 evidence、`evidence_after_request`、`requested_at/claimed_at/completed_at`、总耗时和 evidence 中可用的流量字段；`evidence_after_request=false` 时不得把历史 evidence 当作本次刷新结果。

### 提交按需刷新

Agent 推荐使用 1 至 5 条批量入口：

```http
POST /v1/asin/refresh
Content-Type: application/json

{"marketplace":"US","asins":["B00RCPDCQU","B00RCPDI50"],"reason":"price_is_stale"}
```

服务端去重并硬限制 1 至 5 个 ASIN。只有 `refresh-agent` 可以提交；`read-agent` 返回 403。PostgreSQL仓储先在同一事务内校验全部ASIN，再原子提交整批；相同 ASIN 已有 `queued/claimed` job 时复用活跃 job。请求接受后会唤醒 refresh Worker，Worker 使用 PostgreSQL lease 原子领取，且不会领取普通全量队列。

兼容的单条入口仍可使用：

```http
POST /v1/asin/US/{asin}/refresh
Content-Type: application/json

{"requested_by":"agent-name","reason":"price_is_stale"}
```

HTTP线程只负责登记队列并返回 `202 Accepted`；真实采集由后台受控 Worker 执行。若 Worker 因访问控制或内部错误进入 `blocked/failed`，`/readyz` 返回 503，服务拒绝新增刷新，避免任务无限积压。

若Worker在领取后发生未预期异常，服务按固定脱敏原因将该Worker仍持有的refresh job置为`failed`，释放对应item lease并写状态历史；异常正文不进入API响应或健康状态。Agent客户端只允许HTTP loopback基址，拒绝外部主机、URL内嵌凭据、路径、query、fragment和HTTP重定向，避免scoped key外发。

### 批量查询

```http
POST /v1/asin/batch
Content-Type: application/json

{"marketplace":"US","asins":["B00RCPDCQU","B000000001"]}
```

每次最多查询 100 个去重后的 ASIN，只读取已有快照，不会因为批量查询而触发采集；不存在的 ASIN 会以 `found=false` 返回。

## 当前不支持

- 直接在 API 请求线程中运行爬虫；
- Agent 触发全量抓取、普通pending队列或超过5条的刷新；
- 远程公网访问；
- 自动代理轮换、CAPTCHA/WAF绕过、登录或个人Cookie；
- 直接查询个人 Cookie、Token 或代理凭证。

其他业务路由遇到数据库异常时返回 HTTP 500 `database_unavailable`，不返回底层驱动或连接详情。
