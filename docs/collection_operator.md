# 普通爬虫网页 Operator

Operator 是独立的 loopback-only 写入口，默认监听 `127.0.0.1:8772`。它不修改现有只读 Console，也不要求业务用户使用 Agent、MCP、API 或 PowerShell。

## 用户流程

1. 管理员启动本机 Operator 服务。
2. 用户用浏览器打开 `http://127.0.0.1:8772`。
3. 上传 `.xlsx`，查看有效、重复和无效行预览。上传本身不会启动爬虫。
4. 若无法自动识别 `ASIN` 表头，在页面选择工作表和列。
5. 无效非空行清零后，选择自有、竞品或候选清单，再选择期望的 1 或 2 个 Worker，点击“开始采集”。
6. 页面轮询显示请求、处理中、完成、变体、失败、阻断和待处理数量，可停止当前批次。
7. 点击“打开运行大屏”查看固定业务 tenant 的运行、商品和变化数据。
8. 下载标准 manifest、结果 CSV/JSON 和回执。

## 本机控制

```powershell
.\run_owned_full_secure.ps1 agent-service -AgentWorkerCount 2
.\operator.ps1 start -BusinessTenantId owned_us_asin_20260902_full_01 -AgentPort 8765 -CampaignDeadlineSeconds 86400
.\operator.ps1 status
.\operator.ps1 open
.\operator.ps1 stop
```

`operator.ps1 stop` 只停止 Operator Web 服务，不会误杀已有 campaign。运行中的批次必须在页面点击“停止当前批次”；该操作向同一个 `campaign_id` 发取消请求，尚未开始的成员进入 cancelled，已经持有租约的成员继续到真实终态。原 Runner丢失时，Operator会重新启动受控取消 Runner；后端未确认 cancelled/terminal 前状态保持 `cancel_pending`，页面显示“取消待确认”并阻止下一批。后端completed是权威终态：无论它发生在停止请求之前还是取消调用返回时，都优先于本地stop意图；本地成员计数投影暂时落后也不能把completed改写为failed。

控制入口使用按规范化 release/data/port 生成的 Windows 命名 mutex 串行化 start 与 stop。stop 在等待超时后只会强制终止再次核验过 PID/启动时间的进程，随后同时确认进程和端口释放，并在删除前确认 lock 仍对应原进程；任何核验失败都会保留 lock 并返回非零。

部署时可由服务安装脚本传入固定的 `DataRoot`、`ConfigPath`、`VaultPath` 和内部只读 Console 端口。这些值是管理员侧配置，浏览器客户端没有对应参数。

Operator 的 `-BusinessTenantId` 和页面选择的 `subject_type` 必须分别与 Collection `/readyz` 返回的 `tenant_id`、`subject_type` 一致；不一致时在 campaign 提交前失败。`-CampaignDeadlineSeconds` 默认 86400（1 天），可在 60–604800 秒内配置，避免把原单次 Worker 的 90 分钟恢复时限误当成全量 campaign 截止时间。每次上传继续生成独立 `import_id` 和本地 `batch_id`，确认后绑定唯一 `campaign_id`。本地 batch 文件是下载和页面投影，不是第二个任务事实源。

## 五个接口

| 接口 | HTTP | 作用 |
|---|---|---|
| `upload_preview` | `POST /api/upload_preview` | 首次 multipart 上传，或以 JSON 对同一 `import_id` 重新选择工作表/列 |
| `start_batch` | `POST /api/start_batch` | 接受 `import_id`、`worker_count` 与 `subject_type`，异步幂等提交共享 campaign |
| `get_batch` | `GET /api/get_batch?batch_id=...` | 返回安全进度；省略 ID 时返回最近批次 |
| `stop_batch` | `POST /api/stop_batch` | 仅接受当前 `batch_id`，写入停止审计并终止对应受管宿主 |
| `download_artifact` | `GET /api/download_artifact?...` | 只映射 `manifest/results_csv/results_json/receipt` 四个固定文件 |

## XLSX 安全边界

- 只接受扩展名 `.xlsx` 和 ZIP 文件头；拒绝 `.xls`、`.xlsm`、宏内容类型、`vbaProject.bin`、外部关系、`xl/externalLinks`、加密项、DTD/实体和路径穿越。
- 兼容标准 OOXML 的相对 worksheet relationship 和单前导 `/xl/worksheets/...` package-absolute relationship；继续拒绝 `//`、反斜杠、`..` 逃逸、非 worksheet 类型及非 worksheet 路径。
- 默认上限：上传 5 MiB、解压 50 MiB、1,000 个 ZIP entry、单项异常压缩比 200、20 张工作表、10,000 行、5,000 个唯一 ASIN。
- 服务生成 `import_id`、`batch_id`、tenant 和所有文件路径；原文件名从不参与路径构造。
- 预览响应只包含 ASIN、相关行号和计数，不返回其他 Excel 单元格正文。
- 标准 manifest 包含 `asin,url,marketplace,source_site_label,source_workbook`，并记录源文件与 manifest SHA256。

## 鉴权与进程边界

- 仅允许 loopback 绑定、loopback 客户端与 `localhost/127.0.0.1/::1` Host；用于防止误开放和 DNS rebinding。
- 首次页面请求签发最多 20 分钟的随机 session cookie，设置 `HttpOnly; SameSite=Strict`；所有写请求还必须提供独立 CSRF token。
- 响应带 CSP、`nosniff`、禁止 frame、no-referrer 和 no-store。
- 浏览器不能提交可执行文件、脚本、DSN、代理、配置或输出路径。生产 argv 完全由服务端生成，`shell=False` 交给受管宿主。
- `operator_secure_runner.ps1` 只从 DPAPI vault 注入 Collection API 主密钥，在进程内派生 `refresh-agent` scoped key，再调用 loopback campaign；不向 Operator Web 服务或子进程注入 PostgreSQL DSN、代理用户名或代理密码。
- 每批保存 tenant、`campaign_id`、release/config/manifest/source 哈希、请求/实际 Worker 数、计数、时间和终态。Windows 命名 mutex 限制一个活动 Operator 批次；权威成员状态来自 `collection_campaign`。

## 候选部署变更清单

本候选尚未部署。部署阶段需要：

1. 从候选 commit 创建不可变 release，保留 archive SHA256。
2. 将 Operator 静态目录和新增脚本包含进 release；复用现有 Python venv，无新增依赖。
3. 为 Operator 指定独立数据目录、现有 DPAPI vault、8772 端口和 Collection 8765 端口；Operator tenant 必须与 Collection readiness 一致。
4. 为 Operator 新建独立计划任务或受管服务；由 Agent 服务独占 Worker pool，Operator 不再启动第二套普通 Controller。
5. ACL 只允许运行账户/SYSTEM 写 Operator 数据目录；防火墙不得开放 8772/8773 到公网。远程使用 SSH tunnel。
6. 部署前确认没有活动 campaign、租约或普通批次；部署后只做 health/UI 离线检查，真实请求另行授权。

## 真实小批验收计划

1. 先用 3 个已批准 ASIN、WorkerCount=1，验证上传预览不会产生网络请求、一次确认只生成一个 batch 和一个 campaign、进度和四项下载一致、停止只影响该 campaign。
2. 检查 3 条均有明确终态，且无重复 claim、无遗留租约、无 DSN/key/proxy/path 泄露。
3. 再运行最多 20 个已批准 ASIN；达到 20 个终态立即停止，不扩大样本。
4. 比较 requested/claimed/completed/variant/failed/blocked/pending、耗时、流量与 receipt 哈希；若出现 CAPTCHA 或容量 gate，按现有策略停止，不尝试绕过。
