"""
Migration script: order_conversation_links table.

Run once:
    python -m fashion_bot.Tables.order_conversation_links_table

Maps orders to the conversations that influenced them, so the sales channel
can show "View conversation" links next to each order.

Link types:
  - conversion:           customer placed this order within 12h of the conversation
  - cancellation_aversion: bot averted cancellation of this order
  - order_update:         bot updated details on this order (address, phone, etc.)
  - status_inquiry:       customer asked about this order's status
"""

import logging
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


DDL = """
CREATE TABLE IF NOT EXISTS order_conversation_links (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id         UUID NOT NULL,
    order_id          VARCHAR(100) NOT NULL,
    conversation_id   UUID NOT NULL REFERENCES conversations(conversation_id),
    phone_number      VARCHAR(20),

    -- What role the conversation played for this order
    link_type         VARCHAR(30) NOT NULL,

    -- When the link was detected (analytics run time, not conversation time)
    linked_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Conversation timestamps for the sales page to display
    conversation_started_at TIMESTAMPTZ,
    conversation_ended_at   TIMESTAMPTZ,

    -- Quick-reference metadata so the sales page doesn't need a second query
    message_count     INT,
    channel_type      VARCHAR(20) DEFAULT 'whatsapp',

    CONSTRAINT uq_order_conv_link UNIQUE (order_id, conversation_id, link_type)
);

-- Sales page: "show me all conversations for this order"
CREATE INDEX IF NOT EXISTS idx_ocl_order_client
    ON order_conversation_links (client_id, order_id);

-- Analytics: "show me all orders influenced by conversations"
CREATE INDEX IF NOT EXISTS idx_ocl_conversation
    ON order_conversation_links (conversation_id);

-- Dashboard: filter by link type
CREATE INDEX IF NOT EXISTS idx_ocl_link_type
    ON order_conversation_links (client_id, link_type, linked_at DESC);
"""


def create_order_conversation_links_table(cursor) -> None:
    """Execute full CREATE TABLE DDL (idempotent)."""
    cursor.execute(DDL)
    logger.info("order_conversation_links table and indexes ensured.")


def run_migration() -> None:
    from fashion_bot.database_manager import get_postgres_connection

    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            create_order_conversation_links_table(cur)
        conn.commit()
    print("Migration complete: order_conversation_links")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_migration()
