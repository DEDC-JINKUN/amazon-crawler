-- =====================================================================
-- 批次模型迁移脚本（在 postgres_schema.sql 之后执行）
-- 解决的问题：
--   1. 批次与历史数据没分清：进度只认本批次成员和本批次证据
--   2. 状态来源不统一：批次终态由协调器写入，页面只读 batch_progress 视图
--   3. 双 Worker 协调：容量改为数据库槽位租约，一槽一行天然防超卖
--   4. 停止/崩溃恢复：租约带过期时间，协调器启动时统一回收
--
-- 新增对象：
--   batch             批次表（一次上传 = 一行）
--   batch_item        批次成员表（batch_id + asin = 一个任务，任务租约在这）
--   egress_endpoint   代理出口定义（总槽位数）
--   egress_slot       容量槽租约（一行 = 一个槽，租给谁/何时过期）
--   coordinator_state 协调器心跳表（可观测，单例靠咨询锁保证）
--   batch_progress    视图：批次进度的唯一推导处，页面只读它
-- 修改对象：
--   collection_evidence / collection_run 增加 batch_id 列
--
-- 幂等：可重复执行。
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS amazon_us;
SET search_path TO amazon_us, public;

-- ---------------------------------------------------------------------
-- 1. 批次表：一次上传 = 一行
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS amazon_us.batch (
    batch_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id text NOT NULL DEFAULT 'default',
    marketplace text NOT NULL DEFAULT 'US' CHECK (marketplace = 'US'),
    -- 上传清单规范化后的哈希：完全相同的清单视为重复上传（应用层判断，
    -- 活跃批次中重复则拒绝；已结束的批次允许重跑，所以这里不做唯一约束）
    manifest_hash text NOT NULL,
    requested_count integer NOT NULL CHECK (requested_count > 0),
    -- pending        已创建，未开始
    -- provisioning   代理探测 / 容量预约中
    -- canary         预检阶段：只跑探针成员（前 2 个），通过才放开全量
    -- running        进行中
    -- stopping       已收到停止请求，等待 worker 收尾
    -- completed      全部成员成功
    -- blocked        剩余失败全部是"被拦"（验证码/机器人）
    -- failed         存在"系统"或"网络"类失败
    -- stopped        被用户停止，部分完成
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','provisioning','canary','running','stopping',
                          'completed','blocked','failed','stopped')),
    -- 用户请求停止的标记，worker 心跳时读到即优雅退出
    stop_requested boolean NOT NULL DEFAULT false,
    -- 评论采集开关：false 时商品完成后直接收尾，不进评论阶段
    -- （评论页限制多、拖慢批次，只要商品数据时关掉）
    collect_reviews boolean NOT NULL DEFAULT true,
    -- 协调器收尾时写入的终态主导失败类别：blocked / fetch / system
    final_failure_class text
        CHECK (final_failure_class IS NULL
               OR final_failure_class IN ('blocked','fetch','system')),
    uploaded_by text NOT NULL DEFAULT 'console',
    -- Agent 提交批次的显式幂等键：同租户同键在窗口期内返回同一批次，
    -- 防止超时重试重复建批次（未传键的正常上传不受影响）
    idempotency_key text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finalized_at timestamptz,           -- 协调器写终态的时间
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 幂等键查询索引（只索引带键的行）
CREATE INDEX IF NOT EXISTS idx_batch_tenant_idempotency
    ON amazon_us.batch (tenant_id, idempotency_key, created_at DESC)
    WHERE idempotency_key IS NOT NULL;

