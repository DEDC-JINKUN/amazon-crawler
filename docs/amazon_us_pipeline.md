# Amazon US 本地采集流水线

## 范围与合规硬闸门

当前输入是 `amazon_us_asin_manifest.csv`，来自领星 FBA 补货建议导出，不是 Listing 全量。US manifest 当前为 1,892 个 unique `(marketplace, asin)`，仅作为首轮一次性补全的输入范围。

默认命令只做离线初始化或 CSV 物化，不访问 Amazon。live 模式必须显式指定 `--live`，先使用 HTTP 获取公开 HTML；只有页面字段不足时才懒加载 Selenium Firefox 的独立临时 profile、默认 headless、可配置 `geckodriver_path`。不会读取 Chrome Profile、自动登录、Cookie、Token 或其他凭据。当前 POC 不启用代理池；媒体只保存公开 URL 和 DOM 元数据，不下载媒体文件。

生产采集配置应填写 `[context]` 的 `expected_country=US`、`expected_currency=USD` 和业务 ZIP。页面出现明显非美国币种或配送地区时，质量门禁会拒绝写入快照并记录 `context_mismatch`。

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

Windows 运行前先执行 `powershell -ExecutionPolicy Bypass -File scripts\install_geckodriver.ps1` 和 `python scripts\preflight.py --require-live`。固定驱动版本后再执行 live；真实探针记录见 `docs/live_probe_report.md`。

如果配置付费代理，`preflight` 会校验 `proxy_url` 必须是明确的 HTTP(S) 出口，并拒绝把用户名/密码写进 URL；需要认证时只填写 `proxy_username_env` / `proxy_password_env` 两个环境变量名，worker 在运行时读取其值。当前 POC 的 `proxy_url` 为空，表示直连。

HTTP 和 Firefox 兜底共用同一 `proxy_url`。Firefox 可传递无内嵌凭证的 HTTP(S) 代理；代理认证是否被目标供应商和浏览器环境接受，必须在拿到批准出口后做真实测试。

当 HTTP 返回的页面与 `[context]` 不一致（例如出现 HKD 或香港配送），worker 会把该结果作为 evidence 保留，不写入商品快照；随后尝试在隔离 Firefox 会话中设置 `postal_code`，重新加载商品页并再次进行区域门禁。若仍不匹配，则按 `context_mismatch` 失败处理。

Windows 每小时任务应调用 `run_scheduled_windows.bat`：它会先运行 `preflight --require-live`（包括美国 ZIP 检查），再按字段新鲜度生成刷新队列，随后执行 worker、物化 CSV 和验收。`install_hourly_task_windows.bat` 已指向该脚本；`run_once_windows.bat` 仍用于人工单批运行。

首轮区域 live 验证使用 `config/amazon_us.test_west.toml`（美西 `90001`、最多 10 个 action），不修改默认配置；通过后再复制同样流程验证 `60601` 和 `10001`。

当前实测：可见 Firefox 的单 ASIN 能确认 `90001`，但 10 条跨 ASIN 批次仍有区域不匹配；在该问题解决并复测通过前，不得扩大到 30 条或 1,892 条。HTTP chunked 断片会进入可重试失败并保留任务断点。

美国 VPN 下使用全新空状态库采集 `B00RCPDCQU` 已通过商品页门禁：HTTP 200、美元 `$23.99`、`product_done`，媒体 34、内容模块 23、评论摘要 1、证据 1；评论页为空单独标记 `empty_review_page`。该输出位于 `data/amazon_us/quick_clean_vpn`，仅为测试副本。

美国 VPN/90001 的全新 SQLite 10 条小批量已完成：9 条商品页成功、1 条 `asin_mismatch` 拦截；bullets 覆盖率 55.56%，描述覆盖率 44.44%。这批结果支持继续修解析器，但不支持直接扩大到全量 ASIN。

遇到 CAPTCHA、Robot Check 或明确访问拒绝时，worker 立即停止本次批次；当前任务写入 `blocked`，未领取任务保持 `pending`，不会继续换页、换账号或无限更换出口。

人工确认出口/会话恢复后，使用 `scripts/requeue_tasks.py --db <state> --asin <ASIN> --reason <说明>` 重入队；`blocked` 任务必须额外加 `--include-blocked`。工具只重置任务状态并写入历史，不删除 evidence 或旧快照。

可用 `coverage_report.py --raw-html-dir <raw_html 根目录>` 对成功页面做字段容器审计：`present` 表示原始容器有内容，`empty` 表示页面明确没有内容，`uninspectable` 表示证据文件缺失。只有 `present` 但结构化字段为空时，才应作为解析器缺陷处理。

解析器对 `productDescription` 使用原始 HTML 容器提取，避免 Amazon malformed/重复 `div` 导致后续模块文本被误归入 description；空容器会保持空值，不伪造商品描述。

HTTP 传输层默认 `http_max_attempts=2`、`http_retry_backoff_seconds=0.5`，仅对连接中断、`IncompleteRead` 和超时等传输异常重试；HTTP 403/429/挑战页仍由原阻断流程处理。实测 10 条耗时 32.99 秒，9 条商品页成功，未出现 `IncompleteRead`。

评论页若使用 `/portal/customer-reviews/` 且返回空记录，worker 会在标记 `empty_review_page` 前尝试 `/product-reviews/<ASIN>`；备用入口仍为空时才按空页失败，并保留两个入口的 evidence。

评论记录以 `marketplace + asin + review_id` 为主键：重复抓取不会新增重复记录，Amazon 编辑同一评论时会更新已有记录；分页游标仍按 `next_review_url/next_review_page` 续跑。

`--once` 是一批 action，不是一条商品或一页评论。遇 403/CAPTCHA 阻断返回非零并保留 checkpoint，不再选择；遇 429 返回非零但保留 pending/reviews_pending 游标，下一小时使用同一会话类型重试；非阻断 failed 在达到 `max_attempts` 前继续。

若配置 `user_agent`，必须包含透明标识 `Agent/<agent_name>`；默认配置已使用带 `Agent/amazon-us-worker` 的 Firefox UA，不覆盖为隐藏身份。不存在 Selenium 时 live 模式仅报告清晰依赖错误，不会发出网络请求。

## 验收

```bash
python3 scripts/amazon_us_verify.py --once
python3 scripts/verify_agent_review.py
```

验证脚本只读 manifest、SQLite 和 CSV，写入 `data/amazon_us/verification/latest_verification.json`。检查包括：悬挂 running、状态/history 一致、分页连续性与末页终态、exhausted failed、六表 schema、唯一键、summary/state 一致、阻断原因。空初始化会报告 `phase=initialized`、`collection_phase=not_collected`，不会被摘要称为采集完成。验收摘要不输出评论正文、Cookie、Token 或其他秘密。

验收测试副本时必须显式指定 `--manifest`、`--state`、`--output-dir` 和 `--verification`；`ok=true` 表示结构和一致性检查通过，`phase=collecting` 表示仍有 pending/进行中任务，不能解读为全量完成。

解析器修复不会自动重置已有数据。现有 `B00RCPDCQU` 若已保存 fragment 作为 `next_review_url`，必须由运维显式 reset/requeue 后再重新执行商品 action；不得通过代码自动回队或覆盖 live 状态。
