# 竞品候选审核入池

候选发现后，运营将批准的 ASIN 每行写入一个文本文件，再执行：

```powershell
.venv\Scripts\python.exe scripts\promote_competitor_candidates.py `
  --candidates candidates.csv `
  --approved-asins approved.txt `
  --output competitor_manifest.csv
```

工具只把批准且存在于候选文件中的 ASIN 写入标准 manifest，并标记 `competitor_approved`；未批准、拼写错误或不在候选中的 ASIN 会报错，不会静默入池。
