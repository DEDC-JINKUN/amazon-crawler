# PostgreSQL 正式 Worker 运行说明

## 定位

正式采集链路只使用 PostgreSQL 保存任务、租约、断点、结果和历史。SQLite 仅用于读取旧 POC 数据、回放迁移和离线单元测试，不参与正式运行，也不双写。

原始 HTML 仍保存在 `data/<批次>/raw_html/`。PostgreSQL 的 `collection_evidence.raw_html_path` 保存相对路径、哈希、来源、时间和解析器版本。

## 前置条件

- Python 虚拟环境已执行 `setup_windows.bat`；
- PostgreSQL 14+，本机实际验证版本为 17.10；
- `schema/postgres_schema.sql` 已执行；
- Firefox 和仓库固定版本 geckodriver 可用；
- `config/amazon_us.windows.toml` 的正式 ZIP 为 `90001`；
- PowerShell 当前会话已设置 `AMAZON_US_POSTGRES_DSN` 和数据库密码。

不要把密码写入 TOML、BAT、Git 或运行回执。

```powershell
$env:AMAZON_US_POSTGRES_DSN = 'host=127.0.0.1 port=5432 dbname=postgres user=postgres'
$env:PGPASSWORD = '本机密码'
```

## 初始化与预检

```powershell
.venv\Scripts\python.exe scripts\preflight.py `
  --require-live `
  --backend postgres `
  --dsn-env AMAZON_US_POSTGRES_DSN
```

预检必须同时通过 manifest、ZIP、Firefox、geckodriver 和 PostgreSQL租约字段检查。

只初始化 manifest、不访问 Amazon：

```powershell
.venv\Scripts\python.exe scripts\amazon_us_worker.py `
  --backend postgres `
  --tenant-id amazon_us_local `
  --subject-type own `
  --init
```

当前补货清单按 `own` 导入。竞品清单运行时必须显式使用 `--subject-type competitor`，不要混在同一主体分类中。

## 单批采集

```powershell
.venv\Scripts\python.exe scripts\amazon_us_worker.py `
  --config config\amazon_us.windows.toml `
  --backend postgres `
  --tenant-id amazon_us_local `
  --subject-type own `
  --worker-id worker-01 `
  --lease-seconds 600 `
  --live --once --limit 10
```

Worker 在一个事务中使用 `FOR UPDATE SKIP LOCKED` 领取任务。领取后写入租约令牌、Worker 标识和到期时间；只有租约持有者可以提交结果。程序异常退出后，其他 Worker 只能回收已过期租约，不会重置仍在工作的任务。

## 定时刷新

```powershell
.venv\Scripts\python.exe scripts\schedule_postgres_refresh.py `
  --dsn-env AMAZON_US_POSTGRES_DSN `
  --tenant-id amazon_us_local `
  --subject-type own `
  --min-age-hours 24 `
  --limit 1000
```

同一 ASIN 已存在 `queued` 或 `claimed` 刷新任务时不会重复入队。Windows计划任务使用 `run_scheduled_windows.bat` 顺序执行预检、刷新入队、Worker 和 PostgreSQL 验证。

## 验证数据

```powershell
.venv\Scripts\python.exe scripts\verify_postgres.py `
  --dsn-env AMAZON_US_POSTGRES_DSN `
  --tenant-id amazon_us_local `
  --asin B00RCPDCQU
```

Collection API：

```powershell
.venv\Scripts\python.exe scripts\collection_api.py `
  --backend postgres `
  --dsn "$env:AMAZON_US_POSTGRES_DSN" `
  --tenant-id amazon_us_local
```

## 已验证结果

- 两个并发 Worker 从真实 PostgreSQL 领取到不同 ASIN；
- 事务异常不会留下半条 manifest 或半套商品结果；
- 商品 evidence、追加快照、媒体、内容模块、评论摘要、评论记录和状态可以直接写入 PostgreSQL；
- `B00RCPDCQU` 真实验收得到完整标题、5 条 bullets、34 条媒体和 23 个内容模块；
- HTTP 页面显示 Portland `97230` 时会触发 ZIP不一致门禁；Firefox 提交 `90001`、刷新页面并再次确认后才允许保存；
- 空标题、ASIN不一致、错误币种和错误配送 ZIP 均不能进入成功状态。

## 已知限制

Amazon 独立评论 URL 当前可能返回 HTTP 200 但不包含 review DOM，并出现登录入口。系统会：

- 尝试 portal URL；
- 尝试稳定 `/product-reviews/{ASIN}` URL；
- 在可用时降级到 Firefox；
- 仍为空时记录 `empty_review_page`、保留评论阶段和页码、增加失败次数；
- 不把空页或登录页伪装成评论采集成功。

商品详情页已经显示的 `top_reviews` 会随商品快照保存。独立评论分页仍需要继续验证 Amazon登录上下文与访问策略，当前不读取个人浏览器 Cookie，也不绕过登录或验证码。

## 清理会话凭据

```powershell
Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
Remove-Item Env:AMAZON_US_POSTGRES_DSN -ErrorAction SilentlyContinue
```
