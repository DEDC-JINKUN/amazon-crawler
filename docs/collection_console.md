# Amazon Collection Console

## 定位

这是面向当前 PostgreSQL tenant 的本机只读运营控制台。它用于集中查看批次进度、状态、错误、阻断、商品快照、媒体 URL、商品页 top reviews、独立 Woot 评论、内容模块、evidence 和流量证据。独立评论默认从同一 DSN 的 `amazon_reviews` schema 只读查询，进度和失败状态不写入 PDP 任务状态；已校验聚合目录继续作为显式 fallback。

控制台不会：

- 请求 Amazon；
- 领取或重试任务；
- 修改任务状态或解除 blocked；
- 执行 raw HTML；
- 自动加载 Amazon 图片；
- 输出 DSN、数据库密码或代理凭据。

默认每 5 秒只读取本机 PostgreSQL 和 raw HTML 文件统计，因此不影响网络出口冷却。

## Windows 启动

在项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\run_console_windows.ps1 `
  -TenantId real_batch_20260828_500_04 `
  -RawHtmlDir data\postgres_real_batch_20260828_500_04\raw_html `
  -ReviewSource auto
```

`-ReviewSource auto` 优先 PostgreSQL；只有数据库评论面不可用、同时提供 `-WootReviewDir` 且显式绑定 `-TenantId` 时才回退到聚合文件。也可显式选择 `postgres`、`files` 或 `disabled`；`files` 必须同时传入 `-WootReviewDir` 和 `-TenantId`。未绑定租户的文件不会随页面租户切换复用。

密码使用隐藏输入，只存在于当前 PowerShell 进程。启动后打开：

```text
http://127.0.0.1:8770
```

按 `Ctrl+C` 停止服务并清除会话内数据库环境变量。

## 可选 API Key

需要本机 API Key 时，先在当前 PowerShell 设置：

```powershell
$env:AMAZON_COLLECTION_API_KEY = '当前会话的随机值'
```

然后添加 `-RequireApiKey`。页面右上角 `API Key` 按钮会把 Key 仅保存在当前浏览器标签页的 `sessionStorage`，关闭标签即消失。

## 页面

- 批次总览：任务进度、有效商品、blocked/failed、四种规模口径和字节证据；
- 本次运行：按 `run_id` 选择一次Worker启动，逐项显示ASIN、结果、来源、HTTP、错误和流量；
- 操作记录：canary、容量reservation（含Agent/直接Worker拒绝）、egress、preflight与采集控制；显示authorizing canary、reservation、计划/已测/可用/唯一/预约槽、事实过期时间、Gate原因和P95，`unknown`不显示为0；
- 状态与信号：status、stage、source、error、block、最近 run；
- 独立评论采集：明确显示PostgreSQL只读或聚合文件来源，展示ASIN成功数、文字评论数、媒体数、Partial/Source limited分布；
- 评论ASIN状态：按ASIN和覆盖状态筛选，每页50条，进度独立于商品任务；
- 任务表：按状态、阶段、ASIN、标题和错误筛选；
- ASIN 详情：商品、媒体 URL、top reviews、内容模块、评论摘要、历史和 evidence；Woot评论按ASIN每页20条读取，支持上一页/下一页，显示评分、作者、日期、Verified、Vine、Helpful及媒体链接。

媒体默认只显示 URL。只有用户手动点击链接时浏览器才会访问外部图片地址。raw HTML 只显示相对路径、哈希和时间，不通过控制台提供渲染路由。

评论只读路由：

- `GET /api/reviews/overview?tenant=<tenant>`：任务、覆盖、评论和媒体汇总；
- `GET /api/reviews/items?tenant=<tenant>&limit=50&offset=0&q=&coverage=`：ASIN状态分页；
- `GET /api/reviews/{asin}?tenant=<tenant>&limit=20&offset=0&rating=&verified=`：评论详情分页。

PostgreSQL评论查询使用只读事务和5秒statement timeout。`amazon_reviews`缺失或暂时不可用时，只把评论区域标成不可用，PDP批次、商品和证据区域继续工作。评论接口不提供写入、排期、启动或停止Worker。

## Task、Run 与 Action

- Task：同一个tenant中的长期ASIN任务，当前状态会随重试更新；
- Run：一次Worker进程启动，对应唯一 `run_id`；
- Action：该run中对某个ASIN/阶段的一次尝试。

控制台的“本次运行结果”按不可变evidence聚合。网络读取失败也会写没有raw HTML、但包含 `run_id`、`fetch_error` 和已知传输字节的evidence，因此新run不会漏掉失败action。

历史run在修复前可能缺少网络失败evidence。控制台只在该run首末evidence时间范围内补充状态变化，并明确标记为“时间推断”；这类记录不能冒充确定归属。

长期扩展到多Worker/跨进程审计时，可将现有evidence账本提升为独立 `collection_run`/`collection_action` 表，记录requested_limit、配置哈希、退出码和进程心跳；当前MVP不增加该schema。

## 安全边界

- HTTP Server 只允许 `127.0.0.1`、`localhost` 或 `::1`；
- PostgreSQL 连接设置 `default_transaction_read_only=on`；
- HTTP 只实现 GET，POST/PUT/DELETE 均返回 `405 read_only_console`；
- 静态文件使用精确白名单，不接受任意文件路径；
- PostgreSQL评论按当前UI tenant和provider过滤，浏览器不能指定DSN；评论查询要求limit为1–100、offset为0–100000，不返回连接异常正文；
- Woot聚合目录只能由服务启动参数或 `WOOT_REVIEW_AGGREGATE_DIR` 环境变量指定，浏览器不能提交文件路径；启动时验证manifest中的精确文件名、大小和SHA256；
- 43MB `reviews.jsonl` 不作为静态文件，也不会整包返回浏览器；服务启动时建立ASIN字节偏移，详情接口每次最多返回100条；
- 评论文件损坏或未配置时只把评论面板标为不可用，不改变PDP商品数据；
- CSP 禁止外部脚本、图片自动加载、iframe 和表单提交；
- 数据库异常统一返回 `database_unavailable`，不回显驱动或连接详情。
