-- Additive recovery v1. Apply explicitly to a stopped deployment after approval.
-- Rollback: stop collectors; retain these audit tables. Old collectors ignore
-- these budgets and must not resume without explicit approval. Read-only rollback is safe.
CREATE TABLE IF NOT EXISTS amazon_us.recovery_job (
    tenant_id text NOT NULL, asin varchar(10) NOT NULL, subject_type text NOT NULL,
    stage text NOT NULL CHECK (stage IN ('product','reviews')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    request_count integer NOT NULL DEFAULT 0 CHECK (request_count >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 3),
    max_requests integer NOT NULL DEFAULT 12 CHECK (max_requests BETWEEN 1 AND 12),
    deadline timestamptz NOT NULL,
    next_retry_at timestamptz, outcome text NOT NULL DEFAULT 'new',
    terminal boolean NOT NULL DEFAULT false,
    lease_token text, egress_id text NOT NULL,
    known_bytes bigint NOT NULL DEFAULT 0 CHECK (known_bytes >= 0),
    unknown_byte_attempts integer NOT NULL DEFAULT 0 CHECK (unknown_byte_attempts >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id,asin,subject_type,stage)
);
CREATE TABLE IF NOT EXISTS amazon_us.recovery_egress (
    tenant_id text NOT NULL, egress_id text NOT NULL,
    paused_until timestamptz, manually_paused boolean NOT NULL DEFAULT false,
    half_open boolean NOT NULL DEFAULT false,
    consecutive_blocks integer NOT NULL DEFAULT 0,
    outcomes jsonb NOT NULL DEFAULT '[]'::jsonb,
    next_request_at timestamptz,
    lease_token text, lease_expires_at timestamptz,
    PRIMARY KEY (tenant_id,egress_id)
);
CREATE INDEX IF NOT EXISTS recovery_due_idx ON amazon_us.recovery_job(tenant_id,next_retry_at) WHERE NOT terminal;
ALTER TABLE amazon_us.recovery_job ADD COLUMN IF NOT EXISTS browser_request_count integer NOT NULL DEFAULT 0 CHECK (browser_request_count >= 0);
ALTER TABLE amazon_us.recovery_job ADD COLUMN IF NOT EXISTS relay_payload_bytes bigint NOT NULL DEFAULT 0 CHECK (relay_payload_bytes >= 0);
