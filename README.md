# Amazon US 商品网页爬虫

面向 Amazon.com 自有 ASIN 和竞品 ASIN 的统一网页采集 MVP。

## 当前开发边界

- HTTP 优先获取公开 HTML；页面字段不足时再使用 Firefox 渲染。
- SQLite 保存本地任务状态和断点，CSV 用于导出和人工检查。
- 原始 HTML、采集时间、来源和解析器版本必须可追溯。
- 当前 Windows POC 不启用代理池和多线程扩容。
- 生产阶段再接入经过批准的 IP 代理池和受控 Worker Pool。
- 不读取个人浏览器 Profile、Cookie、Token 或密码，不绕过验证码和访问控制。

## 目录

```text
amazon-scraping/
├── scripts/              # 采集、解析、验收代码
├── tests/                # 单元、回归和页面 fixture
├── config/               # Windows 与示例配置
├── docs/                 # 需求、技术设计、数据契约、流水线和验收说明
├── scripts/              # Windows 启动和安装脚本
├── data/                 # 本地导出（不提交真实数据）
└── state/                # SQLite 状态（不提交真实数据）
```

## 快速检查

```powershell
python -m pytest tests -q
```

## 下一步

1. 准备 Windows Python、Firefox 和 Selenium 运行环境。
2. 用少量已授权 ASIN 做真实页面采集。
3. 根据成功率、字段完整率、阻断率和耗时决定是否接入代理池和多 Worker。
