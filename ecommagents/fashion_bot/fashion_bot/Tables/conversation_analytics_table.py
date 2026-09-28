"""
Migration script: conversation_analytics table.

Run once:
    python -m fashion_bot.Tables.conversation_analytics_table

Or import and call create_conversation_analytics_table(cursor) from a migration runner.

Stores multi-dimensional LLM analysis of completed conversations.
The prompt-based analytics pipeline runs as a cron job and writes results here.
This exists alongside the real-time tool-based cancellation_aversion_events table
so both approaches can be compared.
"""

import logging
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


DDL = """
CREATE TABLE IF NOT EXISTS conversation_analytics (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id                UUID NOT NULL,
    conversation_id          UUID NOT NULL REFERENCES conversations(conversation_id),
    phone_number             VARCHAR(100),

    -- Multi-dimensional analysis results (all from single LLM call)
    cancellation_attempted   BOOLEAN,
    cancellation_averted     BOOLEAN,
    cancellation_aversion_method VARCHAR(50),

    order_conversion_assisted BOOLEAN,
    order_conversion_method   VARCHAR(50),
    -- Order IDs created within 12 hours after the conversation (data-driven check)
    converted_order_ids      JSONB DEFAULT '[]'::jsonb,
    -- 'data_driven' (order found in Shopify), 'llm_inferred', or 'both'
    conversion_detected_via  VARCHAR(20),

    -- Deterministic lead generation from customer-message tags
    is_lead                  BOOLEAN NOT NULL DEFAULT FALSE,
    lead_type                VARCHAR(50),
    lead_status              VARCHAR(30),
    lead_source_tags         TEXT[] DEFAULT ARRAY[]::TEXT[],
    lead_customer_message_count INT NOT NULL DEFAULT 0,
    lead_generated_at        TIMESTAMPTZ,
    lead_details             JSONB NOT NULL DEFAULT '{}'::jsonb,

    order_update_performed   BOOLEAN,
    order_update_types       JSONB DEFAULT '[]'::jsonb,

    customer_satisfaction     VARCHAR(50),
    bot_effectiveness         VARCHAR(50),

    escalation_needed        BOOLEAN,
    escalation_reason        VARCHAR(100),

    -- Raw LLM output
    llm_analysis             JSONB NOT NULL DEFAULT '{}'::jsonb,
    llm_confidence           FLOAT,
    llm_reasoning            TEXT,

    -- Metadata
    prompt_version           VARCHAR(20),
    message_count            INT,
    first_message            TEXT,
    channel_type             VARCHAR(20),
    analyzed_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Prevent duplicate analyses for the same conversation
CREATE UNIQUE INDEX IF NOT EXISTS idx_ca_conversation_unique
    ON conversation_analytics (conversation_id);

-- Dashboard: per-client time-ordered listing
CREATE INDEX IF NOT EXISTS idx_ca_client_analyzed
    ON conversation_analytics (client_id, analyzed_at DESC);

-- Dashboard: cancellation aversion filter
CREATE INDEX IF NOT EXISTS idx_ca_cancellation
    ON conversation_analytics (client_id, cancellation_attempted, cancellation_averted)
    WHERE cancellation_attempted = TRUE;

-- Dashboard: bot effectiveness breakdown
CREATE INDEX IF NOT EXISTS idx_ca_effectiveness
    ON conversation_analytics (client_id, bot_effectiveness);

-- Dashboard: order conversion
CREATE INDEX IF NOT EXISTS idx_ca_conversion
    ON conversation_analytics (client_id, order_conversion_assisted)
    WHERE order_conversion_assisted = TRUE;

-- Safe re-run migration for existing installs
ALTER TABLE conversation_analytics
    ADD COLUMN IF NOT EXISTS is_lead BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS lead_type VARCHAR(50),
    ADD COLUMN IF NOT EXISTS lead_status VARCHAR(30),
    ADD COLUMN IF NOT EXISTS lead_source_tags TEXT[] DEFAULT ARRAY[]::TEXT[],
    ADD COLUMN IF NOT EXISTS lead_customer_message_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS lead_generated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lead_details JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS first_message TEXT,
    ADD COLUMN IF NOT EXISTS channel_type VARCHAR(20);

-- Dashboard: leads list and count
CREATE INDEX IF NOT EXISTS idx_ca_leads
    ON conversation_analytics (client_id, lead_generated_at DESC, conversation_id)
    WHERE is_lead = TRUE;

CREATE INDEX IF NOT EXISTS idx_ca_lead_status
    ON conversation_analytics (client_id, lead_status, lead_generated_at DESC, conversation_id)
    WHERE is_lead = TRUE;

CREATE INDEX IF NOT EXISTS idx_ca_lead_type
    ON conversation_analytics (client_id, lead_type, lead_generated_at DESC, conversation_id)
    WHERE is_lead = TRUE;

-- Cron job: find conversations not yet analyzed
-- (used via NOT EXISTS subquery against conversations table)
CREATE INDEX IF NOT EXISTS idx_ca_conversation_id
    ON conversation_analytics (conversation_id);
"""


def create_conversation_analytics_table(cursor) -> None:
    """Execute full CREATE TABLE DDL against an already-open cursor (idempotent)."""
    cursor.execute(DDL)
    logger.info("conversation_analytics table and indexes ensured.")


def run_migration() -> None:
    """
    Standalone migration runner (idempotent).
    Also creates the order_conversation_links table.
    """
    from fashion_bot.database_manager import get_postgres_connection
    from fashion_bot.Tables.global_configs_table import create_global_configs_table
    from fashion_bot.Tables.order_conversation_links_table import (
        create_order_conversation_links_table,
    )

    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            create_global_configs_table(cur)
            create_conversation_analytics_table(cur)
            create_order_conversation_links_table(cur)
        conn.commit()
    print("Migration complete: conversation_analytics + order_conversation_links")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_migration()
