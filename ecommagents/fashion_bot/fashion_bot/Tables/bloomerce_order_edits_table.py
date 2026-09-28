"""
Migration script: bloomerce_order_edits table.

Run once:
    python -m fashion_bot.Tables.bloomerce_order_edits_table

Stores parsed BLOOMERCE_EDITED note data for each agent-assisted order
update.  Populated daily by the ``bloomerce_edited_sync_job`` cron.
"""

import logging
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


DDL = """
CREATE TABLE IF NOT EXISTS bloomerce_order_edits (
    id                  BIGSERIAL PRIMARY KEY,
    client_id           UUID NOT NULL,
    shopify_order_id    TEXT NOT NULL,
    order_name          TEXT,
    customer_name       TEXT,
    order_status        TEXT,
    update_type         TEXT NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    conversation_id     TEXT,
    phone_number        TEXT,
    session_id          TEXT,
    order_value         TEXT,
    shopify_created_at  TIMESTAMPTZ,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_boe_client_order_updated
    ON bloomerce_order_edits (client_id, shopify_order_id, updated_at);

CREATE INDEX IF NOT EXISTS idx_boe_client_updated
    ON bloomerce_order_edits (client_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_boe_update_type
    ON bloomerce_order_edits (client_id, update_type, updated_at DESC);
"""


def create_bloomerce_order_edits_table(cursor) -> None:
    """Execute full CREATE TABLE DDL against an already-open cursor (idempotent)."""
    cursor.execute(DDL)
    logger.info("bloomerce_order_edits table and indexes ensured.")


def run_migration() -> None:
    """Standalone migration runner (idempotent)."""
    from fashion_bot.database_manager import get_postgres_connection

    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            create_bloomerce_order_edits_table(cur)
        conn.commit()
    print("Migration complete: bloomerce_order_edits")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_migration()
