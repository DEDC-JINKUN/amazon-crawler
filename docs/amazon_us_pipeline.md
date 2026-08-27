# Amazon US 本地采集流水线

## 范围与合规硬闸门

当前输入是 `amazon_us_asin_manifest.csv`，来自领星 FBA 补货建议导出，不是 Listing 全量。US manifest 当前为 1,892 个 unique `(marketplace, asin)`，仅作为首轮一次性补全的输入范围。

默认命令只做离线初始化或 CSV 物化，不访问 Amazon。live 模式必须显式指定 `--live`，先使用 HTTP 获取公开 HTML；只有页面字段不足时才懒加载 Selenium Firefox 的独立临时 profile、默认 headless、可配置 `geckodriver_path`。不会读取 Chrome Profile、自动登录、Cookie、Token 或其他凭据。当前 POC 不启用代理池；媒体只保存公开 URL 和 DOM 元数据，不下载媒体文件。

HTTP 请求支持全局和出口级 Token Bucket 限速：`global_requests_per_second`、`egress_requests_per_second` 和 `rate_burst` 默认为 0/0/1，保持 POC 不主动等待；生产接入授权出口后再设置。限速模块不负责代理轮换，出口切换仍需经过批准、隔离和人工审计。

遇到 HTTP `403`、或页面标题/正文包含明确阻断短语 `robot check`、`enter the characters`、`captcha`、`sorry we just need to make sure you're not a robot`、`automated access`、`access denied`、`too many requests`，立即写入 `blocked` 和 `block_reason`，停止本次流水线，不重试、不切换 IP、不代理规避、不伪装身份。HTTP `429` 是可恢复的限流：保留当前 product/review 游标，写 evidence 和 `http_429`，本次 run 停止；下一小时使用同一会话类型/透明身份重试同一 action，不代理、不换 IP。普通文本如 `robot vacuum` 不会阻断。

## SQLite 事实源与 Box 映射

SQLite 是唯一事实源。每个 page action 只发一个页面请求，并在一个事务内写入 state、页面记录、内容和 evidence；run 结束后统一原子物化六个 CSV。

| 来源范围 | SQLite 表 / CSV |
| --- | --- |
| ASIN、canonical、标题、品牌、库存、评分、reported rating/review 数、价格 | `product_snapshot` / `product_snapshot.csv` |
| bullets 逐条、description、specs、BuyBox、Top reviews、A+、from brand | `content_module` 与 `product_snapshot` JSON / 对应 CSV |
| gallery、product videos、A+、from brand、review 的图片/视频入口与 URL | `media_asset` / `media_asset.csv`；过滤导航、推荐、广告、tracking |
| 评论摘要与 reported/fetched/pages/next | `review_summary` / `review_summary.csv` |
| 评论记录、日期、locale、verified、图片、分页页码 | `review_record` / `review_record.csv` |
| 每次请求 URL、HTTP 状态、内容哈希、阻断原因、错误码 | `collection_evidence` / `collection_evidence.csv`；保留 run 历史 |

## 状态与断点

`item_state.status` 枚举：`pending`、`running`、`product_done`、`reviews_pending`、`succeeded`、`blocked`、`failed`。状态转移由 `_set_status` 强制，`state_history` 保存每次转移。启动时所有残留 `running` 按 `resume_status` 恢复到 `reviews_pending` 或 `pending`。

- `pending -> running`：商品 action。
- `running -> product_done -> reviews_pending`：商品成功且存在可分页的全量评论入口（href 路径含 `/product-reviews/` 或 `/portal/customer-reviews/`）。商品页 `#averageCustomerReviewsAnchor` 等 fragment 只记录为 `review_section_anchor`，不可作为评论 URL。
- `running -> product_done -> succeeded`：没有可分页评论入口；`review_summary.status` 为 `not_available` 或 `section_only`，`fetched_count=0`，不宣称已采集全评论。
- `reviews_pending -> running -> reviews_pending`：评论页仍有下一页或达到分页 limit。
- `running -> blocked`：403/明确阻断页，终态，不重试。
- `running -> pending/reviews_pending`：429 限流延迟，保留 `last_error=block_reason=http_429` 与当前游标，不标记永久 blocked；下一小时用同一会话类型重试。
- 非阻断错误进入 `failed`，只在 `attempts < max_attempts` 时再次领取；attempts 是当前 stage 的连续失败次数，成功 action 后重置为 0。

`next_review_page` 与 `next_review_url` 是精确续跑游标。产品 action 和评论 page action 分离；已有评论游标不会重抓产品。`--once` 表示本次批次运行后退出，批次默认最多 `max_actions_per_run=10` 个 action；通常每个 action 一次 HTTP 请求，HTTP 页面字段不足时可能追加一次 Firefox 请求。`--limit` 可覆盖本次 action 数上限。

## 运行、恢复与物化

项目依赖固定在 `requirements.txt`。ZCode AppImage 会向子进程传入 `APPIMAGE`，创建和运行虚拟环境时应清除此变量，避免解释器被错误链接到 AppImage：

```bash
env -u APPIMAGE -u __PYVENV_LAUNCHER__ /usr/bin/python3.14 -m venv .venv
env -u APPIMAGE .venv/bin/python -m pip install -r requirements.txt
```

先做离线初始化：

```bash
python3 scripts/amazon_us_worker.py --dry-run
```

预期显示 `phase=initialized`、1,892 条 manifest；会创建/更新 SQLite 和六个 UTF-8 BOM CSV headers，不访问网络。

只物化现有 SQLite：

```bash
python3 scripts/amazon_us_worker.py --materialize-only
```

live 运行需另行获得网络采集授权，并在依赖已准备后运行：

```bash
env -u APPIMAGE -u __PYVENV_LAUNCHER__ .venv/bin/python scripts/amazon_us_worker.py --live --once --limit 10
```

Windows 每小时任务应调用 `run_scheduled_windows.bat`：它会先运行 preflight，再按字段新鲜度生成刷新队列，随后执行 worker、物化 CSV 和验收。`install_hourly_task_windows.bat` 已指向该脚本；`run_once_windows.bat` 仍用于人工单批运行。

`--once` 是一批 action，不是一条商品或一页评论。遇 403/CAPTCHA 阻断返回非零并保留 checkpoint，不再选择；遇 429 返回非零但保留 pending/reviews_pending 游标，下一小时使用同一会话类型重试；非阻断 failed 在达到 `max_attempts` 前继续。

若配置 `user_agent`，必须包含透明标识 `Agent/<agent_name>`；默认配置已使用带 `Agent/amazon-us-worker` 的 Firefox UA，不覆盖为隐藏身份。不存在 Selenium 时 live 模式仅报告清晰依赖错误，不会发出网络请求。

## 验收

```bash
python3 scripts/amazon_us_verify.py --once
python3 scripts/verify_agent_review.py
```

验证脚本只读 manifest、SQLite 和 CSV，写入 `data/amazon_us/verification/latest_verification.json`。检查包括：悬挂 running、状态/history 一致、分页连续性与末页终态、exhausted failed、六表 schema、唯一键、summary/state 一致、阻断原因。空初始化会报告 `phase=initialized`、`collection_phase=not_collected`，不会被摘要称为采集完成。验收摘要不输出评论正文、Cookie、Token 或其他秘密。

解析器修复不会自动重置已有数据。现有 `B00RCPDCQU` 若已保存 fragment 作为 `next_review_url`，必须由运维显式 reset/requeue 后再重新执行商品 action；不得通过代码自动回队或覆盖 live 状态。
