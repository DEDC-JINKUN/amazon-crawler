-- Apply after 20260904_recovery.sql, with production collectors stopped.
-- Additive only. Rollback keeps this audit table and leaves old collectors stopped.
CREATE TABLE IF NOT EXISTS amazon_us.recovery_batch (
    tenant_id text NOT NULL, run_id text NOT NULL, subject_type text NOT NULL,
    asins text[] NOT NULL CHECK(cardinality(asins)>0),
    deadline timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'running' CHECK(status IN ('running','completed','exhausted','deadline','interrupted')),
    reason text, created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
    PRIMARY KEY(tenant_id,run_id)
);
