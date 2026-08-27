# SQLite / PostgreSQL 后端对账

迁移脚本成功只代表 SQL 已执行；`compare_backends.py` 进一步验证两套后端向 Collection API 暴露的任务状态和商品结果是否一致。

## 使用

在项目根目录执行，密码通过临时 `PGPASSWORD` 或 PostgreSQL 密码文件提供，不要写进命令行、代码或仓库：

```powershell
$env:PGPASSWORD = '在本机安全输入的密码'
try {
  .venv\Scripts\python.exe scripts\compare_backends.py `
    --sqlite state\amazon_us.sqlite3 `
    --dsn 'host=127.0.0.1 port=5432 dbname=postgres user=postgres' `
    --sample-limit 20
} finally {
  Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}
```

工具只读两套数据库，比较：

- `item_state` 的状态汇总；
- `refresh_request` 的状态汇总；
- 按 ASIN 排序抽取的最多 100 个样本：任务状态、来源、商品是否存在、媒体数、A+ 内容模块数和最新 evidence 的 `transfer_bytes`。

`verify_postgres.py` 会先返回 `schema_contract` 和 `schema_ok`，检查 `next_retry_at`、`context_json` 和 `transfer_bytes` 是否存在；字段缺失时不进入业务数据对账。

`schema_ok=false` 时脚本不会继续查询任务表或单 ASIN，返回码为 `2`；先按提示执行最新 schema，再重跑验收和对账。

输出中的 `ok: true` 才表示对账通过；`sample_mismatches` 会列出需要排查的 ASIN。它不比较实时采集时间，也不覆盖任何业务数据。

如果两套后端的状态数量或样本证据不一致，不要直接判定爬虫失败。先确认是否使用了同一清单、租户、回放时间和解析器版本；旧租户的历史回放不应与当前 SQLite 直接作生产对账。

本机 QA 回放模板：先新建独立数据库（例如 `amazon_us_qa`），再执行 schema、SQLite 回放和 `verify_postgres.py --asin <ASIN>`。本次回放结果为 `pending=1891`、`reviews_pending=1`，单 ASIN 来源为 `http_html`，`transfer_bytes` 可正常返回；旧默认数据库的历史数据不应覆盖或直接当作当前基线。
