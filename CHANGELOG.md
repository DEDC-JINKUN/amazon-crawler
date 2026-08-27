# Changelog

## 0.1.15 - 2026-08-27

- Firefox 动态兜底继承同一显式 HTTP(S) 代理出口，避免 HTTP→Firefox 降级时意外直连。
- 增加 Firefox 代理配置和非法代理端口回归测试；Firefox 认证代理仍需供应商环境实测。

## 0.1.14 - 2026-08-27

- 付费代理认证支持从成对的环境变量读取，不在配置文件或代理 URL 中保存凭证。
- preflight 增加认证环境变量成对检查，补充 HTTP worker 代理认证回归测试。

## 0.1.13 - 2026-08-27

- preflight 增加代理配置门禁：仅接受明确 HTTP(S) 出口，拒绝 URL 内嵌凭证且不在诊断信息中泄露密码。

## 0.1.12 - 2026-08-27

- 增加只读 SQLite/PostgreSQL 后端对账工具，比较任务状态、刷新请求状态和 ASIN 样本结果。
- 增加对账工具说明及假仓储回归测试，避免仅凭迁移脚本退出码判断数据一致。

## 0.1.11 - 2026-08-27

- SQLite 新增商品历史快照保存，Collection API 增加 `/history` 查询。
- PostgreSQL 历史快照回放映射和测试完成。

## 0.1.10 - 2026-08-27

- 美国 live preflight 只接受 5 位 ZIP 或 ZIP+4 格式。
- 增加空 ZIP、非法 ZIP 的回归测试。

## 0.1.9 - 2026-08-27

- `preflight --require-live` 现在要求美国上下文配置 `postal_code`。
- Windows 定时任务改用严格 live preflight，未固定美国 ZIP 时不会启动采集。
- 57 项测试通过，记录区域上下文保护门禁。

## 0.1.8 - 2026-08-27

- 增加美国国家/币种上下文质量门禁，避免 HKD/非美国配送结果写入美国快照。
- 增加 `context_mismatch` evidence 和任务失败记录及回归测试。

## 0.1.7 - 2026-08-27

- 修复 PostgreSQL `datetime/date` 返回值导致 Collection API JSON 序列化失败的问题。
- 完成本机 PostgreSQL 后端 Collection API 健康、任务汇总和 ASIN 查询验证。

## 0.1.6 - 2026-08-27

- 记录真实探针返回 HKD/香港配送上下文的业务限制。
- 明确美国价格、可售和配送数据必须固定美国 ZIP/配送上下文后再验收。

## 0.1.5 - 2026-08-27

- 修复媒体、内容、评论等子表回放时 `marketplace/asin` 主键字段丢失的问题。
- 完成 1,892 条补货 ASIN 到本机 PostgreSQL 的实际回放和 repository 查询验证。

## 0.1.4 - 2026-08-27

- 修复 SQLite→PostgreSQL 回放时 JSONB 字段无法适配 psycopg `%s` 参数的问题。
- 增加 JSONB 参数回归测试。

## 0.1.3 - 2026-08-27

- 增加交互式 SQLite→PostgreSQL 回放脚本和 PostgreSQL repository 验证命令。
- 回放脚本通过临时 `PGPASSWORD` 接收密码，不写入仓库或持久化环境变量。
- 补充 PostgreSQL 连接和回放操作文档。

## 0.1.2 - 2026-08-27

- 增加交互式 PostgreSQL schema 初始化脚本，不保存数据库密码。
- 增加独立开发依赖，测试不再依赖系统 Python 全局包。
- Windows 安装脚本支持缺少 `py` 启动器时回退到 `python`。
- 固定 Firefox/geckodriver 运行路径和版本检查说明。

## 0.1.1 - 2026-08-27

- 固定 Windows geckodriver 0.37.1 安装路径并校验下载包 SHA-256。
- 修正 Windows 安装脚本在缺少 `py` 启动器时回退到 `python`。
- 补充 Firefox/Selenium live preflight 和单 ASIN HTTP 真实探针记录。
- 确认本机 PostgreSQL 17 服务位置；Docker Compose 保留为可选开发环境。
- 交接文档改为使用固定 geckodriver 安装脚本，不依赖 Selenium Manager 在线下载。
