# Amazon US 数据契约

## 主键与编码

- manifest 主键：`marketplace + asin`，当前固定 `marketplace=US`。
- 商品快照主键：`marketplace + asin`。
- 媒体主键：稳定 `unique_key`，由 `marketplace + asin + placement + entry_type + asset_url` 组成。
- 内容模块主键：稳定 `unique_key`，由 `marketplace + asin + module_type + position` 组成。
- 评论主键：`marketplace + asin + review_id`，CSV 同时输出 `unique_key`。
- 证据为历史表，记录 `run_id + marketplace + asin + url + retrieved_at + content_hash`。
- 所有 CSV 使用 UTF-8 BOM、稳定 headers、Unix LF；run 结束统一采用临时文件替换。

## 状态契约

`item_state.status` 只允许：`pending`、`running`、`product_done`、`reviews_pending`、`succeeded`、`blocked`、`failed`。状态转换由 worker 的合法转移表校验，验证器同时核对 `state_history` 和当前状态。

`resume_status`、`task_stage`、`next_review_page`、`next_review_url` 是恢复字段。启动会把残留 `running` 恢复到 `reviews_pending` 或 `pending`。

- `blocked`：403 或明确访问阻断页的终态；不自动重试。
- `failed`：非阻断错误；`attempts` 是当前 stage 连续失败次数，成功 product/review action 重置为 0，仅在 `< max_attempts` 时重试；达到上限的 failed 会在验收中列为 `exhausted_failed`。
- HTTP `429` 不进入永久 `blocked`：写入 `http_429` evidence，保留 product/review 游标，转回 `pending` 或 `reviews_pending`，下一小时使用同一会话类型重试。

## 输出表

### product_snapshot.csv

包含 `canonical_url`、`availability`、`title`、`brand`、`rating`、`reported_rating_count`、`reported_review_count`、`review_count`、`review_count_source`、`price`、JSON bullets/specs/BuyBox/Top reviews、可分页 review link、可选 `review_section_anchor`、A+ marker 和状态。`brand` 去除 Amazon 的 `Visit the <brand> Store` 展示包装，但不改写品牌本身。`price` 保留页面显示币种并归一化重复视觉节点，不做汇率转换。Buy Box JSON 保留 `text`，并按页面显示情况提供 `seller`、`coupon`、`delivery`；缺失字段保持为空，不推断优惠。`review_section_anchor` 仅记录商品页锚点，绝不作为 `next_review_url`。

### media_asset.csv

固定字段：`asin, marketplace, placement, entry_type, thumbnail_url, display_url, asset_url, poster_url, ordinal, is_primary, width, height, alt_text, variant_asin, load_status, failure_reason, unique_key`。

只允许商品自身 `gallery`、`product_videos`、`aplus`、`from_brand`、`review` 区域，排除导航、推荐、广告和 tracking。入口存在与 `asset_url` 是否可得分别记录；不会下载媒体二进制。

### content_module.csv

固定字段：`module_type`、`position`、`order_index`、`text`、`image_url`、`link_url`、`status`。模块类型包括逐条 `bullet`、`product_description`、`product_information`、`aplus`、`from_brand`；规格支持 table `tr` 和常见 li/div label-value 结构。

运营字段落点约定：页面显示的 BSR、类目节点、型号和其他商品信息进入 `specs_json`；卖家、Coupon 和配送提示进入 `buy_box_json`（同时保留 `text` 原文）。页面未显示、容器为空或证据不可检查时保持空值，不根据价格、品牌或推荐商品推断。

### review_summary.csv

同时保留 `reported_rating_count` 与 `reported_review_count`，并以 `reported_count_source` 记录页面来源；页面只有一个 count 时不会伪造另一个字段。只有 review section anchor 或无法分页时，summary 使用 `section_only`/`not_available`，`fetched_count=0`，不会进入 `reviews_pending`。`fetched_count` 是 review ID 去重后的本地数量，`pages_fetched`、`next_page` 和 `status` 表示分页断点；末页状态为 `exhausted`。

### review_record.csv

包含 `review_date`、`locale`、`verified`、`body_truncated`、`review_images_json`、页码和稳定 review key。评论正文只在已授权 live 采集时由公开页面写入；验收摘要不会输出正文。

### collection_evidence.csv

每次 page action 一条历史 evidence，保留 `run_id`、URL、可获取的 response status、抓取时间、内容哈希、阻断原因、解析器版本、错误码和 `context_json`。`context_json` 至少记录 `postal_code`、`expected_country`、`expected_currency`；Selenium 在导航后读取 `performance.getEntriesByType('navigation')[-1].responseStatus`；Firefox 不支持时保留空值，但仍执行页面阻断结构检测，不发第二次请求。

## 合规边界

默认 dry-run/materialize/verify 不访问网络。live 使用独立临时 Firefox profile，不读取 Chrome Profile、Cookie、Token，不自动登录，不代理、不随机 IP、不伪装身份，不绕过 CAPTCHA/403/429。当前输入来自 FBA 补货建议范围，不是 Listing 全量，不能据此声称 Listing 全量覆盖。