-- 查租户批次列表 / 判断重复上传
CREATE INDEX IF NOT EXISTS idx_batch_tenant_created
    ON amazon_us.batch (tenant_id, marketplace, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_batch_tenant_active
    ON amazon_us.batch (tenant_id, marketplace, manifest_hash)
    WHERE status IN ('pending','provisioning','canary','running','stopping');

-- ---------------------------------------------------------------------
-- 2. 批次成员表：batch_id + asin = 一个任务
--    任务状态机比旧 item_state 收敛为 6 态：
--    pending → running → succeeded / blocked / failed
--              （failed 可重试回 pending 语义，见领取索引）
--    用户停止时残余成员 → cancelled
--    评论续采用 task_stage 区分，不再有 reviews_pending 中间态
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS amazon_us.batch_item (
    batch_id uuid NOT NULL
        REFERENCES amazon_us.batch(batch_id) ON DELETE CASCADE,
    tenant_id text NOT NULL DEFAULT 'default',
    marketplace text NOT NULL DEFAULT 'US' CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL CHECK (asin ~ '^[A-Z0-9]{10}$'),
    url text NOT NULL,
    -- pending    待领取（含评论续采：task_stage='reviews'）
    -- running    某个 worker 持有租约执行中
    -- succeeded  本批次内采集完成（写入时事务内必存证据）
    -- blocked    被拦终态（验证码/机器人检查，等冷却换代理）
    -- failed     失败终态（可重试：attempts < max_attempts 时可被重新领取）
    -- cancelled  用户停止批次时未完成的成员
    -- variant    变体跳转终态：页面有效但返回的是另一个 ASIN（通常是变体），
    --             不是失败（重试结果相同）也不是原商品成功（数据不属于清单 ASIN）
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','succeeded','blocked',
                          'failed','cancelled','variant')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    task_stage text NOT NULL DEFAULT 'product'
        CHECK (task_stage IN ('product','reviews')),
    -- canary 探针成员：批次处于 canary 状态时只有这些成员可被领取，
    -- 探针全部成功后协调器放开全量（批次转 running）
    is_canary boolean NOT NULL DEFAULT false,
    -- running 之前的状态，租约过期回收时回退用
    resume_status text,
    next_retry_at timestamptz,
    -- 失败三分类：blocked=被拦 / fetch=网络代理 / system=解析代码数据库
    error_class text
        CHECK (error_class IS NULL OR error_class IN ('blocked','fetch','system')),
    block_reason text,
    last_error text,
    -- 变体跳转时实际页面解析出的 ASIN（status='variant' 时有值）
    variant_asin varchar(10),
    -- 评论续采断点（task_stage='reviews' 时使用）
    next_review_url text,
    next_review_page integer,
    -- 评论续采计数（成员自己的计数，新批次从零开始，批次语义正确）
    review_page_limit integer,
    reported_rating_count integer,
    reported_review_count integer,
    reported_count_source text,
    fetched_review_count integer NOT NULL DEFAULT 0,
    review_pages_fetched integer NOT NULL DEFAULT 0,
    -- 任务租约三件套（沿用原 item_state 的租约设计）
    lease_token text,
    lease_owner text,
    lease_expires_at timestamptz,
    finalized_at timestamptz,           -- 成员进入终态的时间
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (batch_id, asin)
);

-- 领取扫描：pending 成员 + 可重试的 failed 成员
CREATE INDEX IF NOT EXISTS idx_batch_item_claim
    ON amazon_us.batch_item (batch_id, status, updated_at)
    WHERE status IN ('pending','failed');
-- 租约过期回收扫描
CREATE INDEX IF NOT EXISTS idx_batch_item_lease_expiry
    ON amazon_us.batch_item (lease_expires_at)
    WHERE lease_expires_at IS NOT NULL;
-- 批次收尾时统计成员状态
CREATE INDEX IF NOT EXISTS idx_batch_item_status
    ON amazon_us.batch_item (batch_id, status);

