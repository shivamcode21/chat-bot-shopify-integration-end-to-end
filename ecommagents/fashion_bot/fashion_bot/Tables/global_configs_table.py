"""
Migration helper for global platform-level config.

Use this for settings that are shared across all clients. Per-client settings
belong in client_configs.
"""

import logging
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


DDL = """
CREATE TABLE IF NOT EXISTS global_configs (
    config_key TEXT PRIMARY KEY,
    config_value JSONB NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def create_global_configs_table(cursor) -> None:
    """Execute global_configs DDL against an already-open cursor."""
    cursor.execute(DDL)
    logger.info("global_configs table ensured.")


def run_migration() -> None:
    from fashion_bot.database_manager import get_postgres_connection

    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            create_global_configs_table(cur)
        conn.commit()
    print("Migration complete: global_configs")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_migration()
