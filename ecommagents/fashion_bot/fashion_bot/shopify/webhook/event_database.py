"""
Database service for Shopify webhook events.
Supports multi-client architecture with client_id.
"""
from typing import Optional, Dict, Any
import json
import logging
import time
from datetime import datetime, timezone
from fashion_bot.database_manager import get_async_postgres_connection, get_direct_postgres_cursor
from fashion_bot.config_manager import resolve_client_id, aresolve_client_id

logger = logging.getLogger(__name__)

# Transient error patterns that warrant a retry
TRANSIENT_ERROR_PATTERNS = [
    'ssl connection has been closed',
    'connection has been closed',
    'connection is lost',
    'server closed the connection',
    'connection refused',
    'connection reset',
    'broken pipe',
    'network is unreachable',
    'timeout',
    'consuming input failed'
]


def is_transient_error(error: Exception) -> bool:
    """Check if an error is transient and should be retried."""
    error_str = str(error).lower()
    return any(pattern in error_str for pattern in TRANSIENT_ERROR_PATTERNS)

class ShopifyEventDatabaseService:
    def __init__(self):
        self._ensure_tables()
    
    def _ensure_tables(self):
        """
        Ensure required tables exist with multi-client support.
        Uses DIRECT connection (not pooled) since this runs at startup
        and shouldn't compete with user traffic for pool connections.
        """
        conn = None
        cur = None
        try:
            # Use direct connection for startup operations (bypasses pool)
            conn, cur = get_direct_postgres_cursor()
            
            # Table for tracking processed events (with client_id)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS shopify_events (
                    id SERIAL PRIMARY KEY,
                    client_id VARCHAR(100) NOT NULL,
                    order_id VARCHAR(255) NOT NULL,
                    event_name VARCHAR(100) NOT NULL,
                    event_data JSONB NOT NULL,
                    processed_at TIMESTAMPTZ DEFAULT NOW(),
                    notification_sent BOOLEAN DEFAULT FALSE,
                    notification_sent_at TIMESTAMPTZ,
                    UNIQUE(client_id, order_id, event_name)
                );
            """)
            
            # Add client_id column if it doesn't exist (for existing tables)
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='shopify_events' AND column_name='client_id'
                ) as column_exists;
            """)
            result = cur.fetchone()
            column_exists = result['column_exists'] if result else False
            
            if not column_exists:
                resolved_client_id = resolve_client_id()
                cur.execute("ALTER TABLE shopify_events ADD COLUMN client_id VARCHAR(100);")
                cur.execute("UPDATE shopify_events SET client_id = %s WHERE client_id IS NULL;", (resolved_client_id,))
                cur.execute("ALTER TABLE shopify_events ALTER COLUMN client_id SET NOT NULL;")
                logger.info(f"Added client_id column to shopify_events table with resolved client: {resolved_client_id}")
            
            # Ensure unique constraint exists (drop old one if needed, create new one with client_id)
            cur.execute("""
                DO $$ 
                BEGIN
                    -- Drop old constraint if it exists (without client_id)
                    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'shopify_events_order_id_event_name_key') THEN
                        ALTER TABLE shopify_events DROP CONSTRAINT shopify_events_order_id_event_name_key;
                    END IF;
                    -- Create new constraint with client_id if it doesn't exist
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'shopify_events_client_id_order_id_event_name_key') THEN
                        ALTER TABLE shopify_events ADD CONSTRAINT shopify_events_client_id_order_id_event_name_key UNIQUE (client_id, order_id, event_name);
                    END IF;
                END $$;
            """)
            logger.info("Ensured unique constraint on shopify_events (client_id, order_id, event_name)")
            
            # Table for event deduplication (with client_id)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS shopify_event_deduplication (
                    id SERIAL PRIMARY KEY,
                    client_id VARCHAR(100) NOT NULL,
                    order_id VARCHAR(255) NOT NULL,
                    event_name VARCHAR(100) NOT NULL,
                    event_hash VARCHAR(255) NOT NULL,
                    processed_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(client_id, order_id, event_name, event_hash)
                );
            """)
            
            # Add client_id column if it doesn't exist (for existing tables)
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='shopify_event_deduplication' AND column_name='client_id'
                ) as column_exists;
            """)
            result = cur.fetchone()
            column_exists = result['column_exists'] if result else False
            
            if not column_exists:
                resolved_client_id = resolve_client_id()
                cur.execute("ALTER TABLE shopify_event_deduplication ADD COLUMN client_id VARCHAR(100);")
                cur.execute("UPDATE shopify_event_deduplication SET client_id = %s WHERE client_id IS NULL;", (resolved_client_id,))
                cur.execute("ALTER TABLE shopify_event_deduplication ALTER COLUMN client_id SET NOT NULL;")
                logger.info(f"Added client_id column to shopify_event_deduplication table with resolved client: {resolved_client_id}")
            
            # Ensure unique constraint exists (drop old one if needed, create new one with client_id)
            cur.execute("""
                DO $$ 
                BEGIN
                    -- Drop old constraint if it exists (without client_id)
                    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'shopify_event_deduplication_order_id_event_name_event_hash_key') THEN
                        ALTER TABLE shopify_event_deduplication DROP CONSTRAINT shopify_event_deduplication_order_id_event_name_event_hash_key;
                    END IF;
                    -- Create new constraint with client_id if it doesn't exist
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'shopify_event_dedup_client_order_event_hash_key') THEN
                        ALTER TABLE shopify_event_deduplication ADD CONSTRAINT shopify_event_dedup_client_order_event_hash_key UNIQUE (client_id, order_id, event_name, event_hash);
                    END IF;
                END $$;
            """)
            logger.info("Ensured unique constraint on shopify_event_deduplication (client_id, order_id, event_name, event_hash)")
            
            # Create indexes for better performance (with client_id)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shopify_events_client_id ON shopify_events(client_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shopify_events_order_id ON shopify_events(order_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shopify_events_client_order ON shopify_events(client_id, order_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shopify_event_dedup_client_id ON shopify_event_deduplication(client_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shopify_event_dedup_order_id ON shopify_event_deduplication(order_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shopify_event_dedup_client_order ON shopify_event_deduplication(client_id, order_id);")
            
            logger.info("Shopify event tables initialized successfully with multi-client support")
            
        except Exception as e:
            logger.error(f"Error creating Shopify tables: {e}")
            raise
        finally:
            # Always close direct connections
            if cur:
                try:
                    cur.close()
                except:
                    pass
            if conn:
                try:
                    conn.close()
                except:
                    pass
    
    async def atry_claim_event(self, order_id: str, event_name: str, event_data: Dict[str, Any], client_id: str = None) -> bool:
        """
        Atomically claim (client_id, order_id, event_name, event_hash) for processing.

        The INSERT and the uniqueness check happen as a single atomic operation
        (INSERT ... ON CONFLICT DO NOTHING RETURNING id), so two near-simultaneous
        webhook deliveries for the same event can never both "win" the way a
        separate SELECT-then-INSERT could. Only the caller whose INSERT actually
        creates the row gets True; every other caller — including a genuine
        duplicate delivery arriving milliseconds later — gets False and must
        skip sending.

        On a persistent DB error, fails open (returns True) so a broken database
        doesn't block all notification sends.
        """
        if client_id is None:
            client_id = await aresolve_client_id()
        client_id_str = str(client_id) if client_id else None
        event_hash = self._create_event_hash(event_data)

        for attempt in range(3):
            try:
                async with get_async_postgres_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            INSERT INTO shopify_event_deduplication (client_id, order_id, event_name, event_hash)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (client_id, order_id, event_name, event_hash) DO NOTHING
                            RETURNING id
                            """,
                            (client_id_str, order_id, event_name, event_hash),
                        )
                        claimed = await cur.fetchone() is not None
                        logger.info(f"[DEDUP-CLAIM] [CLIENT: {client_id_str}] order={order_id} event={event_name} claimed={claimed}")
                        return claimed
            except Exception as e:
                if is_transient_error(e) and attempt < 2:
                    logger.warning(f"🔄 Retry {attempt + 1}/2 atry_claim_event for client {client_id_str}: {e}")
                    continue
                logger.error(f"Error claiming Shopify event for client {client_id_str}: {e} — failing open (proceeding)")
                return True

        return True

    async def arelease_event_claim(self, order_id: str, event_name: str, event_data: Dict[str, Any], client_id: str = None) -> bool:
        """
        Release a claim previously won via atry_claim_event().

        Called when processing/sending fails after this caller won the claim,
        so a genuine webhook redelivery for the same event isn't dropped forever.
        """
        if client_id is None:
            client_id = await aresolve_client_id()
        client_id_str = str(client_id) if client_id else None
        event_hash = self._create_event_hash(event_data)

        for attempt in range(3):
            try:
                async with get_async_postgres_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            DELETE FROM shopify_event_deduplication
                            WHERE client_id = %s AND order_id = %s AND event_name = %s AND event_hash = %s
                            """,
                            (client_id_str, order_id, event_name, event_hash),
                        )
                        logger.info(f"[DEDUP-RELEASE] [CLIENT: {client_id_str}] Released claim for order={order_id}, event={event_name}")
                        return True
            except Exception as e:
                if is_transient_error(e) and attempt < 2:
                    logger.warning(f"🔄 Retry {attempt + 1}/2 arelease_event_claim for client {client_id_str}: {e}")
                    continue
                logger.error(f"Error releasing Shopify event claim for client {client_id_str}: {e}")
                return False

        return False

    async def amark_event_processed(self, order_id: str, event_name: str, event_data: Dict[str, Any], client_id: str = None) -> bool:
        """Upsert the latest event snapshot. Deduplication itself is handled by atry_claim_event()."""
        if client_id is None:
            client_id = await aresolve_client_id()
        client_id_str = str(client_id) if client_id else None

        for attempt in range(3):
            try:
                async with get_async_postgres_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            INSERT INTO shopify_events (client_id, order_id, event_name, event_data)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (client_id, order_id, event_name) DO UPDATE SET
                                event_data = EXCLUDED.event_data,
                                processed_at = NOW()
                            """,
                            (client_id_str, order_id, event_name, json.dumps(event_data)),
                        )
                        logger.info(f"Event marked as processed for client {client_id_str}, order {order_id}, event {event_name}")
                        return True
            except Exception as e:
                if is_transient_error(e) and attempt < 2:
                    logger.warning(f"🔄 Retry {attempt + 1}/2 amark_event_processed for client {client_id_str}: {e}")
                    continue
                logger.error(f"Error marking Shopify event as processed for client {client_id_str}: {e}")
                return False

        return False

    async def amark_notification_sent(self, order_id: str, event_name: str, client_id: str = None) -> bool:
        """Async mark notification sent."""
        if client_id is None:
            client_id = await aresolve_client_id()
        client_id_str = str(client_id) if client_id else None

        for attempt in range(3):
            try:
                async with get_async_postgres_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            UPDATE shopify_events
                            SET notification_sent = TRUE, notification_sent_at = NOW()
                            WHERE client_id = %s AND order_id = %s AND event_name = %s
                            """,
                            (client_id_str, order_id, event_name),
                        )
                        logger.debug(f"Notification marked as sent for client {client_id_str}, order {order_id}, event {event_name}")
                        return True
            except Exception as e:
                if is_transient_error(e) and attempt < 2:
                    logger.warning(f"🔄 Retry {attempt + 1}/2 amark_notification_sent for client {client_id_str}: {e}")
                    continue
                logger.error(f"Error marking Shopify notification sent for client {client_id_str}: {e}")
                return False

        return False
    
    def _create_event_hash(self, event_data: Dict[str, Any]) -> str:
        import hashlib
        # Deliberately excludes 'updated_at': that field changes on every order
        # touch (including our own note-write annotations), which previously
        # made a no-op re-delivery of the same status combo look like a brand
        # new event and defeated this table's permanent dedup claim.
        stable_data = {
            'order_id': event_data.get('id', ''),
            'financial_status': event_data.get('financial_status', ''),
            'fulfillment_status': event_data.get('fulfillment_status', ''),
        }
        data_str = json.dumps(stable_data, sort_keys=True)
        return hashlib.md5(data_str.encode()).hexdigest()
