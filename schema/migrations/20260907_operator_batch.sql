-- Isolated candidate only. Apply explicitly; never auto-migrate from a reader.
CREATE TABLE IF NOT EXISTS amazon_us.operator_batch (
    tenant_id text NOT NULL,
    batch_id text NOT NULL,
    subject_type text NOT NULL CHECK(subject_type IN ('own','competitor','candidate')),
    status text NOT NULL DEFAULT 'queued' CHECK(status IN
      ('queued','canary','reserving','collecting','finalizing','releasing','cleanup_pending','completed','failed','stopped')),
    requested integer NOT NULL CHECK(requested > 0),
    stop_requested boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    last_error text,
    PRIMARY KEY(tenant_id,batch_id)
);
ALTER TABLE amazon_us.operator_batch ADD COLUMN IF NOT EXISTS last_error text;
CREATE TABLE IF NOT EXISTS amazon_us.operator_batch_member (
    tenant_id text NOT NULL,
    batch_id text NOT NULL,
    asin varchar(10) NOT NULL CHECK(asin ~ '^[A-Z0-9]{10}$'),
    ordinal integer NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK(status IN
      ('pending','claimed','completed','variant','failed','blocked','stopped','reused')),
    lease_token text,
    lease_owner text,
    lease_expires_at timestamptz,
    last_error text,
    evidence_id bigint REFERENCES amazon_us.collection_evidence(id),
    PRIMARY KEY(tenant_id,batch_id,asin),
    UNIQUE(tenant_id,batch_id,ordinal),
    FOREIGN KEY(tenant_id,batch_id) REFERENCES amazon_us.operator_batch(tenant_id,batch_id)
);
ALTER TABLE amazon_us.operator_batch_member ADD COLUMN IF NOT EXISTS last_error text;
CREATE TABLE IF NOT EXISTS amazon_us.operator_batch_run (
    tenant_id text NOT NULL,
    batch_id text NOT NULL,
    run_id text NOT NULL,
    worker_id text NOT NULL,
    PRIMARY KEY(tenant_id,run_id),
    FOREIGN KEY(tenant_id,batch_id) REFERENCES amazon_us.operator_batch(tenant_id,batch_id)
);
