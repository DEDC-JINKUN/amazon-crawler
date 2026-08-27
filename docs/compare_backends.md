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

输出中的 `ok: true` 才表示对账通过；`sample_mismatches` 会列出需要排查的 ASIN。它不比较实时采集时间，也不覆盖任何业务数据。
