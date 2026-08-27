# 个人开发计划

## 当前进度

- [x] HTTP 优先适配器和 Firefox 懒加载兜底。
- [x] HTTP/Firefox 来源证据区分。
- [x] Token Bucket 全局/出口级限速基础和受控 Worker Pool 基础。
- [x] 授权出口池登记与最多一次备用出口切换基础。
- [x] 本地只读 Collection API（快照、任务状态、最近证据）。
- [x] PostgreSQL 初版 schema（生产迁移目标，不影响当前 SQLite POC）。
- [x] Collection API 与 SQLite 查询层解耦，后续可替换为 PostgreSQL repository。
- [x] PostgreSQL repository 和可选驱动入口（未连接真实数据库）。
- [x] 本机 PostgreSQL schema 执行和 SQLite→PostgreSQL 回放。
- [x] 记录真实探针的区域上下文限制（HKD/香港配送）；美国 ZIP 固定仍是正式上线前门禁。
- [x] PostgreSQL 后端 Collection API 端到端只读验证。
- [x] SQLite/PostgreSQL 后端状态和 ASIN 样本只读对账。
- [x] 代理 URL preflight 校验（禁止 URL 内嵌凭证）。
- [x] HTTP 代理认证从成对环境变量读取，不持久化凭证。
- [x] Firefox 动态兜底继承显式代理出口（认证代理待真实环境验证）。
- [x] 区域上下文不匹配时由 Firefox 隔离会话设置 ZIP 并重新采集。
- [x] 区域不匹配转 Firefox 的重采集回归测试。
- [x] 区域不匹配转 Firefox 的生产实现已纳入发布版本。
- [x] HTTP chunked 断片归类为可重试失败，不使 worker 崩溃。
- [x] 美西 10 条 live 小批量测试（结果已记录，未通过生产验收）。
- [x] 美国 VPN 下全新空状态库 1 条 live 采集（商品页入库，字段和证据可追溯）。
- [x] 美国 VPN/90001 下全新 SQLite 10 条小批量验证（9 条成功，1 条 ASIN 不匹配拦截）。
- [x] 对齐运营竞品来源、HTML 本地存储和默认单 ZIP 策略。
- [x] 覆盖率报告区分原始 HTML 字段存在、为空和不可检查。
- [x] 修复 description 容器误吸收后续模块文本。
- [x] 解析修复后重复 10 条美国 VPN 回归（网络断片导致样本不足，未扩大规模）。
- [x] 受控 HTTP 传输重试及 10 条回归（9 条成功、0 条 IncompleteRead、32.99 秒）。
- [x] 评论 portal 入口空页时尝试 product-reviews 稳定入口。
- [x] 按 run_id 生成真实采集流量、耗时和有效吞吐指标。
- [x] 评论入口降级实现和测试已纳入发布版本。
- [x] HTTP 错误正文断片容错，30 条容量测试遇 captcha 按设计停止。
- [x] 30 条容量测试配置纳入版本，命令可复现。
- [x] CAPTCHA 阻断停止批次、保留未领取任务 pending 的回归验收。
- [x] 人工复核失败/阻断任务受控重入队并记录历史。
- [x] 离线 HTML 解析吞吐基准，拆分解析与网络瓶颈。
- [x] CAPTCHA 测试副本人工确认后的重入队实操演练。
- [x] 评论备用入口为空时保存独立 evidence。
- [x] 主报告与实际 HTTP opener/会话策略对齐。
- [x] 测试副本显式验收参数和 collecting 状态解释。
- [x] 评论增量幂等去重、编辑更新和分页游标回归。
- [x] 评论分页离线大批量回放和去重吞吐基准。
- [x] 本机测试库 Collection API 健康、状态、单 ASIN 和批量查询验证。
- [x] 重入队 dry-run 预览和 selected/updated 审计输出。
- [x] 离线竞品 ASIN 候选发现、去重和运营审核标记。
- [x] 运营批准竞品 ASIN 后安全转换为正式 manifest。
- [x] 价格显示归一化，避免视觉节点重复且不转换汇率。
- [x] Buy Box 原文及可选 seller/coupon/delivery 结构化字段。
- [x] 竞品候选发现支持 `data-asin` 搜索卡片属性。
- [x] 审计并明确 BSR/类目与卖家/Coupon/配送字段落点。
- [x] evidence 保存 ZIP/国家/币种上下文并同步迁移链路。
- [x] 美国上下文质量门禁（国家/币种不匹配不写入快照）。
- [x] 美国 ZIP 格式门禁（5 位或 ZIP+4，live 模式必填）。
- [x] 商品历史快照保存和 `/history` 查询。
- [x] 本机 PostgreSQL 交互式 schema 初始化脚本（实际执行待输入密码）。
- [x] 清单缺失时使用开发样例，真实清单保持本地忽略。
- [x] 回归测试 43 项通过。
- [x] 采集覆盖率、来源和阻断原因报告。
- [x] SQLite→PostgreSQL 回放工具（真实数据库连接待环境就绪）。
- [x] 按需刷新队列消费：采集批处理优先处理 queued 请求，完成后更新结果。
- [x] Collection API 批量查询（单次最多 100 个 ASIN）。
- [x] 字段级 freshness 策略（价格/可售/Offer/评论/内容分级 TTL）。
- [x] 按字段 freshness 自动生成刷新请求队列。
- [x] 运行前 preflight 检查（清单、配置、Python、Firefox/Selenium 和状态目录）。
- [x] 单 ASIN 真实页面探针（HTTP 成功、原始 HTML 和字段输出可追溯）。
- [x] 开发依赖独立锁定（venv 内 pytest，不依赖系统全局包）。
- [x] 交互式 PostgreSQL 回放与验证脚本（实际执行待用户输入密码）。

## MVP 顺序

1. 字段与任务契约：固定 ASIN、页面字段、刷新频率、错误分类和验收口径。
2. HTTP 采集：获取公开 HTML，保存原始页面和请求证据。
3. 解析与质量门禁：解析商品、媒体、A+、Offer、评分、评论和规格；校验 ASIN 与页面真实性。
4. 断点与失败恢复：记录任务状态、重试次数、评论分页游标和失败原因。
5. Firefox 兜底：仅处理 HTTP 缺字段或必须 JavaScript 渲染的页面。
6. 生产化：接入 PostgreSQL、Collection API、Weknora 发布和监控。
7. 扩容：最后再接入授权 IP 代理池和受控 Worker Pool。

当前的出口池模块只接受人工提供的明确出口，不扫描公网代理、不自动无限轮换，也不把代理凭证写入配置。

## 一周 MVP 验收

- 能读取一份 ASIN 清单并创建任务。
- 能通过 HTTP 获取并解析公开商品页面。
- HTTP 缺字段时能切换 Firefox，且不覆盖有效旧数据。
- 进程中断后能从断点继续。
- 403、429、验证码、登录墙和空页面进入明确的失败路径。
- 原始 HTML、采集时间、来源和解析器版本可追溯。
- 测试和离线 fixture 可重复运行。

## 暂不实现

- 全量历史评论长期抓取。
- 媒体二进制批量下载。
- 多账号自动轮换。
- 无限代理轮换或验证码破解。
- 直接让 Agent 运行爬虫脚本。
