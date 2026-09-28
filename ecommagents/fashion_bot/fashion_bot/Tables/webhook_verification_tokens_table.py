"""
Migration script: webhook_verification_tokens table.

Stores per-client webhook verification tokens for Meta/Instagram-style
challenge verification. Tokens are stored as plaintext because Meta sends the
plaintext hub.verify_token during callback verification.

Run once:
    python -m fashion_bot.Tables.webhook_verification_tokens_table
"""

import logging
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


DDL = """
CREATE TABLE IF NOT EXISTS webhook_verification_tokens (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id       UUID REFERENCES clients(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL DEFAULT 'meta',
    channel         TEXT NOT NULL,
    verify_token    TEXT NOT NULL,
    description     TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_wvt_channel_verify_token_unique
    ON webhook_verification_tokens (provider, channel, verify_token);

CREATE INDEX IF NOT EXISTS idx_wvt_active_lookup
    ON webhook_verification_tokens (provider, channel, verify_token)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_wvt_client_channel
    ON webhook_verification_tokens (client_id, provider, channel)
    WHERE is_active = TRUE;
"""


def create_webhook_verification_tokens_table(cursor) -> None:
    """Execute full CREATE TABLE DDL against an already-open cursor."""
    cursor.execute(DDL)
    logger.info("webhook_verification_tokens table and indexes ensured.")


def run_migration() -> None:
    """Standalone migration runner."""
    from fashion_bot.database_manager import get_postgres_connection

    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            create_webhook_verification_tokens_table(cur)
        conn.commit()
    print("Migration complete: webhook_verification_tokens")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_migration()
