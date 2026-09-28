"""Idempotency/audit table for Return Prime -> WhatsApp/email notifications.

One row per (client_id, request_id, event_key) attempt. The unique
constraint is the atomic claim gate: whichever caller's INSERT actually
creates the row is the only one that proceeds to send, so a redelivered
Return Prime webhook (which creates a *new* raw row in
``return_prime_webhook_events`` on every delivery) can never double-send a
WhatsApp template or email for the same return/exchange/refund event.
"""

from __future__ import annotations


def create_return_prime_event_notifications_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS return_prime_event_notifications (
            id BIGSERIAL PRIMARY KEY,
            client_id UUID REFERENCES clients(id) ON DELETE SET NULL,
            webhook_event_id BIGINT REFERENCES return_prime_webhook_events(id) ON DELETE SET NULL,
            request_id TEXT,
            event_key TEXT NOT NULL,
            order_number TEXT,
            customer_phone TEXT,
            whatsapp_status TEXT,
            whatsapp_error TEXT,
            whatsapp_message_id TEXT,
            email_status TEXT,
            email_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (client_id, request_id, event_key)
        );

        CREATE INDEX IF NOT EXISTS idx_return_prime_event_notifications_client_created
            ON return_prime_event_notifications (client_id, created_at DESC);
        """
    )
