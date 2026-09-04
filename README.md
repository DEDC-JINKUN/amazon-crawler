# Amazon US 商品网页爬虫

面向 Amazon.com 自有 ASIN 和竞品 ASIN 的统一网页采集 MVP。

当前版本：`0.3.0`（PostgreSQL 正式 Worker）。

`develop`待发布更新：[美国站采集与简明控制台](docs/develop_us_marketplace_20260904.md)。
`main`保持正式运行版本；本开发分支不自动更新现有服务、生产配置或数据库。

本地测试结果可用 `scripts/evidence_health.py` 检查源 HTML 存在性和哈希；用 `scripts/collection_metrics.py` 按 `run_id` 查看请求数、流量和有效吞吐。
代理流量和成本实测见 [`docs/traffic_cost_validation.md`](docs/traffic_cost_validation.md)，并用 `scripts/traffic_cost_report.py` 结合代理商后台的用量差值出具报告。
付费出口批量前探针见 [`docs/egress_probe.md`](docs/egress_probe.md)。
一次运行的结构、流量和成本回执见 [`docs/run_receipt.md`](docs/run_receipt.md)。

## 当前开发边界

- HTTP 优先获取公开 HTML；页面字段不足时再使用 stock Firefox 渲染。付费HTTP CONNECT代理认证通过仅监听loopback的临时中继完成：中继只给上游CONNECT增加认证并转发不透明TLS字节，不做MITM、指纹伪装或验证码处理。
- PostgreSQL 是正式任务、断点、结果和历史的唯一事实源；SQLite 仅保留给历史回放和离线回归测试。
- 原始 HTML、采集时间、来源和解析器版本必须可追溯。
- PostgreSQL 使用租约和 `FOR UPDATE SKIP LOCKED` 支持多个 Worker 安全领取；当前 Windows 默认仍以单 Worker 小批量运行。
- 正式 PostgreSQL live 入口必须配置已批准粘滞端口，并先有同配置、同credential generation、未过期且状态/计数一致的非Amazon canary。系统在PostgreSQL中原子预约具体`session-NN`槽，跨tenant/Controller/Agent不允许超卖；预约或事实失效时在创建collection run、领取任务和访问Amazon前拒绝。
- 不读取个人浏览器 Profile、Cookie、Token 或密码，不绕过验证码和访问控制。

## 目录

```text
amazon-scraping/
├── scripts/              # 采集、解析、验收代码
├── tests/                # 单元、回归和页面 fixture
├── config/               # Windows 与示例配置
├── docs/                 # 需求、技术设计、数据契约、流水线和验收说明
├── schema/               # PostgreSQL 生产库结构
├── *.bat                 # Windows 启动、定时和验收脚本
├── data/                 # 本地导出（不提交真实数据）
└── state/                # 历史 SQLite 回放数据（不进入正式运行）
```

## 快速检查

```powershell
python -m pytest tests -q
```

`setup_windows.bat` 会同时安装 `requirements-dev.txt`，保证测试不依赖系统 Python 的全局包。

首次运行 `setup_windows.bat` 时，如果根目录没有业务清单，会自动复制两条记录的 `amazon_us_asin_manifest.example.csv` 作为离线开发样例。接入真实采集前，必须用经过确认的业务清单替换 `amazon_us_asin_manifest.csv`；真实清单不会提交到 Git。

## 正式 PostgreSQL 运行

完整步骤、环境变量和故障说明见 [`docs/postgres_production_worker.md`](docs/postgres_production_worker.md)。正式入口不会读取 SQLite，也不会把数据库密码写入仓库。

Windows本机推荐使用统一控制入口：

```powershell
.\crawler.ps1 canary -Limit 3
.\crawler.ps1 probe
.\crawler.ps1 run -Limit 10
.\crawler.ps1 status
.\crawler.ps1 console
.\crawler.ps1 stop
```

`canary` 对每个计划会话只访问一次精确允许的非Amazon HTTPS端点，禁止重定向，验证认证、CONNECT/TLS、延迟和本次运行内的出口去重；真实IP只在内存比较。它只证明代理连通容量，不证明Amazon业务成功。DPAPI vault每次轮换生成非秘密credential generation，使旧canary立即不匹配。`probe/run/reviews`先原子预约未被其他消费者占用的槽，并把canary operation、事实/过期时间、reservation和容量快照写入operation、collection run、receipt、evidence及Console；Console/receipt另列Amazon completed/variant/failed/blocked及访问控制率。超过100个action仍需显式 `-ConfirmLargeBatch`。

```powershell
$env:AMAZON_US_POSTGRES_DSN = 'host=127.0.0.1 port=5432 dbname=postgres user=postgres'
$env:PGPASSWORD = '仅在当前 PowerShell 会话填写'
$env:AMAZON_PROXY_CREDENTIAL_GENERATION = 'manual-proxy-generation-v1' # 非秘密；代理凭据轮换时必须同时更换
try {
  .\run_once_windows.bat
} finally {
  Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}
```

直接使用`crawler.ps1`、`run_once_windows.bat`或`run_scheduled_windows.bat`时必须显式提供上述非秘密generation。推荐的正式入口仍是`run_owned_full_secure.ps1`，它会从DPAPI vault自动注入并在轮换时更新。

定时入口 `run_scheduled_windows.bat` 会先将超过 24 小时的 PostgreSQL 商品快照加入刷新队列，再运行一个受限批次。

本机 PostgreSQL 开发环境：

```powershell
docker compose up -d postgres
docker compose ps
```

数据库只绑定本机 `127.0.0.1:5433`，schema 会在首次创建数据卷时自动执行。`.env` 仅用于本机开发并被 Git 忽略；切换公司数据库时只替换 DSN 和凭据，不提交 `.env`。