-- ---------------------------------------------------------------------
-- 3. 代理出口容量 + 槽位租约
--    一个槽一行：容量永不超卖由行数物理保证；
--    双 worker 分别租槽，先到先得但不会"占满导致对方启动失败"——
--    对方租不到槽就排队等待（或按批次配额），由协调器仲裁。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS amazon_us.egress_endpoint (
    egress_id text PRIMARY KEY,             -- 出口标识，如 'proxy-01'
    endpoint_url text,                      -- 代理地址（可为空=直连）
    description text,
    total_slots integer NOT NULL CHECK (total_slots > 0),
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS amazon_us.egress_slot (
    egress_id text NOT NULL
        REFERENCES amazon_us.egress_endpoint(egress_id) ON DELETE CASCADE,
    slot_index integer NOT NULL CHECK (slot_index >= 0),
    -- 当前租约信息；lease_expires_at 为空 = 空闲
    worker_id text,
    batch_id uuid,
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    PRIMARY KEY (egress_id, slot_index)
);

-- 过期槽回收扫描
CREATE INDEX IF NOT EXISTS idx_egress_slot_expiry
    ON amazon_us.egress_slot (lease_expires_at)
    WHERE lease_expires_at IS NOT NULL;

-- ---------------------------------------------------------------------
-- 4. 协调器心跳表（可观测用；真正单例靠 pg 咨询锁，不靠这张表）
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS amazon_us.coordinator_state (
    coordinator_id uuid PRIMARY KEY,
    host text NOT NULL,
    pid integer NOT NULL,
    -- 咨询锁 key，固定值（如 tenant 哈希），调试用
    advisory_lock_key bigint NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    -- running / stopped / crashed（由下一次启动的协调器回填 crashed）
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','stopped','crashed'))
);

-- ---------------------------------------------------------------------
-- 5. 证据与运行记录挂上批次
-- ---------------------------------------------------------------------
ALTER TABLE amazon_us.collection_evidence
    ADD COLUMN IF NOT EXISTS batch_id uuid;
ALTER TABLE amazon_us.collection_run
    ADD COLUMN IF NOT EXISTS batch_id uuid;

-- 老库补列（新库 CREATE TABLE 已包含，此处幂等）
ALTER TABLE amazon_us.batch_item ADD COLUMN IF NOT EXISTS review_page_limit integer;
ALTER TABLE amazon_us.batch_item ADD COLUMN IF NOT EXISTS reported_rating_count integer;
ALTER TABLE amazon_us.batch_item ADD COLUMN IF NOT EXISTS reported_review_count integer;
ALTER TABLE amazon_us.batch_item ADD COLUMN IF NOT EXISTS reported_count_source text;
ALTER TABLE amazon_us.batch_item ADD COLUMN IF NOT EXISTS fetched_review_count integer NOT NULL DEFAULT 0;
ALTER TABLE amazon_us.batch_item ADD COLUMN IF NOT EXISTS review_pages_fetched integer NOT NULL DEFAULT 0;

-- "只认本批次证据"的查询入口
CREATE INDEX IF NOT EXISTS idx_evidence_batch_asin
    ON amazon_us.collection_evidence (batch_id, asin, retrieved_at DESC)
    WHERE batch_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_collection_run_batch
    ON amazon_us.collection_run (batch_id)
    WHERE batch_id IS NOT NULL;

-- ---------------------------------------------------------------------
-- 6. batch_progress 视图：批次进度的唯一推导处
--    页面 / API 只读这个视图，禁止再从多张表自行拼装进度。
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW amazon_us.batch_progress AS
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
    -- 成员状态计数（分子分母都只来自本批次）
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
    -- 本批次证据计数（与 succeeded 交叉校验用：两者应一致）
    (SELECT COUNT(*) FROM amazon_us.collection_evidence e
      WHERE e.batch_id = b.batch_id)                             AS evidence_count,
    -- 本批次流量（字节，只统计有记录的）
    (SELECT COALESCE(SUM(e.transfer_bytes), 0)
       FROM amazon_us.collection_evidence e
      WHERE e.batch_id = b.batch_id)                             AS evidence_bytes
FROM amazon_us.batch b
LEFT JOIN amazon_us.batch_item i ON i.batch_id = b.batch_id
GROUP BY b.batch_id;

-- ---------------------------------------------------------------------
-- 7.（可选回填，默认注释）把旧 item_state 里的存量任务转成一个迁移批次
--    新装环境无需执行；存量环境首次切换时打开注释执行一次。
-- ---------------------------------------------------------------------
-- INSERT INTO amazon_us.batch (tenant_id, marketplace, manifest_hash,
--     requested_count, status, uploaded_by, started_at)
-- SELECT tenant_id, marketplace, 'legacy-' || md5(tenant_id || marketplace),
--        COUNT(*), 'running', 'migration', now()
-- FROM amazon_us.item_state
-- WHERE status IN ('pending','running','product_done','reviews_pending')
-- GROUP BY tenant_id, marketplace;
--
-- INSERT INTO amazon_us.batch_item (batch_id, tenant_id, marketplace, asin, url, status)
-- SELECT (SELECT batch_id FROM amazon_us.batch
--         WHERE manifest_hash = 'legacy-' || md5(s.tenant_id || s.marketplace)
--         LIMIT 1),
--        s.tenant_id, s.marketplace, s.asin, s.url,
--        CASE WHEN s.status = 'running' THEN 'pending' ELSE 'pending' END
-- FROM amazon_us.item_state s
-- WHERE s.status IN ('pending','running','product_done','reviews_pending');
