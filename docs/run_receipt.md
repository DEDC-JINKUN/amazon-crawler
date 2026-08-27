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

如果已有代理商后台前后用量和实际费用，可追加 `--proxy-usage-before-bytes`、`--proxy-usage-after-bytes`、`--proxy-charge-cny` 和 `--allocated-fixed-cost-cny`。回执中的 `verification.ok` 只表示结构验收通过；成本是否达标要看 `cost`，不能用 `ok=true` 代替。`action_items` 中的 `blocked_asins` 和 `failed_asins` 是运营处理入口，`exhausted_failed_asins` 优先进入人工复核。

`run_scheduled_windows.bat`、`run_once_windows.bat` 和 `verify_windows.bat` 会自动写入 `data/amazon_us/run_receipt.json`。回执使用临时文件替换，写入中断不会截断上一份回执；读库失败时不覆盖旧回执。回执生成失败不会覆盖已有验收文件；脚本仍会先返回 worker、物化或验收的原始错误码。
