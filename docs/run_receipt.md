# 采集运行回执

`scripts/run_receipt.py` 将一次运行的结构验收、采集指标、传输字节和可选代理账单合并为一个 JSON 回执。它只读 SQLite、CSV 和 raw HTML，不启动网络采集、不修改任务状态。

```powershell
.venv\Scripts\python.exe scripts\run_receipt.py `
  --manifest amazon_us_asin_manifest.csv `
  --state state\traffic_test.sqlite3 `
  --output-dir data\traffic_test `
  --raw-html-dir data\traffic_test\raw_html `
  --run-id <run_id> `
  --output data\traffic_test\run_receipt.json
```

如果已有代理商后台前后用量和实际费用，可追加 `--proxy-usage-before-bytes`、`--proxy-usage-after-bytes`、`--proxy-charge-cny` 和 `--allocated-fixed-cost-cny`。回执中的 `verification.ok` 只表示结构验收通过；成本是否达标要看 `cost`，不能用 `ok=true` 代替。
