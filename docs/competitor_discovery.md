# 竞品 ASIN 候选发现

运营确认的竞品来源包括已有竞品表、关键词搜索、类目/相关商品和系统发现。`discover_competitor_candidates.py` 对已保存的 Amazon 搜索或相关商品 HTML 做离线提取：只保留 Amazon.com 商品链接、去重 ASIN，并标记为 `candidate`。

```powershell
.venv\Scripts\python.exe scripts\discover_competitor_candidates.py `
  --input search.html `
  --source-type keyword_search `
  --source-query "pressure sensor" `
  --source-url "https://www.amazon.com/s?k=pressure+sensor" `
  --output candidates.csv
```

候选必须由运营审核后才能进入正式监控；工具不访问网络，也不把推荐位自动视为竞品。
