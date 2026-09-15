-- Release-gate authorization for a fixed cohort inside one existing campaign.
CREATE TABLE IF NOT EXISTS amazon_us.collection_release_gate (
    tenant_id text NOT NULL,
    gate_id text NOT NULL,
    campaign_id text NOT NULL,
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    release_id text NOT NULL,
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','stopped','passed','failed')),
    selection_limit integer NOT NULL CHECK (selection_limit = 20),
    selection_criteria jsonb NOT NULL DEFAULT '{}'::jsonb,
    selection_hash text,
    selected_count integer NOT NULL DEFAULT 0 CHECK (selected_count BETWEEN 0 AND 20),
    stop_requested_at timestamptz,
    started_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at timestamptz,
    failure_reason text,
    receipt_json jsonb,
    created_by text NOT NULL,
    PRIMARY KEY (tenant_id, gate_id),
    UNIQUE (tenant_id, campaign_id),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, campaign_id)
        REFERENCES amazon_us.collection_campaign(tenant_id, campaign_id)
);

ALTER TABLE amazon_us.collection_campaign
    ADD COLUMN IF NOT EXISTS release_gate_id text;

ALTER TABLE amazon_us.collection_campaign_member
    ADD COLUMN IF NOT EXISTS release_gate_id text;
ALTER TABLE amazon_us.collection_campaign_member
    ADD COLUMN IF NOT EXISTS release_gate_ordinal integer;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='collection_campaign_release_gate_fk'
          AND conrelid='amazon_us.collection_campaign'::regclass
    ) THEN
        ALTER TABLE amazon_us.collection_campaign
            ADD CONSTRAINT collection_campaign_release_gate_fk
            FOREIGN KEY (tenant_id, release_gate_id)
            REFERENCES amazon_us.collection_release_gate(tenant_id, gate_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='collection_campaign_member_release_gate_fk'
          AND conrelid='amazon_us.collection_campaign_member'::regclass
    ) THEN
        ALTER TABLE amazon_us.collection_campaign_member
            ADD CONSTRAINT collection_campaign_member_release_gate_fk
            FOREIGN KEY (tenant_id, release_gate_id)
            REFERENCES amazon_us.collection_release_gate(tenant_id, gate_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='collection_campaign_member_release_gate_shape'
          AND conrelid='amazon_us.collection_campaign_member'::regclass
    ) THEN
        ALTER TABLE amazon_us.collection_campaign_member
            ADD CONSTRAINT collection_campaign_member_release_gate_shape
            CHECK ((release_gate_id IS NULL AND release_gate_ordinal IS NULL)
                OR (release_gate_id IS NOT NULL AND release_gate_ordinal BETWEEN 0 AND 19));
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS campaign_release_gate_member_ordinal_uq
ON amazon_us.collection_campaign_member(tenant_id, release_gate_id, release_gate_ordinal)
WHERE release_gate_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS campaign_release_gate_member_job_idx
ON amazon_us.collection_campaign_member(tenant_id, release_gate_id, refresh_job_id)
WHERE release_gate_id IS NOT NULL;
