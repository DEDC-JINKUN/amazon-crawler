# 付费出口上线前探针

`scripts/check_egress.py` 用于在批量采集前验证一条经过批准的 HTTP(S) 出口。它只允许精确的非Amazon HTTPS目标 `https://api.ipify.org?format=json`，禁止重定向并复核最终URL；不领取爬虫任务，不输出代理地址、账号、密码、出口IP或响应正文。

## 使用

```powershell
.venv\Scripts\python.exe scripts\check_egress.py `
  --proxy-url "http://proxy.example:8080" `
  --target-url "https://api.ipify.org?format=json" `
  --proxy-username-env AMAZON_PROXY_USER `
  --proxy-password-env AMAZON_PROXY_PASS
```

探针返回 JSON：

- `ok=true`：非Amazon健康目标返回2xx且有非空响应体；仍必须先有完整多会话canary和原子容量reservation，不能直接进入采集；
- `http_403` / `http_429` / `robot` / `captcha` / `automated_access` / `access_denied`：暂停该出口，不把它交给批量任务；
- `empty_response`：不把空响应当成健康线路；
- `network_error`：线路或认证不可用，先修复出口；
- `configuration_error`：代理 URL 或认证变量配置不合法。

探针成功不代表 Amazon 后续一定不阻断，只证明此时出口可连通。该脚本不再访问任何Amazon域名；批量采集仍必须保留固定cohort、429冷却、403/CAPTCHA熔断和可恢复断点。

探针通过后再运行 `preflight.py --require-live` 和 worker。代理凭证只放在当前进程环境变量中，不写入配置、日志或 Git。

也可以让 preflight 将探针作为上线门禁：

```powershell
.venv\Scripts\python.exe scripts\preflight.py --require-live --probe-egress
```

`--probe-egress` 是显式网络操作；当 `preflight.py` 使用 `--require-live` 且配置了 `proxy_url` 时，也会自动执行探针。默认直连 POC 不配置代理，因此仍只做本地检查。
