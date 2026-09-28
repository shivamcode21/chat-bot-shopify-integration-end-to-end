-- Run this setup SQL before enabling the Return Prime webhook flow.
CREATE TABLE IF NOT EXISTS return_prime_webhook_events (
    id BIGSERIAL PRIMARY KEY,
    client_id TEXT NOT NULL,
    event_type TEXT NULL,
    request_id TEXT NULL,
    request_number TEXT NULL,
    request_type TEXT NULL,
    request_status TEXT NULL,
    order_name TEXT NULL,
    customer_email TEXT NULL,
    customer_phone TEXT NULL,
    payload_json JSONB NOT NULL,
    headers_json JSONB NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    processing_status TEXT NOT NULL DEFAULT 'received',
    processing_error TEXT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS return_prime_whatsapp_notifications (
    id BIGSERIAL PRIMARY KEY,
    client_id TEXT NOT NULL,
    webhook_event_id BIGINT NULL REFERENCES return_prime_webhook_events(id) ON DELETE SET NULL,
    request_id TEXT NULL,
    request_number TEXT NULL,
    order_name TEXT NULL,
    customer_phone TEXT NULL,
    template_key TEXT NULL,
    template_id TEXT NULL,
    template_params_json JSONB NULL,
    message_text TEXT NULL,
    gupshup_message_id TEXT NULL,
    gupshup_response_json JSONB NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT NULL,
    sent_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
