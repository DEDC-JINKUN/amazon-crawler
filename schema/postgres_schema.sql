-- Amazon US crawler production schema (PostgreSQL 14+).
-- This file defines storage only; it does not start workers or grant access.

CREATE SCHEMA IF NOT EXISTS amazon_us;
SET search_path TO amazon_us, public;

CREATE TABLE IF NOT EXISTS asin_master (
    tenant_id text NOT NULL DEFAULT 'default',
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL CHECK (asin ~ '^[A-Z0-9]{10}$'),
    subject_type text NOT NULL CHECK (subject_type IN ('own', 'competitor', 'candidate')),
    source_type text NOT NULL,
    source_url text,
    priority integer NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
    refresh_policy text NOT NULL DEFAULT 'standard',
    active_status text NOT NULL DEFAULT 'active' CHECK (active_status IN ('active', 'paused', 'retired', 'rejected')),
    discovered_at timestamptz,
    approved_at timestamptz,
    approved_by text,
    owner text,
    provenance_hash text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, marketplace, asin, subject_type)
);

CREATE TABLE IF NOT EXISTS item_state (
    tenant_id text NOT NULL DEFAULT 'default',
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL CHECK (asin ~ '^[A-Z0-9]{10}$'),
    subject_type text NOT NULL CHECK (subject_type IN ('own', 'competitor', 'candidate')),
    url text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'running', 'product_done', 'reviews_pending', 'succeeded', 'blocked', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    resume_status text,
    task_stage text NOT NULL DEFAULT 'product',
    next_review_url text,
    next_review_page integer,
    next_retry_at timestamptz,
    review_page_limit integer NOT NULL DEFAULT 0,
    reported_rating_count integer,
    reported_review_count integer,
    reported_count_source text,
    fetched_review_count integer NOT NULL DEFAULT 0,
    review_pages_fetched integer NOT NULL DEFAULT 0,
    block_reason text,
    last_error text,
    lease_token text,
    lease_owner text,
    lease_expires_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, marketplace, asin, subject_type),
    FOREIGN KEY (tenant_id, marketplace, asin, subject_type)
        REFERENCES asin_master (tenant_id, marketplace, asin, subject_type)
);

