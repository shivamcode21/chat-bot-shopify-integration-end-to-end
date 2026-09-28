"""Storage for raw Return Prime webhook events."""

from __future__ import annotations


def create_return_prime_webhook_events_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS return_prime_webhook_events (
            id BIGSERIAL PRIMARY KEY,
            client_id UUID REFERENCES clients(id) ON DELETE SET NULL,
            topic TEXT,
            event_id TEXT,
            request_id TEXT,
            request_number TEXT,
            request_type TEXT,
            request_status TEXT,
            order_number TEXT,
            return_request_id TEXT,
            shopify_order_id TEXT,
            customer_phone TEXT,
            customer_email TEXT,
            refund_status TEXT,
            refund_mode TEXT,
            awb TEXT,
            shipping_company TEXT,
            shipment_status TEXT,
            exchange_order_id TEXT,
            exchange_order_name TEXT,
            original_variant_id TEXT,
            exchange_variant_id TEXT,
            token_hash TEXT,
            payload JSONB NOT NULL,
            headers JSONB NOT NULL DEFAULT '{}'::jsonb,
            trace_id TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_return_prime_webhook_events_client_received
            ON return_prime_webhook_events (client_id, received_at DESC);
        CREATE INDEX IF NOT EXISTS idx_return_prime_webhook_events_topic_received
            ON return_prime_webhook_events (topic, received_at DESC);
        CREATE INDEX IF NOT EXISTS idx_return_prime_webhook_events_order_number
            ON return_prime_webhook_events (order_number)
            WHERE order_number IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_return_prime_webhook_events_request_id
            ON return_prime_webhook_events (request_id)
            WHERE request_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_return_prime_webhook_events_request_number
            ON return_prime_webhook_events (request_number)
            WHERE request_number IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_return_prime_webhook_events_customer_phone
            ON return_prime_webhook_events (customer_phone)
            WHERE customer_phone IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_return_prime_webhook_events_awb
            ON return_prime_webhook_events (awb)
            WHERE awb IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_return_prime_webhook_events_exchange_order
            ON return_prime_webhook_events (exchange_order_name)
            WHERE exchange_order_name IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_return_prime_webhook_events_token_hash
            ON return_prime_webhook_events (token_hash)
            WHERE token_hash IS NOT NULL;
        """
    )
