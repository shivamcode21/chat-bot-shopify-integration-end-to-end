"""
Migration helpers for inactive-conversation cursor processing.

The inactivity pipeline needs durable state outside Redis so a scheduler/worker
restart cannot cause missed or repeatedly processed inbound messages.
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


DDL = """
CREATE TABLE IF NOT EXISTS conversation_inactivity_cursors (
    conversation_id UUID PRIMARY KEY REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    client_id UUID NOT NULL,
    phone VARCHAR(100),
    channel_type VARCHAR(20),

    last_processed_inbound_message_at TIMESTAMPTZ,
    last_processed_inbound_message_id UUID,
    last_queued_inbound_message_at TIMESTAMPTZ,
    last_queued_inbound_message_id UUID,

    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    queued_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    processed_at TIMESTAMPTZ,
    retry_count INT NOT NULL DEFAULT 0,
    last_error TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE conversation_inactivity_cursors
    ADD COLUMN IF NOT EXISTS client_id UUID,
    ADD COLUMN IF NOT EXISTS phone VARCHAR(100),
    ADD COLUMN IF NOT EXISTS channel_type VARCHAR(20),
    ADD COLUMN IF NOT EXISTS last_processed_inbound_message_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_processed_inbound_message_id UUID,
    ADD COLUMN IF NOT EXISTS last_queued_inbound_message_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_queued_inbound_message_id UUID,
    ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS queued_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS retry_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_error TEXT,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_cic_status_queued
    ON conversation_inactivity_cursors (status, queued_at);

CREATE INDEX IF NOT EXISTS idx_cic_client_status
    ON conversation_inactivity_cursors (client_id, status);

CREATE INDEX IF NOT EXISTS idx_cic_last_queued
    ON conversation_inactivity_cursors (last_queued_inbound_message_at);

CREATE TABLE IF NOT EXISTS conversation_leads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL,
    conversation_id UUID NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
    phone_number VARCHAR(100),
    lead_type VARCHAR(50),
    lead_status VARCHAR(30),
    lead_source_tags TEXT[] DEFAULT ARRAY[]::TEXT[],
    lead_customer_message_count INT NOT NULL DEFAULT 0,
    lead_details JSONB NOT NULL DEFAULT '{}'::jsonb,
    source VARCHAR(50) NOT NULL DEFAULT 'inactive_conversation',
    analytics_id UUID,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE conversation_leads
    ADD COLUMN IF NOT EXISTS client_id UUID,
    ADD COLUMN IF NOT EXISTS conversation_id UUID,
    ADD COLUMN IF NOT EXISTS phone_number VARCHAR(100),
    ADD COLUMN IF NOT EXISTS lead_type VARCHAR(50),
    ADD COLUMN IF NOT EXISTS lead_status VARCHAR(30),
    ADD COLUMN IF NOT EXISTS lead_source_tags TEXT[] DEFAULT ARRAY[]::TEXT[],
    ADD COLUMN IF NOT EXISTS lead_customer_message_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS lead_details JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS source VARCHAR(50) NOT NULL DEFAULT 'inactive_conversation',
    ADD COLUMN IF NOT EXISTS analytics_id UUID,
    ADD COLUMN IF NOT EXISTS generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_leads_conversation_unique
    ON conversation_leads (conversation_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_leads_customer_active_unique
    ON conversation_leads (client_id, phone_number)
    WHERE phone_number IS NOT NULL
      AND LOWER(COALESCE(lead_status, '')) <> 'closed';

CREATE INDEX IF NOT EXISTS idx_conversation_leads_client_generated
    ON conversation_leads (client_id, generated_at DESC);

CREATE INDEX IF NOT EXISTS idx_conversation_leads_type
    ON conversation_leads (client_id, lead_type, generated_at DESC);
"""


def create_conversation_inactivity_processing_tables(cursor) -> None:
    """Execute idempotent cursor/lead table DDL using an existing cursor."""
    cursor.execute(DDL)
    logger.info("conversation inactivity cursor and lead tables ensured.")


def run_migration() -> None:
    """Standalone idempotent migration runner."""
    from fashion_bot.database_manager import get_postgres_connection

    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            create_conversation_inactivity_processing_tables(cur)
        conn.commit()
    print("Migration complete: conversation_inactivity_cursors + conversation_leads")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_migration()
