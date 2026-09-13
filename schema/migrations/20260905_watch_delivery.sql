-- Watch reporting is additive. Stop consumers before a code rollback; retain
-- observations, reports and delivery receipts as audit evidence.
CREATE TABLE IF NOT EXISTS amazon_us.watch_report (
    report_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    marketplace text NOT NULL,
    report_kind text NOT NULL CHECK (report_kind IN ('immediate','summary','local_review')),
    event_ids jsonb NOT NULL CHECK (jsonb_typeof(event_ids) = 'array'),
    rule_version text NOT NULL,
    config_version text NOT NULL,
    observed_window_json jsonb NOT NULL,
    content_json jsonb NOT NULL,
    content_text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS watch_report_tenant_created_idx
    ON amazon_us.watch_report (tenant_id, marketplace, created_at DESC);

CREATE TABLE IF NOT EXISTS amazon_us.watch_event_handling (
    event_id text PRIMARY KEY REFERENCES amazon_us.product_watch_event(event_id),
    report_id text REFERENCES amazon_us.watch_report(report_id),
    handling_status text NOT NULL CHECK (handling_status IN ('pending','reported','acknowledged','suppressed')),
    updated_at timestamptz NOT NULL DEFAULT now(),
    reason text
);

CREATE TABLE IF NOT EXISTS amazon_us.watch_delivery (
    delivery_id text PRIMARY KEY,
    report_id text NOT NULL REFERENCES amazon_us.watch_report(report_id),
    tenant_id text NOT NULL,
    channel text NOT NULL,
    recipient text NOT NULL,
    dedupe_key text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending','sending','sent','failed','unknown')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    claimed_at timestamptz,
    sent_at timestamptz,
    channel_receipt_json jsonb,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, channel, recipient, dedupe_key)
);
CREATE INDEX IF NOT EXISTS watch_delivery_pending_idx
    ON amazon_us.watch_delivery (tenant_id, status, available_at, created_at);
