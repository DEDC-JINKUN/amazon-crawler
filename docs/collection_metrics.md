# 采集运行指标

`scripts/collection_metrics.py` 根据 SQLite 的 `collection_evidence` 只读生成一次 run 的可比指标，避免用“请求数”代替有效采集量。

```powershell
.venv\Scripts\python.exe scripts\collection_metrics.py `
  --db state\amazon_us.next10_http_retry.sqlite3 `
  --raw-html-dir data\amazon_us\next10_http_retry\raw_html `
  --run-id <run_id>
```

输出包括 evidence/ASIN 数、HTTP 与 Firefox 来源、错误分类、当前任务状态、各输出表记录数、原始 HTML 字节数、耗时和页面/成功页面吞吐。`--run-id` 不填写时取最新 run；做批次对比时应显式指定，避免把后续单 ASIN action 混入统计。
