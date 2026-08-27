# Collection API（本地只读版）

Collection API 是 Agent 查询采集结果的统一入口。当前版本只读 SQLite，不启动采集、不修改任务、不暴露到公网。

## 启动

```powershell
python scripts/collection_api.py --db state/amazon_us.sqlite3 --host 127.0.0.1 --port 8765
```

默认只监听回环地址 `127.0.0.1`。如需部署到其他机器，必须先增加认证、网络隔离和权限控制，不能直接修改 host 绕过限制。

## 路由

### 健康检查

```http
GET /healthz
```

### 查询商品快照

```http
GET /v1/asin/US/{asin}
```

返回商品当前快照、任务状态、最近一次采集证据、媒体数量和内容模块数量。响应带有 `schema_version`、`retrieved_at`、`quality_status` 和 `source`。

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

后续接入 PostgreSQL 时保持相同路由和响应契约，将 SQLite 查询层替换为数据库适配器即可。
