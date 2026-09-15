-- Independent Amazon US review-provider storage.
-- Woot observations are never written into amazon_us PDP task/snapshot tables.

BEGIN;
CREATE SCHEMA IF NOT EXISTS amazon_reviews;
COMMENT ON SCHEMA amazon_reviews IS 'amazon-reviews-schema-v1';
SET search_path TO amazon_reviews, public;

CREATE TABLE IF NOT EXISTS review_collection_run (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    provider text NOT NULL CHECK (length(btrim(provider)) > 0),
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    mode text NOT NULL CHECK (mode IN ('basic', 'full', 'max', 'adaptive', 'import')),
    status text NOT NULL CHECK (status IN ('running', 'success', 'partial_success', 'failed', 'blocked')),
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    request_count integer NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    response_body_bytes bigint CHECK (response_body_bytes IS NULL OR response_body_bytes >= 0),
    successful_asins integer NOT NULL DEFAULT 0 CHECK (successful_asins >= 0),
    failed_asins integer NOT NULL DEFAULT 0 CHECK (failed_asins >= 0),
    unique_reviews integer NOT NULL DEFAULT 0 CHECK (unique_reviews >= 0),
    coverage_counts jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(coverage_counts) = 'object'),
    source_manifest_sha256 char(64),
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata_json) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id),
    CHECK (source_manifest_sha256 IS NULL OR source_manifest_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS review_import_batch (
    tenant_id text NOT NULL,
    import_batch_id text NOT NULL,
    run_id text NOT NULL,
    provider text NOT NULL,
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    schema_version text NOT NULL,
    source_filename text NOT NULL,
    source_sha256 char(64) NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    manifest_sha256 char(64) NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    asin_count integer NOT NULL CHECK (asin_count >= 0),
    review_count integer NOT NULL CHECK (review_count >= 0),
    imported_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, import_batch_id),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES review_collection_run (tenant_id, run_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS review_job_state (
    tenant_id text NOT NULL,
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL CHECK (asin ~ '^[A-Z0-9]{10}$'),
    provider text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'source_limited', 'retryable', 'blocked', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    next_due_at timestamptz,
    last_run_id text,
    coverage_status text CHECK (coverage_status IS NULL OR coverage_status IN ('complete', 'no_reviews_verified', 'partial', 'source_limited', 'blocked')),
    fetched_review_count integer NOT NULL DEFAULT 0 CHECK (fetched_review_count >= 0),
    last_error text,
    lease_token text,
    lease_owner text,
    lease_expires_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, marketplace, asin, provider),
    CHECK ((lease_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_token IS NOT NULL AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS review_query_checkpoint (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    asin varchar(10) NOT NULL CHECK (asin ~ '^[A-Z0-9]{10}$'),
    query_key text NOT NULL,
    page integer NOT NULL CHECK (page > 0),
    star integer CHECK (star IS NULL OR star BETWEEN 0 AND 5),
    sort_order integer CHECK (sort_order IS NULL OR sort_order BETWEEN 0 AND 3),
    paging_next text,
    status text NOT NULL CHECK (status IN ('fetched', 'query_unavailable', 'query_truncated', 'failed', 'blocked')),
    response_sha256 char(64),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id, asin, query_key, page),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES review_collection_run (tenant_id, run_id) ON DELETE RESTRICT,
    CHECK (response_sha256 IS NULL OR response_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS review_raw_evidence (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    evidence_key text NOT NULL,
    asin varchar(10),
    query_key text,
    page integer,
    url text,
    http_status integer,
    body_bytes bigint CHECK (body_bytes IS NULL OR body_bytes >= 0),
    body_sha256 char(64),
    raw_ref text NOT NULL,
    response_headers jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(response_headers) = 'object'),
    error_code text,
    captured_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id, evidence_key),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES review_collection_run (tenant_id, run_id) ON DELETE RESTRICT,
    CHECK (asin IS NULL OR asin ~ '^[A-Z0-9]{10}$'),
    CHECK (page IS NULL OR page > 0),
    CHECK (body_sha256 IS NULL OR body_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS review_record (
    tenant_id text NOT NULL,
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL CHECK (asin ~ '^[A-Z0-9]{10}$'),
    provider text NOT NULL,
    review_id text NOT NULL,
    provider_review_id text,
    id_kind text NOT NULL CHECK (id_kind IN ('provider', 'fallback_hash')),
    author text,
    rating numeric(3, 1) CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
    title text,
    body text,
    review_url text,
    review_date_text text,
    locale text,
    verified boolean,
    vine boolean,
    helpful_votes integer CHECK (helpful_votes IS NULL OR helpful_votes >= 0),
    review_images jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(review_images) = 'array'),
    review_videos jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(review_videos) = 'array'),
    record_sha256 char(64) NOT NULL CHECK (record_sha256 ~ '^[0-9a-f]{64}$'),
    first_seen_run_id text NOT NULL,
    last_seen_run_id text NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, marketplace, asin, provider, review_id),
    FOREIGN KEY (tenant_id, first_seen_run_id)
        REFERENCES review_collection_run (tenant_id, run_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, last_seen_run_id)
        REFERENCES review_collection_run (tenant_id, run_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS review_observation (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL CHECK (asin ~ '^[A-Z0-9]{10}$'),
    provider text NOT NULL,
    review_id text NOT NULL,
    coverage_status text NOT NULL CHECK (coverage_status IN ('complete', 'no_reviews_verified', 'partial', 'source_limited', 'blocked')),
    source_output_sha256 char(64) NOT NULL CHECK (source_output_sha256 ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    review_json jsonb NOT NULL CHECK (jsonb_typeof(review_json) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id, marketplace, asin, provider, review_id),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES review_collection_run (tenant_id, run_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, marketplace, asin, provider, review_id)
        REFERENCES review_record (tenant_id, marketplace, asin, provider, review_id) ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION reject_immutable_review_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'review evidence is append-only; create a new run or import batch'
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS review_import_batch_append_only ON review_import_batch;
CREATE TRIGGER review_import_batch_append_only BEFORE UPDATE OR DELETE ON review_import_batch
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_review_mutation();
DROP TRIGGER IF EXISTS review_import_batch_truncate_guard ON review_import_batch;
CREATE TRIGGER review_import_batch_truncate_guard BEFORE TRUNCATE ON review_import_batch
    FOR EACH STATEMENT EXECUTE FUNCTION reject_immutable_review_mutation();
DROP TRIGGER IF EXISTS review_raw_evidence_append_only ON review_raw_evidence;
CREATE TRIGGER review_raw_evidence_append_only BEFORE UPDATE OR DELETE ON review_raw_evidence
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_review_mutation();
DROP TRIGGER IF EXISTS review_raw_evidence_truncate_guard ON review_raw_evidence;
CREATE TRIGGER review_raw_evidence_truncate_guard BEFORE TRUNCATE ON review_raw_evidence
    FOR EACH STATEMENT EXECUTE FUNCTION reject_immutable_review_mutation();
DROP TRIGGER IF EXISTS review_observation_append_only ON review_observation;
CREATE TRIGGER review_observation_append_only BEFORE UPDATE OR DELETE ON review_observation
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_review_mutation();
DROP TRIGGER IF EXISTS review_observation_truncate_guard ON review_observation;
CREATE TRIGGER review_observation_truncate_guard BEFORE TRUNCATE ON review_observation
    FOR EACH STATEMENT EXECUTE FUNCTION reject_immutable_review_mutation();

CREATE INDEX IF NOT EXISTS idx_review_job_due
    ON review_job_state (tenant_id, provider, status, next_due_at, updated_at);
CREATE INDEX IF NOT EXISTS idx_review_record_asin_date
    ON review_record (tenant_id, marketplace, asin, provider, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_observation_run
    ON review_observation (tenant_id, run_id, asin);
CREATE INDEX IF NOT EXISTS idx_review_evidence_asin
    ON review_raw_evidence (tenant_id, asin, captured_at DESC);

CREATE OR REPLACE VIEW review_asin_latest AS
SELECT job.tenant_id,job.marketplace,job.asin,job.provider,job.status,job.coverage_status,
       job.fetched_review_count,job.last_run_id,job.last_error,job.updated_at
FROM review_job_state AS job;

COMMIT;