本机已有 PostgreSQL 服务时，可交互式执行 schema（密码不会写入项目）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap_postgres.ps1
```

PostgreSQL Collection API：

```powershell
python scripts/collection_api.py --backend postgres --dsn "$env:AMAZON_US_POSTGRES_DSN" --tenant-id amazon_us_local
```

默认只监听 `127.0.0.1`；Agent 通过它读取快照、任务状态、最近证据并提交按需刷新请求。

## Agent 调用爬虫

生产入口把 Collection API 与一个 `refresh-only` Worker 作为同一受控服务。Agent 只能查询既有数据，或一次提交 1 至 5 个已登记 ASIN 的按需刷新；不能触发全量采集。普通 Agent 命令会先幂等确保本机服务已启动，不需要先跑健康检查。

Agent 调用使用 DPAPI 派生的 scoped key，子进程不会得到 PostgreSQL DSN、代理密码或服务主密钥：

```powershell
.\run_owned_full_secure.ps1 agent-get -Asins B00RCPDCQU
.\run_owned_full_secure.ps1 agent-batch -Asins B00RCPDCQU,B00RCPDI50
.\run_owned_full_secure.ps1 agent-refresh -Asins B00RCPDCQU,B00RCPDI50 -Wait
.\run_owned_full_secure.ps1 agent-job -JobId refresh-example
.\run_owned_full_secure.ps1 agent-stop
```

`agent-service` / `agent-health` / `agent-status` / `agent-stop` 只供管理员手工运维；Agent 业务调用不依赖它们作为前置步骤。

服务身份同时指纹化 Agent 服务、Worker、代理池、API、存储模块和实际 TOML。业务调用发现已验证的旧版本进程时会受控替换；无法证明归属的监听器不会被终止或接管。

`agent-get`、`agent-batch` 和 `agent-job` 只要求受控 API 存活，因此 refresh Worker 因访问控制熔断时仍可读取历史数据；`agent-refresh` 是否可接受由 `/readyz` 和服务端 503 单独约束。

`agent-get`、`agent-batch` 和 `agent-job` 只要求受控 API 存活，因此 refresh Worker 因访问控制熔断时仍可读取历史数据；`agent-refresh` 是否可接受由 `/readyz` 和服务端 503 单独约束。

服务身份同时指纹化 Agent 服务、Worker、代理池、API、存储模块和实际 TOML。业务调用发现已验证的旧版本进程时会受控替换；无法证明归属的监听器不会被终止或接管。

带 `-Wait` 的刷新会等待 PostgreSQL job 进入 `completed`、`failed` 或 `cancelled`，并返回最新商品快照、evidence、请求到终态的耗时及可用流量字段。等待客户端每次只轮询一个job、请求间隔至少1.1秒，并遵守本机API的`Retry-After`；CLI自行以UTF-8输出JSON，不依赖Windows当前代码页。

同一次 Agent 批量的最多5条由一个runner run处理，但容量按当时实际可领取的1至5条计算。商品广度默认每ASIN一个代理会话；同一ASIN的评论分页在该会话内粘滞。`agent-health`分别报告`processed_actions`、`succeeded_actions`、`failed_actions`与`blocked_actions`；兼容字段`completed_actions`只等于成功数，不再表示已处理数。Agent与普通Worker共享原子容量reservation：每次claim和创建新槽前复核TTL，只使用分配给自己的生产槽和最多两个有界替换槽；耗尽内部HTTP重试的transport-bad槽会隔离，下一ASIN不得复用。HTTP challenge最多换一个出口，并只允许一次stock Firefox验证；Firefox仍为challenge即熔断。拒绝事实持久化并通过`/readyz`与Console解释，不做无限轮换、验证码填写/破解、登录或个人Cookie读取。

本机只读运营控制台：

```powershell
powershell -ExecutionPolicy Bypass -File .\run_console_windows.ps1 `
  -TenantId real_batch_20260828_500_04 `
  -RawHtmlDir data\postgres_real_batch_20260828_500_04\raw_html
```

打开 `http://127.0.0.1:8770`。控制台只查询 PostgreSQL 和本地 evidence，不访问 Amazon，也不提供任务修改操作。完整说明见 [`docs/collection_console.md`](docs/collection_console.md)。

正式运行前检查：

```powershell
python scripts/preflight.py --require-live --backend postgres --dsn-env AMAZON_US_POSTGRES_DSN
```

按字段新鲜度自动入队：

```powershell
python scripts/schedule_postgres_refresh.py --dsn-env AMAZON_US_POSTGRES_DSN --tenant-id amazon_us_local --subject-type own --min-age-hours 24
```

## 历史 SQLite 分析与迁移

以下命令只用于旧 POC 数据分析、迁移和对账，不进入正式 Worker 运行链路。

采集覆盖率报告：

```powershell
python scripts/coverage_report.py --db state/amazon_us.sqlite3 --output data/amazon_us/coverage_report.json
```

后端回放后，用只读对账工具确认 SQLite 与 PostgreSQL 的 API 视图一致（命令和安全的密码传递方式见 [`docs/compare_backends.md`](docs/compare_backends.md)）：

```powershell
$env:PGPASSWORD = '本机密码'
try { .venv\Scripts\python.exe scripts\compare_backends.py --sqlite state\amazon_us.sqlite3 --dsn 'host=127.0.0.1 port=5432 dbname=postgres user=postgres' --sample-limit 20 }
finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
```

SQLite 回放到 PostgreSQL（拿到公司 DSN 后执行）：

```powershell
python scripts/migrate_sqlite_to_postgres.py --sqlite state/amazon_us.sqlite3 --dsn "$env:AMAZON_POSTGRES_DSN"
```
