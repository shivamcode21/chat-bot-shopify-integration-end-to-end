"""Add indexes used by the conversation inactivity scheduler.

These indexes intentionally live outside the scheduler's lazy DDL path because
they target high-traffic existing tables. Run this migration separately so
Postgres can build them concurrently without blocking normal reads/writes.
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


INDEX_STATEMENTS = (
    """
    CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_messages_conv_side_created_desc
    ON messages (conversation_id, created_at DESC, message_id DESC)
    WHERE message_side = 'user_to_system'
    """,
)


def run_migration() -> None:
    """Create scheduler indexes using autocommit for CONCURRENTLY support."""
    from fashion_bot.database_manager import get_postgres_connection

    with get_postgres_connection() as conn:
        original_autocommit = getattr(conn, "autocommit", None)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                for statement in INDEX_STATEMENTS:
                    cur.execute(statement)
        finally:
            if original_autocommit is not None:
                conn.autocommit = original_autocommit

    logger.info("conversation inactivity scheduler indexes ensured.")
    print("Migration complete: conversation inactivity scheduler indexes")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_migration()
