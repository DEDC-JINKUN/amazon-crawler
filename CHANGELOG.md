# Changelog

## 0.1.1 - 2026-08-27

- 固定 Windows geckodriver 0.37.1 安装路径并校验下载包 SHA-256。
- 修正 Windows 安装脚本在缺少 `py` 启动器时回退到 `python`。
- 补充 Firefox/Selenium live preflight 和单 ASIN HTTP 真实探针记录。
- 确认本机 PostgreSQL 17 服务位置；Docker Compose 保留为可选开发环境。
- 交接文档改为使用固定 geckodriver 安装脚本，不依赖 Selenium Manager 在线下载。
