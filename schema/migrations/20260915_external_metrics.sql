-- Source-separated external sales metrics and explicit own-to-competitor tier inheritance.
CREATE TABLE IF NOT EXISTS amazon_us.external_metric_batch (
    tenant_id text NOT NULL,
    batch_id text NOT NULL,
    source text NOT NULL,
    content_hash text NOT NULL,
    row_count integer NOT NULL CHECK (row_count >= 0),
    imported_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, batch_id)
);

CREATE TABLE IF NOT EXISTS amazon_us.external_metrics (
    tenant_id text NOT NULL,
    marketplace text NOT NULL DEFAULT 'US' CHECK (marketplace = 'US'),
    asin varchar(10) NOT NULL CHECK (asin ~ '^[A-Z0-9]{10}$'),
    sales_units numeric NOT NULL CHECK (sales_units >= 0),
    observed_at timestamptz NOT NULL,
    source text NOT NULL,
    batch_id text NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, batch_id, asin),
    FOREIGN KEY (tenant_id, batch_id)
        REFERENCES amazon_us.external_metric_batch(tenant_id, batch_id)
);

CREATE INDEX IF NOT EXISTS external_metrics_latest_idx
ON amazon_us.external_metrics(tenant_id, marketplace, asin, observed_at DESC, imported_at DESC);

CREATE TABLE IF NOT EXISTS amazon_us.external_metric_relationship (
    tenant_id text NOT NULL,
    marketplace text NOT NULL DEFAULT 'US' CHECK (marketplace = 'US'),
    own_asin varchar(10) NOT NULL CHECK (own_asin ~ '^[A-Z0-9]{10}$'),
    competitor_asin varchar(10) NOT NULL CHECK (competitor_asin ~ '^[A-Z0-9]{10}$'),
    source text NOT NULL,
    batch_id text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, own_asin, competitor_asin),
    FOREIGN KEY (tenant_id, batch_id)
        REFERENCES amazon_us.external_metric_batch(tenant_id, batch_id)
);

ALTER TABLE amazon_us.review_record
    ADD COLUMN IF NOT EXISTS source_type text NOT NULL DEFAULT 'amazon_html';
ALTER TABLE amazon_us.review_record
    ADD COLUMN IF NOT EXISTS source_record_id text;

CREATE INDEX IF NOT EXISTS review_record_source_idx
ON amazon_us.review_record(tenant_id, marketplace, asin, subject_type, source_type);