CREATE TABLE IF NOT EXISTS state_history (
    id bigserial PRIMARY KEY,
    tenant_id text NOT NULL DEFAULT 'default',
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL,
    subject_type text NOT NULL,
    from_status text,
    to_status text NOT NULL,
    reason text,
    changed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS refresh_request (
    job_id text PRIMARY KEY,
    tenant_id text NOT NULL DEFAULT 'default',
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL,
    subject_type text NOT NULL DEFAULT 'candidate',
    requested_by text NOT NULL,
    reason text NOT NULL,
    status text NOT NULL CHECK (status IN ('queued', 'claimed', 'completed', 'failed', 'cancelled')),
    requested_at timestamptz NOT NULL DEFAULT now(),
    claimed_at timestamptz,
    completed_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_refresh_request_queue ON refresh_request (tenant_id, marketplace, status, requested_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_refresh_request_active_asin
    ON refresh_request (tenant_id, marketplace, asin, subject_type)
    WHERE status IN ('queued', 'claimed');

CREATE TABLE IF NOT EXISTS collection_api_audit (
    id bigserial PRIMARY KEY,
    tenant_id text NOT NULL DEFAULT 'default',
    agent_id text NOT NULL,
    action text NOT NULL,
    resource text NOT NULL,
    outcome text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_collection_api_audit_tenant_time
    ON collection_api_audit (tenant_id, recorded_at DESC);

ALTER TABLE item_state ADD COLUMN IF NOT EXISTS next_retry_at timestamptz;
ALTER TABLE item_state ADD COLUMN IF NOT EXISTS lease_token text;
ALTER TABLE item_state ADD COLUMN IF NOT EXISTS lease_owner text;
ALTER TABLE item_state ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;

CREATE TABLE IF NOT EXISTS review_page_state (
    tenant_id text NOT NULL DEFAULT 'default',
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL,
    subject_type text NOT NULL CHECK (subject_type IN ('own', 'competitor', 'candidate')),
    page integer NOT NULL CHECK (page > 0),
    url text NOT NULL,
    status text NOT NULL,
    next_url text,
    fetched_at timestamptz,
    PRIMARY KEY (tenant_id, marketplace, asin, subject_type, page)
);

CREATE TABLE IF NOT EXISTS collection_evidence (
    id bigserial PRIMARY KEY,
    tenant_id text NOT NULL DEFAULT 'default',
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL,
    subject_type text NOT NULL,
    run_id text NOT NULL,
    url text NOT NULL,
    http_status integer,
    transfer_bytes bigint,
    retrieved_at timestamptz NOT NULL DEFAULT now(),
    source_type text,
    content_hash char(64),
    raw_html_path text,
    block_reason text,
    parser_version text,
    error_code text,
    context_json jsonb NOT NULL DEFAULT '{}'::jsonb
);

ALTER TABLE collection_evidence ADD COLUMN IF NOT EXISTS context_json jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE collection_evidence ADD COLUMN IF NOT EXISTS transfer_bytes bigint;

CREATE TABLE IF NOT EXISTS collection_run (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    command text NOT NULL,
    requested_actions integer NOT NULL CHECK (requested_actions > 0),
    status text NOT NULL CHECK (status IN ('starting','running','completed','blocked','quality_failed','failed','interrupted')),
    worker_id text,
    controller_pid integer,
    controller_exit_code integer,
    worker_exit_code integer,
    termination_reason text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    receipt_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id)
);

CREATE INDEX IF NOT EXISTS idx_collection_run_tenant_started
    ON collection_run (tenant_id, started_at DESC);

CREATE TABLE IF NOT EXISTS operation_run (
    operation_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    operation_type text NOT NULL CHECK (operation_type IN ('egress','probe','run','reviews')),
    status text NOT NULL CHECK (status IN ('running','succeeded','failed','blocked','interrupted')),
    preflight_status text NOT NULL DEFAULT 'not_applicable'
        CHECK (preflight_status IN ('not_applicable','not_started','running','succeeded','failed')),
    preflight_duration_ms numeric(14,1),
    failure_stage text,
    error_class text,
    egress_id text,
    collection_run_id text,
    http_status integer,
    response_bytes bigint,
    probe_elapsed_ms numeric(14,1),
    started_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at timestamptz,
    duration_ms numeric(16,1),
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_operation_run_tenant_started
    ON operation_run (tenant_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_operation_run_status_started
    ON operation_run (status, started_at DESC);

CREATE TABLE IF NOT EXISTS product_snapshot (
    snapshot_id bigserial PRIMARY KEY,
    tenant_id text NOT NULL DEFAULT 'default',
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL,
    subject_type text NOT NULL,
    canonical_url text,
    availability text,
    title text,
    brand text,
    rating text,
    reported_rating_count integer,
    reported_review_count integer,
    review_count text,
    review_count_source text,
    price text,
    bullets jsonb NOT NULL DEFAULT '[]'::jsonb,
    product_description text,
    specs jsonb NOT NULL DEFAULT '{}'::jsonb,
    buy_box jsonb NOT NULL DEFAULT '{}'::jsonb,
    top_reviews jsonb NOT NULL DEFAULT '[]'::jsonb,
    review_link text,
    review_section_anchor text,
    aplus_present boolean NOT NULL DEFAULT false,
    -- BSR（Best Sellers Rank）类目销售排名：主排名 + 主类目 + 全部条目
    bsr_rank integer,
    bsr_category text,
    bsr_entries jsonb NOT NULL DEFAULT '[]'::jsonb,
    collected_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL
);

CREATE TABLE IF NOT EXISTS media_asset (
    tenant_id text NOT NULL DEFAULT 'default',
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL,
    subject_type text NOT NULL,
    placement text NOT NULL,
    entry_type text,
    thumbnail_url text,
    display_url text,
    asset_url text,
    poster_url text,
    ordinal integer,
    is_primary boolean,
    width text,
    height text,
    alt_text text,
    variant_asin text,
    load_status text,
    failure_reason text,
    unique_key text NOT NULL,
    PRIMARY KEY (tenant_id, marketplace, asin, subject_type, unique_key)
);

CREATE TABLE IF NOT EXISTS content_module (
    tenant_id text NOT NULL DEFAULT 'default',
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL,
    subject_type text NOT NULL,
    module_type text NOT NULL,
    position integer NOT NULL DEFAULT 0,
    order_index integer,
    text text,
    image_url text,
    link_url text,
    status text,
    unique_key text NOT NULL,
    PRIMARY KEY (tenant_id, marketplace, asin, subject_type, unique_key)
);

CREATE TABLE IF NOT EXISTS review_summary (
    tenant_id text NOT NULL DEFAULT 'default',
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL,
    subject_type text NOT NULL,
    reported_rating_count integer,
    reported_review_count integer,
    reported_count_source text,
    fetched_count integer NOT NULL DEFAULT 0,
    pages_fetched integer NOT NULL DEFAULT 0,
    next_page text,
    status text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, marketplace, asin, subject_type)
);

CREATE TABLE IF NOT EXISTS review_record (
    tenant_id text NOT NULL DEFAULT 'default',
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL,
    subject_type text NOT NULL,
    review_id text NOT NULL,
    rating text,
    title text,
    body text,
    review_url text,
    review_date text,
    locale text,
    verified boolean,
    body_truncated boolean,
    review_images jsonb NOT NULL DEFAULT '[]'::jsonb,
    page integer,
    unique_key text NOT NULL,
    PRIMARY KEY (tenant_id, marketplace, asin, subject_type, review_id)
);

CREATE INDEX IF NOT EXISTS idx_item_state_status ON item_state (tenant_id, marketplace, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_item_state_lease_expiry ON item_state (tenant_id, marketplace, subject_type, lease_expires_at)
    WHERE lease_expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_evidence_asin_time ON collection_evidence (tenant_id, marketplace, asin, retrieved_at DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_tenant_identity_latest
    ON collection_evidence (tenant_id, marketplace, asin, subject_type, id DESC);
CREATE INDEX IF NOT EXISTS idx_snapshot_latest ON product_snapshot (tenant_id, marketplace, asin, subject_type, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_asin_time ON state_history (tenant_id, marketplace, asin, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_page_state_asin ON review_page_state (tenant_id, marketplace, asin, subject_type, page);

CREATE OR REPLACE VIEW product_latest AS
SELECT DISTINCT ON (tenant_id, marketplace, asin, subject_type) *
FROM product_snapshot
ORDER BY tenant_id, marketplace, asin, subject_type, collected_at DESC, snapshot_id DESC;
