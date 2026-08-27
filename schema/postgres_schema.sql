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
    review_page_limit integer NOT NULL DEFAULT 0,
    reported_rating_count integer,
    reported_review_count integer,
    reported_count_source text,
    fetched_review_count integer NOT NULL DEFAULT 0,
    review_pages_fetched integer NOT NULL DEFAULT 0,
    block_reason text,
    last_error text,
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

CREATE TABLE IF NOT EXISTS collection_evidence (
    id bigserial PRIMARY KEY,
    tenant_id text NOT NULL DEFAULT 'default',
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL,
    subject_type text NOT NULL,
    run_id text NOT NULL,
    url text NOT NULL,
    http_status integer,
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
CREATE INDEX IF NOT EXISTS idx_evidence_asin_time ON collection_evidence (tenant_id, marketplace, asin, retrieved_at DESC);
CREATE INDEX IF NOT EXISTS idx_snapshot_latest ON product_snapshot (tenant_id, marketplace, asin, subject_type, collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_asin_time ON state_history (tenant_id, marketplace, asin, changed_at DESC);

CREATE OR REPLACE VIEW product_latest AS
SELECT DISTINCT ON (tenant_id, marketplace, asin, subject_type) *
FROM product_snapshot
ORDER BY tenant_id, marketplace, asin, subject_type, collected_at DESC, snapshot_id DESC;
