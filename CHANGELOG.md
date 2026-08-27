# Changelog

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
