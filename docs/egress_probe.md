# 付费出口上线前探针

`scripts/check_egress.py` 用于在批量采集前验证一条经过批准的 HTTP(S) 出口。它只访问指定探针 URL，不领取爬虫任务，不输出代理地址、账号、密码或响应正文。

## 使用

```powershell
.venv\Scripts\python.exe scripts\check_egress.py `
  --proxy-url "http://proxy.example:8080" `
  --target-url "https://www.amazon.com/robots.txt" `
  --proxy-username-env AMAZON_PROXY_USER `
  --proxy-password-env AMAZON_PROXY_PASS
```

探针返回 JSON：

- `ok=true`：2xx 且有非空响应体，可以进入小批量采集；
- `http_403` / `http_429`：暂停该出口，不把它交给批量任务；
- `network_error`：线路或认证不可用，先修复出口；
- `configuration_error`：代理 URL 或认证变量配置不合法。

探针成功不代表 Amazon 后续一定不阻断，只证明此时出口可连通。批量采集仍必须保留 2–3 个 ASIN 低速探测、429 冷却、403/CAPTCHA 熔断和可恢复断点。

探针通过后再运行 `preflight.py --require-live` 和 worker。代理凭证只放在当前进程环境变量中，不写入配置、日志或 Git。
