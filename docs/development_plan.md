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
- [ ] 本机 PostgreSQL 容器建库、schema 执行和 SQLite→PostgreSQL 回放。
- [x] 清单缺失时使用开发样例，真实清单保持本地忽略。
- [x] 回归测试 43 项通过。
- [x] 采集覆盖率、来源和阻断原因报告。
- [x] SQLite→PostgreSQL 回放工具（真实数据库连接待环境就绪）。
- [x] 按需刷新队列消费：采集批处理优先处理 queued 请求，完成后更新结果。
- [x] Collection API 批量查询（单次最多 100 个 ASIN）。
- [x] 字段级 freshness 策略（价格/可售/Offer/评论/内容分级 TTL）。
- [x] 按字段 freshness 自动生成刷新请求队列。
- [x] 运行前 preflight 检查（清单、配置、Python、Firefox/Selenium 和状态目录）。

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
