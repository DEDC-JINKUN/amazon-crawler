-- Product-watch v1 is additive. Apply only to an approved, non-production or stopped target.
-- Rollback: stop consumers and retain these audit records; no crawler source tables are changed.
CREATE TABLE IF NOT EXISTS amazon_us.product_watch_observation (
    observation_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    marketplace text NOT NULL,
    subject_type text NOT NULL,
    requested_asin varchar(10) NOT NULL,
    observed_asin varchar(10) NOT NULL,
    parent_asin varchar(10),
    kind text NOT NULL CHECK (kind IN ('price','promotion','bsr','keyword_rank')),
    observed_at timestamptz NOT NULL,
    source_evidence_ids jsonb NOT NULL CHECK (jsonb_typeof(source_evidence_ids) = 'array'),
    value_json jsonb,
    raw_value_json jsonb,
    context_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    quality text NOT NULL CHECK (quality IN ('valid','partial','unknown','invalid')),
    presence text NOT NULL CHECK (presence IN ('observed','confirmed_absent','not_observed')),
    coverage_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    parser_version text,
    normalizer_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS product_watch_observation_latest_idx
    ON amazon_us.product_watch_observation (tenant_id, marketplace, subject_type, requested_asin, observed_asin, kind, observed_at DESC);
CREATE INDEX IF NOT EXISTS product_watch_observation_context_idx
    ON amazon_us.product_watch_observation USING gin (context_json);

CREATE TABLE IF NOT EXISTS amazon_us.product_watch_event (
    event_id text PRIMARY KEY,
    event_type text NOT NULL,
    watch_id text NOT NULL,
    before_observation_id text REFERENCES amazon_us.product_watch_observation(observation_id),
    after_observation_id text NOT NULL REFERENCES amazon_us.product_watch_observation(observation_id),
    old_value_json jsonb,
    new_value_json jsonb,
    delta_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    comparison_context_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_window_json jsonb NOT NULL,
    quality text NOT NULL CHECK (quality IN ('valid','partial','unknown','invalid')),
    evidence_ids jsonb NOT NULL CHECK (jsonb_typeof(evidence_ids) = 'array'),
    rule_version text NOT NULL,
    severity text NOT NULL,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (before_observation_id IS NULL OR before_observation_id <> after_observation_id)
);
CREATE INDEX IF NOT EXISTS product_watch_event_watch_idx
    ON amazon_us.product_watch_event (watch_id, created_at DESC);
