# Woot 评论 PostgreSQL 链路

这条链路与 Amazon PDP 采集独立运行。它只读取 Woot 的公开评论 JSON，写入 `amazon_reviews` schema，不登录 Amazon，也不读写 `amazon_us.item_state` 或 `amazon_us.product_snapshot`。

Worker的`--user-agent`允许配置普通浏览器版本，但必须含`Agent/<name>`透明标记；验证发生在网络请求前，不做TLS指纹伪装或访问控制绕过。

## 组件

- `schema/migrations/20260914_woot_reviews.sql`：独立 schema、任务租约、查询检查点、原始证据、规范评论和追加式观测记录。
- `scripts/import_woot_reviews.py`：将已校验的批量聚合结果原子导入。同一 `tenant + import_batch_id + manifest SHA-256` 可幂等重放；同 ID 不同哈希会拒绝。
- `scripts/postgres_review_storage.py`：使用 `FOR UPDATE SKIP LOCKED` 的独立评论任务队列和事务存储。
- `scripts/woot_review_worker.py`：领取评论任务、执行 Woot provider，在一个数据库事务内保存运行、证据、检查点、评论和任务终态。

## 数据边界

`review_import_batch`、`review_raw_evidence` 和 `review_observation` 有 UPDATE/DELETE/TRUNCATE 拒绝触发器。`review_record` 是当前视图，可在新观测到来时更新；每次看到的原始规范 JSON 另行追加到 `review_observation`。

Woot 的端点可能对单查询截断或返回 404，因此 Worker 不会仅因“分页走完”就宣称 100% 覆盖。只有存在可追溯的 Amazon 书面评论分母且数量一致时，provider 才会标记 complete。

## 部署顺序

1. 在受控的测试数据库先执行迁移，再执行生产迁移。
2. 对聚合目录执行 `--validate-only`，确认 ASIN 数、评论数和 manifest 哈希。
3. 使用固定 `tenant-id` 和 `import-batch-id` 导入历史数据。
4. 如需重新抓取已导入 ASIN，用 Worker 的 `--schedule-existing`。
5. 先以 `--limit 1` 小流量验证，再逐步增加批量。遇到 HTTP 403/429 时 Worker 将当前任务标记 blocked 并立即停批。

```powershell
# 1. 仅校验，不连数据库
python scripts/import_woot_reviews.py `
  "outputs/woot-batch-asin-list-20260914-01/aggregate-final" `
  --tenant-id amazon_us_main `
  --import-batch-id woot-reviews-20260914-v1 `
  --validate-only

# 2. 迁移和真实导入（通过 DPAPI 临时注入 DSN）
$sha = (Get-FileHash schema/migrations/20260914_woot_reviews.sql -Algorithm SHA256).Hash.ToLowerInvariant()
& scripts/secure_dpapi_launcher.ps1 `
  -SecretNames @('AMAZON_US_POSTGRES_DSN') `
  -FilePath (Resolve-Path '.venv/Scripts/python.exe').Path `
  -ArgumentList @('scripts/apply_woot_review_migration.py','--mode','apply','--confirm-production','--expected-database','postgres','--expected-sha256',$sha)
$importArgs = @(
  'scripts/import_woot_reviews.py',
  'outputs/woot-batch-asin-list-20260914-01/aggregate-final',
  '--tenant-id','amazon_us_main',
  '--import-batch-id','woot-reviews-20260914-v1'
)
& scripts/secure_dpapi_launcher.ps1 `
  -SecretNames @('AMAZON_US_POSTGRES_DSN') `
  -FilePath (Resolve-Path '.venv/Scripts/python.exe').Path `
  -ArgumentList $importArgs

# 3. 将历史 ASIN 排期并小批量运行
$workerArgs = @(
  'scripts/woot_review_worker.py',
  '--tenant-id','amazon_us_main',
  '--schedule-asins','B0XXXXXXXX',
  '--limit','1',
  '--lease-seconds','3600',
  '--request-delay-seconds','1.0'
)
& scripts/secure_dpapi_launcher.ps1 `
  -SecretNames @('AMAZON_US_POSTGRES_DSN') `
  -FilePath (Resolve-Path '.venv/Scripts/python.exe').Path `
  -ArgumentList $workerArgs
```

## 运维要点

- 同一 ASIN 的 PDP 和 Review Worker 可以有不同进度、不同错误和不同重试时间。
- 默认延时为每次请求 1 秒。实际流量和耗时以 Worker 回执中的 `requests` 和 `response_body_bytes` 为准。
- 失败运行也保存 `receipt.failed.json` 和已获取证据，不把失败解释为零评论。
- 租约过期的 running 任务可被恢复为 retryable；任务完成时会再校验租约，防止过期 Worker 覆盖新结果。
- `outputs/woot-review-worker-production` 属于不可随意清理的生产证据目录；`raw_ref` 保存本机绝对路径和内容哈希。跨机部署前应先改为对象存储 URI，不能只复制数据库。

## 受管日批

Codex App heartbeat `woot`（“Woot评论每日更新”）已设为每天本地时间 02:30 运行。它先读取 `review_worker_status.py`；只有 `active_running_count>0` 才不重复启动，仅有过期租约时仍启动 Worker 进行回收。每轮最多保留 20 个总槽位，其中包括当前可领取的 pending/retryable、所有 running，以及已到期的 succeeded/source_limited；剩余槽位才从最旧 ASIN 中排期。

固定生产参数：

- `schedule-oldest=20`
- `min-age-hours=24`
- `limit=20`
- `lease-seconds=3600`
- `mode=adaptive`
- `max-requests=200`
- `request-delay-seconds=1.0`

正常 success 或单纯可重试 network error 保持静默；provider blocked/rate limited、失败到上限、数据库身份不匹配、证据缺失或租约异常时才通知。

```powershell
& scripts/secure_dpapi_launcher.ps1 `
  -SecretNames @('AMAZON_US_POSTGRES_DSN') `
  -FilePath (Resolve-Path '.venv/Scripts/python.exe').Path `
  -ArgumentList @('scripts/review_worker_status.py','--tenant-id','amazon_us_main','--expected-database','postgres')
```
