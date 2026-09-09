# -*- coding: utf-8 -*-
"""现有库迁移：canary 预检 + variant 变体跳转标记"""
import psycopg

DSN = "postgresql://postgres:123456@localhost:5432/amazon_us"
conn = psycopg.connect(DSN, autocommit=True)
# 1. 成员表加 canary 标记列
conn.execute("ALTER TABLE amazon_us.batch_item ADD COLUMN IF NOT EXISTS is_canary boolean NOT NULL DEFAULT false")
# 2. 批次状态约束放开 canary（先删旧约束再加新的，幂等执行）
conn.execute("ALTER TABLE amazon_us.batch DROP CONSTRAINT IF EXISTS batch_status_check")
conn.execute("""
ALTER TABLE amazon_us.batch ADD CONSTRAINT batch_status_check
    CHECK (status IN ('pending','provisioning','canary','running','stopping',
                      'completed','blocked','failed','stopped'))
""")
# 3. 活跃批次部分索引纳入 canary（重复上传判断靠它覆盖 canary 批次）
conn.execute("DROP INDEX IF EXISTS amazon_us.idx_batch_tenant_active")
conn.execute("""
CREATE INDEX idx_batch_tenant_active
    ON amazon_us.batch (tenant_id, marketplace, manifest_hash)
    WHERE status IN ('pending','provisioning','canary','running','stopping')
""")
# 4. 变体跳转：成员表加 variant 状态 + variant_asin 列
conn.execute("ALTER TABLE amazon_us.batch_item ADD COLUMN IF NOT EXISTS variant_asin varchar(10)")
conn.execute("ALTER TABLE amazon_us.batch_item DROP CONSTRAINT IF EXISTS batch_item_status_check")
conn.execute("""
ALTER TABLE amazon_us.batch_item ADD CONSTRAINT batch_item_status_check
    CHECK (status IN ('pending','running','succeeded','blocked',
                      'failed','cancelled','variant'))
""")
# 5. 进度视图加 variant 计数（CREATE OR REPLACE 不允许变列位置，先删再建）
conn.execute("DROP VIEW IF EXISTS amazon_us.batch_progress")
conn.execute("""
CREATE VIEW amazon_us.batch_progress AS
SELECT
    b.batch_id,
    b.tenant_id,
    b.marketplace,
    b.status AS batch_status,
    b.stop_requested,
    b.requested_count,
    b.final_failure_class,
    b.created_at,
    b.started_at,
    b.finalized_at,
    COUNT(i.asin) FILTER (WHERE i.status = 'succeeded')          AS succeeded,
    COUNT(i.asin) FILTER (WHERE i.status = 'running')            AS running,
    COUNT(i.asin) FILTER (WHERE i.status = 'pending')            AS pending,
    COUNT(i.asin) FILTER (WHERE i.status = 'blocked')            AS blocked,
    COUNT(i.asin) FILTER (WHERE i.status = 'failed'
                          AND i.error_class = 'fetch')            AS failed_fetch,
    COUNT(i.asin) FILTER (WHERE i.status = 'failed'
                          AND i.error_class = 'system')           AS failed_system,
    COUNT(i.asin) FILTER (WHERE i.status = 'failed')             AS failed_total,
    COUNT(i.asin) FILTER (WHERE i.status = 'cancelled')          AS cancelled,
    COUNT(i.asin) FILTER (WHERE i.status = 'variant')            AS variant,
    (SELECT COUNT(*) FROM amazon_us.collection_evidence e
      WHERE e.batch_id = b.batch_id)                             AS evidence_count,
    (SELECT COALESCE(SUM(e.transfer_bytes), 0)
       FROM amazon_us.collection_evidence e
      WHERE e.batch_id = b.batch_id)                             AS evidence_bytes
FROM amazon_us.batch b
LEFT JOIN amazon_us.batch_item i ON i.batch_id = b.batch_id
GROUP BY b.batch_id
""")
# 6. BSR 类目销售排名：商品快照表加列
conn.execute("ALTER TABLE amazon_us.product_snapshot ADD COLUMN IF NOT EXISTS bsr_rank integer")
conn.execute("ALTER TABLE amazon_us.product_snapshot ADD COLUMN IF NOT EXISTS bsr_category text")
conn.execute("ALTER TABLE amazon_us.product_snapshot ADD COLUMN IF NOT EXISTS bsr_entries jsonb NOT NULL DEFAULT '[]'::jsonb")
# 7. 重建 product_latest 视图：PostgreSQL 的 SELECT * 视图在创建时展开列清单，
#    基表后加的列（bsr_*）不会自动出现在旧视图里，必须重建视图才能透传
conn.execute("DROP VIEW IF EXISTS amazon_us.product_latest")
conn.execute("""
CREATE VIEW amazon_us.product_latest AS
SELECT DISTINCT ON (tenant_id, marketplace, asin, subject_type) *
FROM amazon_us.product_snapshot
ORDER BY tenant_id, marketplace, asin, subject_type, collected_at DESC, snapshot_id DESC
""")
# 8. 评论采集开关：批次表加列（默认开，false 时商品完成即收尾不采评论）
conn.execute("ALTER TABLE amazon_us.batch ADD COLUMN IF NOT EXISTS collect_reviews boolean NOT NULL DEFAULT true")
# 验证
cols = [r[0] for r in conn.execute(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema='amazon_us' AND table_name='batch_item' AND column_name='is_canary'"
)]
print("is_canary 列:", "已添加" if cols else "失败")
test = conn.execute(
    "SELECT COUNT(*) FROM amazon_us.batch WHERE status='canary'"
).fetchone()[0]
print("约束更新完成，现有 canary 批次数:", test)
vcols = [r[0] for r in conn.execute(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema='amazon_us' AND table_name='batch_item' AND column_name='variant_asin'"
)]
print("variant_asin 列:", "已添加" if vcols else "失败")
bcols = [r[0] for r in conn.execute(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema='amazon_us' AND table_name='product_snapshot' AND column_name LIKE 'bsr_%'"
)]
print("BSR 列:", bcols)
vview = [r[0] for r in conn.execute(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema='amazon_us' AND table_name='product_latest' AND column_name='bsr_rank'"
)]
print("product_latest 视图透传 bsr_rank:", "是" if vview else "否")
conn.close()
