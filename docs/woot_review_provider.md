# Woot Amazon US 评论 Provider

## 定位

`scripts/woot_review_provider.py` 是独立的 Amazon US 文字评论采集 Provider。它调用 Woot JSON，不调用 Amazon PDP/评论页，不启动 Selenium，也不使用现有 PDP Worker 的代理、租约或恢复状态。

该 Provider 的目标是最大化采集并提供覆盖证据。它不会把“20个查询组合均已结束”自动表述成“Amazon 评论全量”。只有调用方提供可追溯的文字评论总数，且唯一评论数达到该分母时，收据才允许 `coverage.status=complete`。

## 安全默认值

不带 `--execute` 时只输出计划，不访问网络、不写数据库：

```powershell
python scripts/woot_review_provider.py --asin B0MASK0001
```

显式执行示例：

```powershell
python scripts/woot_review_provider.py `
  --asin B0MASK0001 `
  --mode max `
  --expected-written-reviews 120 `
  --expected-count-source amazon_written_review_count `
  --output-root outputs/woot-reviews `
  --execute
```

`--expected-written-reviews` 必须是文字评论数量，不能把 Amazon 的 ratings 总数当作该分母。没有可信分母时，Provider 可以交付评论数据，但不会宣称 100% 覆盖。

## 采集策略

- `adaptive`：先运行无星级筛选的Top查询；结果低于100且正常结束时直接交付，触顶或出现query级404时自动升级到完整max矩阵。批量采集推荐使用该模式。
- `basic`：无星级筛选、Top 排序，一个组合。
- `full`：1–5星分别采集，五个组合。
- `max`：1–5星 × 4种排序，共20个组合，默认模式。
- 每个组合跟随 `PagingNext`，直到游标结束、重复或请求预算耗尽。
- 优先使用上游 review ID；上游没有 ID 时，基于 US、ASIN、作者、日期、星级、标题和完整正文生成 SHA-256 降级身份。
- 重复评论合并查询来源、图片和视频，不用“正文前80/100字符”去重。

## Fail-closed 规则

以下情况写失败收据并返回非零，不得解释为“0条评论”：

- HTTP 403、429或其他非2xx；
- 网络异常；
- 非 UTF-8 JSON；
- 缺少 `Reviews` 数组或游标类型异常；
- 空页仍返回下一游标；
- `PagingNext` 循环；
- 已知文字评论数大于0，但所有查询返回0条。

Woot 的HTTP404是查询级限制：首请求404记录为`query_unavailable`，成功分页后404记录为`query_truncated`；两者保留原始HTML、继续其他组合并把覆盖降为`source_limited`。403/429仍是Provider访问阻断并停止批次。

响应体先写入不可变 evidence 文件，再解析 HTTP 状态和 JSON。响应头仅保存 Content-Type、Date、ETag、Last-Modified 和 Retry-After，不保存 Cookie。UA可通过`--user-agent`配置Firefox版本等普通标识，但必须保留`Agent/<name>`透明标记；缺少该标记时在发请求前拒绝。

## 覆盖状态

| 状态 | 含义 |
|---|---|
| `complete` | 有可追溯文字评论分母，唯一评论数达到分母，且未耗尽请求预算 |
| `no_reviews_verified` | 可追溯文字评论分母明确为0，且结果为0 |
| `source_limited` | 请求预算耗尽，或至少一个查询触及已知100条结果上限 |
| `partial` | 数据可用，但没有证据证明完整 |
| `blocked` | 执行失败；具体错误在 `receipt.failed.json` |

只有 `complete` 和 `no_reviews_verified` 的 `claim_100_percent` 为 true。

## 运行产物

每次执行创建一个不可覆盖的运行目录：

```text
outputs/woot-reviews/<run_id>/
  plan.json
  evidence_manifest.json
  evidence/
    qNN_star-X_sort-Y_pNNN.body
    qNN_star-X_sort-Y_pNNN.json
  reviews.normalized.json
  receipt.json | receipt.failed.json
```

`reviews.normalized.json` 保留 Author、Verified Purchase、Vine、Helpful、图片、视频、原始日期描述、provider ID或降级ID，以及每条评论出现过的查询/页来源。

## 与现有系统的边界

- 不写 `product_snapshot`，不改变 PDP truth。
- 不领取或修改现有 `item_state.task_stage=reviews`；Woot 与 Amazon 评论页状态必须隔离。
- 当前版本不写 PostgreSQL。下一阶段应增加独立 review job/checkpoint/migration，再通过适配层消费规范化输出。
- Woot 失败不得阻塞同 ASIN 的价格、库存、Listing 或西柚采集。
- 原始 provider JSON、provider/schema版本和覆盖收据必须进入后续 RAG 的 provenance，不得按上游 Skill 的“隐藏数据源”规则处理。

## XLSX批量与聚合

批量入口支持全局限速、并发、熔断、progress JSONL、成功项跳过和续跑：

```powershell
python scripts/woot_review_batch.py `
  --workbook <ASIN清单.xlsx> `
  --mode adaptive `
  --concurrency 4 `
  --min-request-interval-seconds 1 `
  --skip-success-root outputs/woot-results `
  --output-root outputs/woot-results/runs `
  --execute
```

批次完成后用聚合器验证每个成功收据和规范化文件哈希，并输出合并评论、ASIN状态、待处理清单与manifest：

```powershell
python scripts/woot_review_aggregate.py `
  --workbook <ASIN清单.xlsx> `
  --results-root outputs/woot-results `
  --output-dir outputs/woot-results/aggregate
```
