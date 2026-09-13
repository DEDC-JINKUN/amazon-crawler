-- Additive campaign parent/membership queue. Old refresh endpoints ignore it.
CREATE TABLE IF NOT EXISTS amazon_us.collection_campaign (
    tenant_id text NOT NULL,
    campaign_id text NOT NULL,
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    marketplace text NOT NULL DEFAULT 'US' CHECK (marketplace='US'),
    subject_type text NOT NULL DEFAULT 'own',
    priority integer NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
    requested_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
    freshness_seconds integer,
    deadline timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed','expired','cancelled')),
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id,campaign_id),
    UNIQUE (tenant_id,idempotency_key)
);

CREATE TABLE IF NOT EXISTS amazon_us.collection_campaign_member (
    tenant_id text NOT NULL,
    campaign_id text NOT NULL,
    marketplace text NOT NULL DEFAULT 'US' CHECK (marketplace='US'),
    asin varchar(10) NOT NULL,
    subject_type text NOT NULL DEFAULT 'own',
    refresh_job_id text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id,campaign_id,asin,subject_type),
    UNIQUE (tenant_id,campaign_id,ordinal),
    FOREIGN KEY (tenant_id,campaign_id) REFERENCES amazon_us.collection_campaign(tenant_id,campaign_id)
);

CREATE INDEX IF NOT EXISTS campaign_member_job_idx
ON amazon_us.collection_campaign_member(tenant_id,refresh_job_id);
CREATE INDEX IF NOT EXISTS campaign_active_idx
ON amazon_us.collection_campaign(tenant_id,status,deadline) WHERE status='active';
