"""
Database service for ShipRocket webhook events.
Handles event persistence, deduplication, and tracking.
Supports multi-client architecture with client_id.
"""

from typing import Optional, Dict, Any, List
import json
import logging
from datetime import datetime, timezone
from fashion_bot.database_manager import awith_retry, get_async_postgres_connection, get_direct_postgres_cursor
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


def _parse_status_timestamp(timestamp: Optional[str]) -> datetime:
    if timestamp:
        try:
            if " " in str(timestamp):
                return datetime.strptime(timestamp, "%d %m %Y %H:%M:%S")
            return datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.now(timezone.utc)


# Note: Retry logic is implemented inline in each method to avoid blocking delays
# that would tie up thread pool workers when called via asyncio.to_thread()

class EventDatabaseService:
    """Service for managing event database operations"""
    
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
                CREATE TABLE IF NOT EXISTS shipment_events (
                    id SERIAL PRIMARY KEY,
                    client_id VARCHAR(100) NOT NULL,
                    order_id VARCHAR(255) NOT NULL,
                    awb VARCHAR(255) NOT NULL,
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
                    WHERE table_name='shipment_events' AND column_name='client_id'
                ) as column_exists;
            """)
            result = cur.fetchone()
            column_exists = result['column_exists'] if result else False
            
            if not column_exists:
                resolved_client_id = resolve_client_id()
                cur.execute("ALTER TABLE shipment_events ADD COLUMN client_id VARCHAR(100);")
                cur.execute("UPDATE shipment_events SET client_id = %s WHERE client_id IS NULL;", (resolved_client_id,))
                cur.execute("ALTER TABLE shipment_events ALTER COLUMN client_id SET NOT NULL;")
                logger.info(f"Added client_id column to shipment_events table with resolved client: {resolved_client_id}")
            
            # Ensure the unique constraint exists
            cur.execute("""
                SELECT COUNT(*) as cnt FROM pg_constraint 
                WHERE conname = 'shipment_events_client_id_order_id_event_name_key'
            """)
            constraint_result = cur.fetchone()
            if constraint_result and constraint_result['cnt'] == 0:
                cur.execute("""
                    ALTER TABLE shipment_events 
                    DROP CONSTRAINT IF EXISTS shipment_events_order_id_event_name_key
                """)
                cur.execute("""
                    ALTER TABLE shipment_events 
                    ADD CONSTRAINT shipment_events_client_id_order_id_event_name_key 
                    UNIQUE (client_id, order_id, event_name)
                """)
                logger.info("Added unique constraint (client_id, order_id, event_name) to shipment_events")
            
            # Table for event deduplication (with client_id)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS event_deduplication (
                    id SERIAL PRIMARY KEY,
                    client_id VARCHAR(100) NOT NULL,
                    order_id VARCHAR(255) NOT NULL,
                    event_name VARCHAR(100) NOT NULL,
                    event_hash VARCHAR(255) NOT NULL,
                    processed_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(client_id, order_id, event_name, event_hash)
                );
            """)
            
            # Add client_id column if it doesn't exist
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='event_deduplication' AND column_name='client_id'
                ) as column_exists;
            """)
            result = cur.fetchone()
            column_exists = result['column_exists'] if result else False
            
            if not column_exists:
                resolved_client_id = resolve_client_id()
                cur.execute("ALTER TABLE event_deduplication ADD COLUMN client_id VARCHAR(100);")
                cur.execute("UPDATE event_deduplication SET client_id = %s WHERE client_id IS NULL;", (resolved_client_id,))
                cur.execute("ALTER TABLE event_deduplication ALTER COLUMN client_id SET NOT NULL;")
                logger.info(f"Added client_id column to event_deduplication table with resolved client: {resolved_client_id}")
            
            # Ensure the unique constraint exists for event_deduplication
            cur.execute("""
                SELECT COUNT(*) as cnt FROM pg_constraint 
                WHERE conname = 'event_deduplication_client_id_order_id_event_name_event_ha_key'
            """)
            constraint_result = cur.fetchone()
            if constraint_result and constraint_result['cnt'] == 0:
                cur.execute("""
                    ALTER TABLE event_deduplication 
                    DROP CONSTRAINT IF EXISTS event_deduplication_order_id_event_name_event_hash_key
                """)
                cur.execute("""
                    ALTER TABLE event_deduplication 
                    ADD CONSTRAINT event_deduplication_client_id_order_id_event_name_event_ha_key 
                    UNIQUE (client_id, order_id, event_name, event_hash)
                """)
                logger.info("Added unique constraint to event_deduplication table")
            
            # Table for tracking shipment status history (with client_id)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS shipment_status_history (
                    id SERIAL PRIMARY KEY,
                    client_id VARCHAR(100) NOT NULL,
                    order_id VARCHAR(255) NOT NULL,
                    awb VARCHAR(255) NOT NULL,
                    status VARCHAR(100) NOT NULL,
                    status_code VARCHAR(50),
                    location VARCHAR(255),
                    activity TEXT,
                    timestamp TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            
            # Add client_id column if it doesn't exist
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name='shipment_status_history' AND column_name='client_id'
                ) as column_exists;
            """)
            result = cur.fetchone()
            column_exists = result['column_exists'] if result else False
            
            if not column_exists:
                resolved_client_id = resolve_client_id()
                cur.execute("ALTER TABLE shipment_status_history ADD COLUMN client_id VARCHAR(100);")
                cur.execute("UPDATE shipment_status_history SET client_id = %s WHERE client_id IS NULL;", (resolved_client_id,))
                cur.execute("ALTER TABLE shipment_status_history ALTER COLUMN client_id SET NOT NULL;")
                logger.info(f"Added client_id column to shipment_status_history table with resolved client: {resolved_client_id}")
            
            # ── Multi-partner support: add `partner` column + bump unique constraints ──
            # Idempotent: safe to run on every startup. Existing Shiprocket rows
            # back-fill to partner='shiprocket' via the column DEFAULT.
            for table_name in ("shipment_events", "event_deduplication", "shipment_status_history"):
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name=%s AND column_name='partner'
                    ) as column_exists;
                    """,
                    (table_name,),
                )
                row = cur.fetchone()
                has_partner = row['column_exists'] if isinstance(row, dict) else (row[0] if row else False)
                if not has_partner:
                    # Split into three statements to avoid a full table rewrite
                    # under ACCESS EXCLUSIVE on PG < 11. On PG >= 11 a
                    # constant DEFAULT on ADD COLUMN is metadata-only, but we
                    # can't assume the server version at install time. The
                    # three-step pattern is safe everywhere:
                    #   1) ADD COLUMN ... NULL (no rewrite).
                    #   2) UPDATE existing rows to backfill 'shiprocket'
                    #      (all pre-PR rows came from the Shiprocket pipeline).
                    #   3) ALTER ... SET NOT NULL + SET DEFAULT for new rows.
                    cur.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN partner VARCHAR(32);"
                    )
                    cur.execute(
                        f"UPDATE {table_name} SET partner = 'shiprocket' WHERE partner IS NULL;"
                    )
                    cur.execute(
                        f"ALTER TABLE {table_name} ALTER COLUMN partner SET NOT NULL;"
                    )
                    cur.execute(
                        f"ALTER TABLE {table_name} ALTER COLUMN partner SET DEFAULT 'shiprocket';"
                    )
                    logger.info(f"Added partner column to {table_name}")

            # Bump unique constraints to include `partner` so Shiprocket and
            # Delhivery can dedupe independently for the same channel order id.
            #
            # We can't DROP by a hard-coded constraint name — that assumes
            # Postgres auto-generated the name we expected. If a tenant's old
            # constraint was renamed (manual migration, restore-from-dump),
            # the DROP-by-name silently no-ops and the legacy constraint
            # without `partner` survives, blocking cross-partner inserts on
            # the same (client_id, order_id, event_name) tuple. Find the old
            # constraint by its column list and drop by the *discovered* name.

            def _drop_unique_on_columns(
                table: str, expected_columns: List[str]
            ) -> None:
                """Drop every UNIQUE constraint on `table` whose column set
                exactly matches `expected_columns` (order-independent)."""
                cur.execute(
                    """
                    SELECT con.conname, array_agg(att.attname ORDER BY att.attname) AS cols
                    FROM pg_constraint con
                    JOIN pg_attribute att
                      ON att.attrelid = con.conrelid AND att.attnum = ANY(con.conkey)
                    WHERE con.conrelid = %s::regclass
                      AND con.contype = 'u'
                    GROUP BY con.conname
                    """,
                    (table,),
                )
                want = sorted(expected_columns)
                for row in cur.fetchall() or []:
                    name = row['conname'] if isinstance(row, dict) else row[0]
                    cols = row['cols'] if isinstance(row, dict) else row[1]
                    if sorted(cols or []) == want:
                        cur.execute(
                            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS \"{name}\""
                        )
                        logger.info(
                            f"Dropped legacy unique constraint {name} on {table} (columns: {cols})"
                        )

            def _has_unique(table: str, expected_columns: List[str]) -> bool:
                cur.execute(
                    """
                    SELECT array_agg(att.attname ORDER BY att.attname) AS cols
                    FROM pg_constraint con
                    JOIN pg_attribute att
                      ON att.attrelid = con.conrelid AND att.attnum = ANY(con.conkey)
                    WHERE con.conrelid = %s::regclass AND con.contype = 'u'
                    GROUP BY con.conname
                    """,
                    (table,),
                )
                want = sorted(expected_columns)
                for row in cur.fetchall() or []:
                    cols = row['cols'] if isinstance(row, dict) else row[0]
                    if sorted(cols or []) == want:
                        return True
                return False

            target = "shipment_events_client_id_partner_order_id_event_name_key"
            if not _has_unique("shipment_events", ["client_id", "partner", "order_id", "event_name"]):
                _drop_unique_on_columns("shipment_events", ["client_id", "order_id", "event_name"])
                cur.execute(
                    f"ALTER TABLE shipment_events ADD CONSTRAINT {target} "
                    "UNIQUE (client_id, partner, order_id, event_name)"
                )
                logger.info("Bumped shipment_events unique constraint to include partner")

            # Postgres truncates long constraint names to 63 chars; keep ours short enough.
            target = "event_dedup_client_partner_order_event_hash_key"
            if not _has_unique("event_deduplication", ["client_id", "partner", "order_id", "event_name", "event_hash"]):
                _drop_unique_on_columns(
                    "event_deduplication",
                    ["client_id", "order_id", "event_name", "event_hash"],
                )
                cur.execute(
                    f"ALTER TABLE event_deduplication ADD CONSTRAINT {target} "
                    "UNIQUE (client_id, partner, order_id, event_name, event_hash)"
                )
                logger.info("Bumped event_deduplication unique constraint to include partner")

            # Create indexes for better performance
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shipment_events_client_id ON shipment_events(client_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shipment_events_order_id ON shipment_events(order_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shipment_events_event_name ON shipment_events(event_name);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shipment_events_client_order ON shipment_events(client_id, order_id);")
            
            cur.execute("CREATE INDEX IF NOT EXISTS idx_event_deduplication_client_id ON event_deduplication(client_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_event_deduplication_order_id ON event_deduplication(order_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_event_deduplication_client_order ON event_deduplication(client_id, order_id);")
            
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shipment_status_history_client_id ON shipment_status_history(client_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shipment_status_history_order_id ON shipment_status_history(order_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shipment_status_history_awb ON shipment_status_history(awb);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_shipment_status_history_client_order ON shipment_status_history(client_id, order_id);")
            
            logger.info("ShipRocket event tables initialized successfully with multi-client support")
            
        except Exception as e:
            logger.error(f"Error creating tables: {e}")
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
    
    def _create_event_hash(self, event_data: Dict[str, Any]) -> str:
        """
        Create a hash of event data for deduplication.
        
        Args:
            event_data: The event data dictionary
            
        Returns:
            A hash string
        """
        import hashlib
        
        # Create a stable representation of the data
        stable_data = {
            'awb': event_data.get('awb', ''),
            'shipment_status': event_data.get('shipment_status', ''),
            'current_status': event_data.get('current_status', ''),
            'current_timestamp': event_data.get('current_timestamp', ''),
            'order_id': event_data.get('order_id', '')
        }
        
        # Convert to JSON string and hash
        data_str = json.dumps(stable_data, sort_keys=True)
        return hashlib.md5(data_str.encode()).hexdigest()
    
    async def atry_claim_event(self, order_id: str, event_name: str, event_data: Dict[str, Any], client_id: str = None) -> bool:
        """
        Atomically claim (client_id, partner='shiprocket', order_id, event_name, event_hash)
        for processing.

        The INSERT and the uniqueness check happen as a single atomic operation
        (INSERT ... ON CONFLICT DO NOTHING RETURNING id), so two near-simultaneous
        webhook deliveries for the same event can never both "win" the way a
        separate SELECT-then-INSERT could. Only the caller whose INSERT actually
        creates the row gets True; every other caller gets False and must skip
        sending.

        On a persistent DB error, fails open (returns True) so a broken database
        doesn't block all notification sends.
        """
        if client_id is None:
            client_id = await aresolve_client_id()

        event_hash = self._create_event_hash(event_data)

        for attempt in range(3):
            try:
                async with get_async_postgres_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            INSERT INTO event_deduplication (client_id, partner, order_id, event_name, event_hash)
                            VALUES (%s, 'shiprocket', %s, %s, %s)
                            ON CONFLICT (client_id, partner, order_id, event_name, event_hash) DO NOTHING
                            RETURNING id
                            """,
                            (client_id, order_id, event_name, event_hash),
                        )
                        claimed = await cur.fetchone() is not None
                        logger.info(f"[DEDUP-CLAIM] [CLIENT: {client_id}] order={order_id} event={event_name} claimed={claimed}")
                        return claimed
            except Exception as e:
                if is_transient_error(e) and attempt < 2:
                    logger.warning(f"🔄 Retry {attempt + 1}/2 atry_claim_event for client {client_id}: {e}")
                    continue
                logger.error(f"Error claiming event for client {client_id}: {e} — failing open (proceeding)")
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

        event_hash = self._create_event_hash(event_data)

        for attempt in range(3):
            try:
                async with get_async_postgres_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            DELETE FROM event_deduplication
                            WHERE client_id = %s AND partner = 'shiprocket'
                              AND order_id = %s AND event_name = %s AND event_hash = %s
                            """,
                            (client_id, order_id, event_name, event_hash),
                        )
                        logger.info(f"[DEDUP-RELEASE] [CLIENT: {client_id}] Released claim for order={order_id}, event={event_name}")
                        return True
            except Exception as e:
                if is_transient_error(e) and attempt < 2:
                    logger.warning(f"🔄 Retry {attempt + 1}/2 arelease_event_claim for client {client_id}: {e}")
                    continue
                logger.error(f"Error releasing event claim for client {client_id}: {e}")
                return False

        return False

    async def amark_event_processed(self, order_id: str, event_name: str, event_data: Dict[str, Any], client_id: str = None) -> bool:
        """Upsert the latest event snapshot. Deduplication itself is handled by atry_claim_event()."""
        if client_id is None:
            client_id = await aresolve_client_id()

        for attempt in range(3):
            try:
                async with get_async_postgres_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            INSERT INTO shipment_events (client_id, partner, order_id, awb, event_name, event_data)
                            VALUES (%s, 'shiprocket', %s, %s, %s, %s)
                            ON CONFLICT (client_id, partner, order_id, event_name) DO UPDATE SET
                                event_data = EXCLUDED.event_data,
                                processed_at = NOW()
                            """,
                            (client_id, order_id, event_data.get("awb", ""), event_name, json.dumps(event_data)),
                        )
                        logger.info(f"Event marked as processed for client {client_id}, order {order_id}, event {event_name}")
                        return True
            except Exception as e:
                if is_transient_error(e) and attempt < 2:
                    logger.warning(f"🔄 Retry {attempt + 1}/2 amark_event_processed for client {client_id}: {e}")
                    continue
                logger.error(f"Error marking event as processed for client {client_id}: {e}")
                return False

        return False

    async def amark_notification_sent(self, order_id: str, event_name: str, client_id: str = None) -> bool:
        if client_id is None:
            client_id = await aresolve_client_id()

        for attempt in range(3):
            try:
                async with get_async_postgres_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            UPDATE shipment_events
                            SET notification_sent = TRUE, notification_sent_at = NOW()
                            WHERE client_id = %s AND partner = 'shiprocket'
                              AND order_id = %s AND event_name = %s
                            """,
                            (client_id, order_id, event_name),
                        )
                        logger.debug(f"Notification marked as sent for client {client_id}, order {order_id}, event {event_name}")
                        return True
            except Exception as e:
                if is_transient_error(e) and attempt < 2:
                    logger.warning(f"🔄 Retry {attempt + 1}/2 amark_notification_sent for client {client_id}: {e}")
                    continue
                logger.error(f"Error marking notification sent for client {client_id}: {e}")
                return False

        return False

    async def acleanup_old_events(self, days_to_keep: int = 90) -> bool:
        for attempt in range(3):
            try:
                async with get_async_postgres_connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            DELETE FROM shipment_events
                            WHERE processed_at < NOW() - (%s * INTERVAL '1 day')
                            """,
                            (days_to_keep,),
                        )
                        await cur.execute(
                            """
                            DELETE FROM event_deduplication
                            WHERE processed_at < NOW() - (%s * INTERVAL '1 day')
                            """,
                            (days_to_keep,),
                        )
                        await cur.execute(
                            """
                            DELETE FROM shipment_status_history
                            WHERE created_at < NOW() - (%s * INTERVAL '1 day')
                            """,
                            (days_to_keep,),
                        )
                        return True
            except Exception as e:
                if is_transient_error(e) and attempt < 2:
                    logger.warning(f"🔄 Retry {attempt + 1}/2 acleanup_old_events: {e}")
                    continue
                logger.error(f"Error cleaning up old events: {e}")
                return False

        return False

    async def asave_shipment_status_batch(self, statuses: List[Dict[str, Any]], client_id: str = None) -> bool:
        if client_id is None:
            client_id = await aresolve_client_id()

        if not statuses:
            return True

        parsed_statuses = [
            (
                client_id,
                s.get("order_id"),
                s.get("awb"),
                s.get("status"),
                s.get("status_code"),
                s.get("location"),
                s.get("activity"),
                _parse_status_timestamp(s.get("timestamp")),
            )
            for s in statuses
        ]

        @awith_retry
        async def _do_insert():
            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.executemany(
                        """
                        INSERT INTO shipment_status_history
                        (client_id, partner, order_id, awb, status, status_code, location, activity, timestamp)
                        VALUES (%s, 'shiprocket', %s, %s, %s, %s, %s, %s, %s)
                        """,
                        parsed_statuses,
                    )

        try:
            await _do_insert()
            logger.debug(f"Batch saved {len(statuses)} shipment statuses for client {client_id}")
            return True
        except Exception as e:
            logger.error(f"Error saving shipment status batch for client {client_id}: {e}")
            return False
