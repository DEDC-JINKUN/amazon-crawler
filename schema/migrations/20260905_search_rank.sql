-- Search rank is additive and remains separate from product collection state.
CREATE TABLE IF NOT EXISTS amazon_us.search_rank_batch (
    batch_id text PRIMARY KEY,
    run_id text NOT NULL,
    tenant_id text NOT NULL,
    marketplace text NOT NULL CHECK (marketplace = 'US'),
    keyword text NOT NULL,
    search_conditions_json jsonb NOT NULL,
    requested_depth integer NOT NULL CHECK (requested_depth > 0),
    target_asins_json jsonb NOT NULL CHECK (jsonb_typeof(target_asins_json) = 'array'),
    status text NOT NULL CHECK (status IN ('running','completed','partial','failed')),
    metrics_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS search_rank_batch_latest_idx ON amazon_us.search_rank_batch (tenant_id, marketplace, keyword, created_at DESC);

CREATE TABLE IF NOT EXISTS amazon_us.search_rank_page (
    batch_id text NOT NULL REFERENCES amazon_us.search_rank_batch(batch_id) ON DELETE RESTRICT,
    page integer NOT NULL CHECK (page > 0),
    status text NOT NULL CHECK (status IN ('completed','failed')),
    url text NOT NULL,
    http_status integer,
    transfer_bytes bigint,
    retrieved_at timestamptz NOT NULL,
    source_evidence_id text NOT NULL,
    content_hash char(64),
    raw_html_path text,
    cards_json jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(cards_json) = 'array'),
    error_code text,
    evidence_context_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (batch_id, page),
    CHECK ((status = 'completed' AND content_hash IS NOT NULL AND raw_html_path IS NOT NULL) OR status = 'failed')
);
ALTER TABLE amazon_us.search_rank_page ADD COLUMN IF NOT EXISTS cards_json jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE amazon_us.search_rank_page DROP CONSTRAINT IF EXISTS search_rank_page_cards_json_check;
ALTER TABLE amazon_us.search_rank_page ADD CONSTRAINT search_rank_page_cards_json_check CHECK (jsonb_typeof(cards_json) = 'array');
CREATE INDEX IF NOT EXISTS search_rank_page_evidence_idx ON amazon_us.search_rank_page (source_evidence_id);
