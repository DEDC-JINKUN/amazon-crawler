# 真实页面单 ASIN 探针记录

## 运行条件

- 运行日期：2026-08-27
- 样本：1 条已授权的 FBA 补货 ASIN（临时清单，不进入仓库）
- 访问方式：HTTP 优先、无登录、无代理、无高并发
- 运行环境：Python 3.13.14、Selenium 4.47.0、Firefox 154.0.1、geckodriver 0.37.1

## 结果

- worker 退出码：0；
- 商品状态：`reviews_pending`；
- evidence：1 条，来源为 `http_html`，阻断原因为空；
- 商品快照：1 条；
- 媒体：34 条；
- 内容模块：23 条；
- 评论摘要：1 条；评论正文记录：0 条（下一步由分页任务继续）；
- 原始 HTML：已保存并记录路径。

本次页面核心字段均完成解析，因此没有启动 Firefox 兜底。该结果只能证明单页链路在当前环境可运行，不能推导大规模吞吐、封禁率或评论分页成功率。

## 重要业务限制

探针页面返回了 HKD 价格和“Deliver to Hong Kong”配送提示，说明当前网络或会话的区域上下文不是美国配送环境。该快照不能直接作为美国价格、可售或配送结论；正式采集前必须固定美国 ZIP/配送上下文，并把上下文写入任务维度。

本机 PostgreSQL 回放后，Collection API 已完成端到端只读验证：健康检查通过，任务汇总返回 1,891 个 pending 和 1 个 reviews_pending，指定 ASIN 查询成功，PostgreSQL 的时间字段可正常转换为 JSON。

### 10 条美西小批量（90001）

在原始 SQLite 只读复制出的测试副本上运行清单前 10 条，使用可见 Firefox 和美国 ZIP `90001`。10 条任务均进入可审计失败路径，未覆盖原有有效快照：1 条 `empty_review_page`，6 条 `context_mismatch`，2 条 `IncompleteRead`（已在 v0.1.19 归类为可重试错误），其余为区域上下文不匹配。该结果说明当前网络/页面变体下，Firefox 的配送 ZIP 在跨 ASIN 页面间不能稳定保持；不能据此宣称批量采集成功。测试副本和输出目录不作为生产数据源。

## 环境结论

Firefox/Selenium 运行依赖已经具备；此前失败的原因是 Selenium Manager 无法在线下载 geckodriver，以及受限目录无法写 SQLite。固定驱动路径和临时状态库后，单页 HTTP 采集已成功。
