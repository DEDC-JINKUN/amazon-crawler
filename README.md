# Amazon US 商品网页爬虫

面向 Amazon.com 自有 ASIN 和竞品 ASIN 的统一网页采集 MVP。

当前版本：`0.1.9`（单机 MVP，增加美国 ZIP live 前置门禁）。

## 当前开发边界

- HTTP 优先获取公开 HTML；页面字段不足时再使用 Firefox 渲染。
- SQLite 保存本地任务状态和断点，CSV 用于导出和人工检查。
- 原始 HTML、采集时间、来源和解析器版本必须可追溯。
- 当前 Windows POC 不启用代理池和多线程扩容；限速和 Worker Pool 代码只作为生产扩展基础。
- 生产阶段再接入经过批准的 IP 代理池和受控 Worker Pool。
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
└── state/                # SQLite 状态（不提交真实数据）
```

## 快速检查

```powershell
python -m pytest tests -q
```

`setup_windows.bat` 会同时安装 `requirements-dev.txt`，保证测试不依赖系统 Python 的全局包。

首次运行 `setup_windows.bat` 时，如果根目录没有业务清单，会自动复制两条记录的 `amazon_us_asin_manifest.example.csv` 作为离线开发样例。接入真实采集前，必须用经过确认的业务清单替换 `amazon_us_asin_manifest.csv`；真实清单不会提交到 Git。

## 下一步

1. 准备 Windows Python、Firefox 和 Selenium 运行环境。
2. 安装固定版本 geckodriver：`powershell -ExecutionPolicy Bypass -File scripts\install_geckodriver.ps1`。
3. 用少量已授权 ASIN 做真实页面采集。
4. 根据成功率、字段完整率、阻断率和耗时配置 Token Bucket 限速。
5. 再决定是否接入授权代理池和多 Worker 扩容。

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

schema 初始化后，使用交互式回放脚本迁移本地状态并验证 PostgreSQL：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\replay_postgres.ps1
```

本地只读 Collection API：

```powershell
python scripts/collection_api.py --db state/amazon_us.sqlite3
```

默认只监听 `127.0.0.1`，不提供写入和刷新接口；Agent 后续通过它读取快照、任务状态和最近证据。

采集覆盖率报告：

```powershell
python scripts/coverage_report.py --db state/amazon_us.sqlite3 --output data/amazon_us/coverage_report.json
```

运行前检查：

```powershell
python scripts/preflight.py
python scripts/preflight.py --require-live
```

按字段新鲜度自动入队：

```powershell
python scripts/schedule_refresh.py --db state/amazon_us.sqlite3 --fields price,availability
```

SQLite 回放到 PostgreSQL（拿到公司 DSN 后执行）：

```powershell
python scripts/migrate_sqlite_to_postgres.py --sqlite state/amazon_us.sqlite3 --dsn "$env:AMAZON_POSTGRES_DSN"
```
