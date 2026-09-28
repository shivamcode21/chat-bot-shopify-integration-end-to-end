"""
Migration script: cancellation_aversion_events table.

Run once:
    python -m fashion_bot.Tables.cancellation_aversion_table

Or import and call create_cancellation_aversion_table(cursor) from a migration runner.
"""

import os
import logging
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


DDL = """
CREATE TABLE IF NOT EXISTS cancellation_aversion_events (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id                UUID NOT NULL,

    -- 'cancellation_aversion' | 'rto_aversion'
    -- cancellation_aversion: user explicitly said cancel, bot averted it
    -- rto_aversion:          user had delivery issue, bot fixed order details
    --                        implicitly preventing a return-to-origin / cancellation
    event_type               VARCHAR(30) NOT NULL DEFAULT 'cancellation_aversion',

    -- conversation_id: informational only, NOT FK-linked (avoids tight coupling)
    conversation_id          VARCHAR(100),
    phone_number             VARCHAR(20),
    order_id                 VARCHAR(100),

    -- ── Intent / Trigger (Moment A) ───────────────────────────────────────────
    intent_detected_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    intent_trigger_msg       TEXT,
    -- cancellation_aversion: 'state_flag' | 'tool_signal' | 'detected_intent' | 'llm_classifier' | 'retroactive'
    -- rto_aversion:          'order_update_tool'
    intent_trigger_type      VARCHAR(30),
    -- 'high' | 'medium' | 'low'
    intent_confidence        VARCHAR(10),

    -- ── Outcome (Moment B) ────────────────────────────────────────────────────
    -- 'pending' | 'averted' | 'cancelled' | 'escalated' | 'abandoned'
    -- rto_aversion events are always 'averted' (open+close in same turn)
    status                   VARCHAR(20) NOT NULL DEFAULT 'pending',

    -- cancellation_aversion resolutions:
    --   tool-driven:  'exchange' | 'return' | 'address_fix' | 'product_change' | 'escalation'
    --   llm-driven:   'llm_persuasion' | 'context_shift' | 'implicit_drop'
    -- rto_aversion resolutions:
    --   'address_update' | 'phone_update' | 'order_detail_update' | 'multi_field_update'
    resolution               VARCHAR(50),

    -- 'tool_driven' | 'llm_driven'
    aversion_method          VARCHAR(20),

    -- exact tool(s) that sealed the outcome; comma-separated for multi-tool RTO events
    outcome_trigger_tool     VARCHAR(255),
    resolved_at              TIMESTAMPTZ,

    -- ── LLM Classification (deferred — only used for cancellation_aversion pending events) ──
    llm_classified           BOOLEAN NOT NULL DEFAULT FALSE,
    -- 'averted' | 'cancelled' | 'abandoned' | 'unclear'
    llm_verdict              VARCHAR(20),
    llm_confidence           FLOAT,
    llm_reasoning            TEXT,

    -- ── Evidence trail ────────────────────────────────────────────────────────
    -- JSON array of tool name strings accumulated across all turns in this flow
    tools_called             JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- JSON array of soft signal strings e.g. ["context_shift", "intent_reasserted"]
    intermediate_signals     JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- JSON array of {role, content} pairs from the cancellation/update window
    conversation_snapshot    JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- how many webhook turns occurred between trigger and outcome
    turn_count               INT NOT NULL DEFAULT 0,
    -- arbitrary extra: trace_id, gupshup_source, etc.
    metadata                 JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Indexes ────────────────────────────────────────────────────────────────────
-- Dashboard: per-client time-ordered listing
CREATE INDEX IF NOT EXISTS idx_cae_client_created
    ON cancellation_aversion_events (client_id, created_at DESC);

-- Dashboard: filter by status
CREATE INDEX IF NOT EXISTS idx_cae_client_status
    ON cancellation_aversion_events (client_id, status);

-- Dashboard: split cancellation_aversion vs rto_aversion KPIs
CREATE INDEX IF NOT EXISTS idx_cae_event_type
    ON cancellation_aversion_events (event_type, client_id, created_at DESC);

-- Redis fallback: find open pending event by phone+client
CREATE INDEX IF NOT EXISTS idx_cae_phone_client
    ON cancellation_aversion_events (phone_number, client_id);

-- Background classifier job: find pending events due for LLM review
-- Only cancellation_aversion events go through deferred LLM classification;
-- rto_aversion events are always closed immediately so they never appear here.
CREATE INDEX IF NOT EXISTS idx_cae_pending_classifier
    ON cancellation_aversion_events (intent_detected_at)
    WHERE status = 'pending' AND llm_classified = FALSE;

-- Dashboard: breakdown by aversion method
CREATE INDEX IF NOT EXISTS idx_cae_aversion_method
    ON cancellation_aversion_events (aversion_method, client_id)
    WHERE status = 'averted';
""";

# ── ALTER TABLE migration (safe to re-run — ADD COLUMN IF NOT EXISTS) ─────────
ALTER_DDL = """
ALTER TABLE cancellation_aversion_events
    ADD COLUMN IF NOT EXISTS event_type VARCHAR(30) NOT NULL DEFAULT 'cancellation_aversion';

-- Backfill existing rows that pre-date the event_type column
UPDATE cancellation_aversion_events
    SET event_type = 'cancellation_aversion'
    WHERE event_type IS NULL OR event_type = '';

CREATE INDEX IF NOT EXISTS idx_cae_event_type
    ON cancellation_aversion_events (event_type, client_id, created_at DESC);

-- Widen outcome_trigger_tool to hold comma-separated multi-tool RTO events
ALTER TABLE cancellation_aversion_events
    ALTER COLUMN outcome_trigger_tool TYPE VARCHAR(255);
"""


def create_cancellation_aversion_table(cursor) -> None:
    """Execute full CREATE TABLE DDL against an already-open cursor (idempotent)."""
    cursor.execute(DDL)
    logger.info("✅ cancellation_aversion_events table and indexes ensured.")


def alter_cancellation_aversion_table(cursor) -> None:
    """
    Apply incremental schema changes to an existing table (idempotent).
    Run this when upgrading from the initial schema that lacked event_type.
    """
    cursor.execute(ALTER_DDL)
    logger.info("✅ cancellation_aversion_events schema upgrade applied (event_type + wider outcome_trigger_tool).")


def run_migration() -> None:
    """
    Standalone migration runner.
    - Fresh installs: runs CREATE TABLE (idempotent).
    - Existing installs: also runs ALTER TABLE to add event_type column.
    """
    from fashion_bot.database_manager import get_postgres_connection

    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            create_cancellation_aversion_table(cur)
            alter_cancellation_aversion_table(cur)
        conn.commit()
    print("✅ Migration complete: cancellation_aversion_events (with event_type)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_migration()

