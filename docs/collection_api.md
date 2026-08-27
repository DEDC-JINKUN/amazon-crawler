# Collection API（本地只读版）

Collection API 是 Agent 查询采集结果的统一入口。当前版本以只读方式打开 SQLite，不启动采集、不修改任务、不暴露到公网。API 通过 `collection_storage.py` 的 repository 接口访问数据，后续替换 PostgreSQL 时保持路由和响应不变。

## 启动

```powershell
python scripts/collection_api.py --db state/amazon_us.sqlite3 --host 127.0.0.1 --port 8765
```

默认只监听回环地址 `127.0.0.1`。如需部署到其他机器，必须先增加认证、网络隔离和权限控制，不能直接修改 host 绕过限制。

PostgreSQL 环境准备好后，安装可选依赖并切换后端：

```powershell
python -m pip install -r requirements-postgres.txt
python scripts/collection_api.py --backend postgres --dsn "postgresql://user:password@host:5432/dbname"
```

DSN 不写入仓库、日志或配置提交；生产环境通过受控环境变量或密钥管理注入。

## 路由

### 健康检查

```http
GET /healthz
```

### 查询商品快照

```http
GET /v1/asin/US/{asin}
```

返回商品当前快照、任务状态、最近一次采集证据、媒体数量和内容模块数量。响应带有 `schema_version`、`retrieved_at`、`freshness`、`quality_status` 和 `source`；`freshness.age_seconds` 表示数据距当前的秒数，Agent 根据业务策略判断是否过期。

### 查询任务汇总

```http
GET /v1/jobs/status
```

返回各任务状态数量，用于运营查看积压和失败情况。

## 当前不支持

- `POST /refresh` 等写入或刷新接口；
- Agent 直接运行爬虫；
- 远程公网访问；
- 直接查询个人 Cookie、Token 或代理凭证。

后续接入 PostgreSQL 时保持相同路由和响应契约，将 SQLite repository 替换为 PostgreSQL repository 即可。
