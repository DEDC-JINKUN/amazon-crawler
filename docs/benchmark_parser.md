# 离线解析吞吐基准

使用已保存的真实 HTML 测量本机解析器能力，不访问 Amazon：

```powershell
.venv\Scripts\python.exe scripts\benchmark_parser.py `
  --raw-html-dir data\amazon_us\next10_http_retry\raw_html
```

`--repeat > 1` 是合成重复，只用于 CPU 稳定性测试，不能当作真实网络吞吐。真实容量还要叠加 HTTP、评论分页、重试、验证码和冷却时间。
