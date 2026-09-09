# 采集运行指标

`scripts/collection_metrics.py` 根据 SQLite 的 `collection_evidence` 只读生成一次 run 的可比指标，避免用“请求数”代替有效采集量。

```powershell
.venv\Scripts\python.exe scripts\collection_metrics.py `
  --db state\amazon_us.next10_http_retry.sqlite3 `
  --raw-html-dir data\amazon_us\next10_http_retry\raw_html `
  --run-id <run_id>
```

输出包括 evidence/ASIN 数、HTTP 与 Firefox 来源、错误分类、当前任务状态、各输出表记录数、原始 HTML 字节数、已知 HTTP 传输字节、耗时和页面/成功页面吞吐。`unique_successful_asin_count` 只统计本批 `/dp/{ASIN}` 商品页且同 URL 没有阻断或质量错误的 ASIN；仅评论页 2xx、ASIN 不一致和上下文错误不算成功。多条 evidence 重用同一 `raw_html_path` 时字节数和已知传输字节只计一次。旧 evidence 无传输字节时该指标为未知，不用本地 HTML 体积冒充。`--run-id` 不填写时取最新 run；做批次对比时应显式指定，避免把后续单 ASIN action 混入统计。
