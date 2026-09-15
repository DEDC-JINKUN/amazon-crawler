# 采集效率、产品分层与评论实验通道

## 产品分层

外部销量与Amazon自采价格保持来源分离。销量CSV写入`external_metric_batch`、`external_metrics`和显式竞品关系表，不写入或改写`product_snapshot`。

CSV字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `asin` | 是 | 自有产品ASIN |
| `sales_units` | 是 | 非负销量数字 |
| `observed_at` | 是 | 带时区ISO时间 |
| `competitor_asins` | 否 | `;`或`,`分隔的竞品ASIN，继承该自有产品层级 |

```powershell
.venv\Scripts\python.exe scripts\external_metrics.py `
  --tenant-id owned_us_asin_20260902_full_01 `
  import-csv --file sales.csv --batch-id sales-20260915 --source mcp-sales

.venv\Scripts\python.exe scripts\external_metrics.py `
  --tenant-id owned_us_asin_20260902_full_01 `
  apply-tiers --revenue-threshold 2000
```

同一tenant的`batch_id`绑定来源、文件SHA256和行数；完全相同的重导返回`duplicate_batch=true`且不增加指标，内容不同则`ExternalMetricBatchConflict`。分层读取每个ASIN最新销量和最新自采USD价格：`sales_units × price >= 2000`标记`own_core/priority=100`，否则标记`own_normal/priority=50`；显式映射的竞品相应继承`competitor_core`或`competitor_normal`。

缺销量、缺价格或非USD价格一律`skipped`，不猜测层级。

## 时效与采集面

- 核心商品PDP：`own_core`每4小时、`competitor_core`每6小时。
- 非核心商品PDP：`own_normal`和`competitor_normal`统一每168小时。
- 普通/候选任务只走HTTP；transport、访问阻断、字段或上下文缺口不启动Firefox。
- Worker启动门禁只要求每个进程至少能处理1条；实际批次在1–3条间按待处理数和原子容量动态缩小。
- Firefox只留给显式核心产品的一次字段/身份补全。
- 标题、品牌等静态字段随PDP请求顺带更新，不为它们制造额外请求；价格、库存和buy box由核心PDP cadence决定。
- 评论是独立采集面，不因PDP动态字段到期而重抓历史评论；后续评论增量以来源和稳定键去重。

## Woot评论实验

`scripts/woot_review_adapter.py`是未接入生产队列的实验适配器，只处理Amazon US书面评论：

- 请求`woot.com/review/Reviews/{ASIN}`，保留`Agent/<name>`透明UA；
- HTTP 401/403/429、非JSON、坏JSON或缺`Reviews`数组全部失败；
- 不把上游失败或HTML响应记为“0条评论成功”；
- Woot无稳定`review_id`时，以ASIN、作者、完整标题、完整正文、评分和来源日期计算SHA256复合键；
- 输出`source_type=woot_ajax`，数据库迁移为`review_record`增加来源字段；
- 在固定样本门禁通过前不得接入生产Worker。

上游项目公开问题显示Woot端点可能返回403并被原脚本误报0条，且API不提供review_id。因此MIT代码可作为实现参考，不构成端点稳定性或数据使用授权证明：

- <https://github.com/mrlong0129/amazon-review-scraper/issues/2>
- <https://github.com/mrlong0129/amazon-review-scraper/issues/3>

## 当前效率门禁

- 固定20-ASIN可信成功率≥90%；
- Amazon access-block≤3%；
- browser fallback≤10%；
- 持续可信终态≥100/小时；
- 1093成员campaign汇总查询p95≤2秒；
- 任一指标不达标即保持fail closed，不通过增加重试、自动换出口或验证码处理绕过。
