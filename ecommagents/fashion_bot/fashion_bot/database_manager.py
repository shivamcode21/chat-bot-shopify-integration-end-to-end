"""
Database connection management with efficient connection pooling.

CRITICAL: Never hold a DB connection across:
- LLM calls
- HTTP calls  
- Webhooks
- Sleeps
- Retries
- Streaming responses

Always use context managers to ensure connections are released immediately:
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(...)
            # Connection released as soon as this block exits
"""
import asyncio
import inspect
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import logging
import time
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple, Generator, TYPE_CHECKING, List
from contextlib import asynccontextmanager, contextmanager
from fashion_bot.env_loader import bootstrap_environment, get_bool, get_env, get_float, get_int

try:
    from opentelemetry import metrics as _otel_metrics
    _db_meter = _otel_metrics.get_meter("fashion_bot.database_manager", "1.0.0")
    _db_connections_counter = _db_meter.create_counter(
        "db.connections.acquired",
        description="Postgres connection acquires (one per get_postgres_connection / get_async_postgres_connection use)",
        unit="1",
    )
    _db_connection_duration = _db_meter.create_histogram(
        "db.connection.duration",
        description="Wall-clock duration a Postgres connection was held (acquire to release)",
        unit="ms",
    )
except Exception:
    _db_connections_counter = None
    _db_connection_duration = None


def _record_db_connection(mode: str, status: str, duration_ms: float) -> None:
    """Record a DB connection acquire + release. Safe when OTel is not configured.

    Labels:
      - mode:   pool | direct  (pool = via psycopg_pool; direct = ad-hoc connection)
      - kind:   sync | async
      - status: ok | error
    """
    if _db_connections_counter is None:
        return
    labels = {"mode": mode, "status": status}
    try:
        _db_connections_counter.add(1, labels)
        _db_connection_duration.record(duration_ms, labels)
    except Exception:
        pass

# Import psycopg_pool for production-grade connection pooling
try:
    from psycopg_pool import AsyncConnectionPool, ConnectionPool
    POOL_AVAILABLE = True
except ImportError:
    try:
        from psycopg_pool import ConnectionPool
        AsyncConnectionPool = None
        POOL_AVAILABLE = True
    except ImportError:
        ConnectionPool = None
        AsyncConnectionPool = None
        POOL_AVAILABLE = False

logger = logging.getLogger(__name__)

bootstrap_environment()

# Database configuration
DATABASE_URL = get_env("DATABASE_URL")

# Pool configuration - TUNED FOR EFFICIENCY
# Lower min_size to avoid holding unnecessary connections
DB_POOL_MIN = get_int("DB_POOL_MIN", 5)  # Increased from 2
DB_POOL_MAX = get_int("DB_POOL_MAX", 30)  # Increased from 20
DB_POOL_TIMEOUT = get_float("DB_POOL_TIMEOUT", 5.0)  # Reduced from 30 to fail fast

# Environment variable to enable/disable pooling
# Set USE_DB_POOL=true to enable, false to disable (fallback to direct connections)
USE_DB_POOL = get_bool("USE_DB_POOL", False)

# Global pool instance (singleton)
_pool: Optional["ConnectionPool"] = None
_async_pool: Optional["AsyncConnectionPool"] = None
_pool_initialized = False
_async_pool_initialized = False
_consecutive_connection_errors = 0
_CONNECTION_ERROR_THRESHOLD = 3
_connection_acquire_counter = 0


def _record_connection_acquire() -> None:
    """Count one connection acquisition, process-wide and per-turn.

    The process-global counter stays for back-compat (`get_connection_acquire_count`);
    the per-turn tally is what `runtime_metrics` reports, because a global delta
    measures elapsed time rather than the turn's own work.
    """
    global _connection_acquire_counter
    _connection_acquire_counter += 1
    try:
        from fashion_bot.utils.turn_metrics import record_db_call
        record_db_call()
    except Exception:
        # Observability only — never fail a DB acquisition over a counter.
        pass


def reset_pool():
    """
    Force reset the connection pool.
    Called when repeated connection errors occur, indicating the pool may be poisoned.
    """
    global _pool, _pool_initialized, _consecutive_connection_errors
    
    if _pool:
        logger.warning("🔄 FORCING POOL RESET - closing all existing connections")
        try:
            _pool.close()
        except Exception as e:
            logger.error(f"Error closing pool during reset: {e}")
            
    _pool = None
    _pool_initialized = False
    _consecutive_connection_errors = 0
    logger.info("✅ Pool reset complete. Next request will initialize a fresh pool.")


async def reset_async_pool():
    """
    Force reset the async connection pool.
    Called when repeated async connection errors occur, indicating the pool may be poisoned.
    """
    global _async_pool, _async_pool_initialized

    if _async_pool:
        logger.warning("🔄 FORCING ASYNC POOL RESET - closing all existing async connections")
        try:
            await _async_pool.close()
        except Exception as e:
            logger.error(f"Error closing async pool during reset: {e}")

    _async_pool = None
    _async_pool_initialized = False
    logger.info("✅ Async pool reset complete. Next async request will initialize a fresh pool.")


def _check_connection(conn) -> bool:
    """
    Health check for pooled connections.
    Returns True if connection is healthy, False otherwise.
    """
    try:
        # Quick ping to verify connection is alive
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
        return True
    except Exception as e:
        logger.warning(f"🔴 Connection health check failed: {e}")
        return False


# Connection recycling settings
# Max lifetime: Recycle connections after this many seconds (5 minutes)
DB_POOL_MAX_LIFETIME = get_int("DB_POOL_MAX_LIFETIME", 300)
# Max idle: Close connections idle for this many seconds (30 seconds - shorter to avoid SSL termination)  
DB_POOL_MAX_IDLE = get_int("DB_POOL_MAX_IDLE", 30)
# Reconnect timeout: How long to wait for reconnection
DB_POOL_RECONNECT_TIMEOUT = get_float("DB_POOL_RECONNECT_TIMEOUT", 5.0)
# TCP keepalives reduce stale SSL connections from Neon/proxy/NAT idle closes.
# DB_KEEPALIVES: Enables OS-level TCP keepalive probes for PostgreSQL sockets.
# DB_KEEPALIVES_IDLE: Seconds a connection can sit idle before the first keepalive probe is sent.
# DB_KEEPALIVES_INTERVAL: Seconds between keepalive probes after the idle period starts.
# DB_KEEPALIVES_COUNT: Number of failed keepalive probes before the OS treats the socket as dead.
DB_KEEPALIVES = get_int("DB_KEEPALIVES", 1)
DB_KEEPALIVES_IDLE = get_int("DB_KEEPALIVES_IDLE", 30)
DB_KEEPALIVES_INTERVAL = get_int("DB_KEEPALIVES_INTERVAL", 5)
DB_KEEPALIVES_COUNT = get_int("DB_KEEPALIVES_COUNT", 3)


def _db_connection_kwargs() -> Dict[str, Any]:
    return {
        "autocommit": True,
        "row_factory": dict_row,
        "keepalives": DB_KEEPALIVES,
        "keepalives_idle": DB_KEEPALIVES_IDLE,
        "keepalives_interval": DB_KEEPALIVES_INTERVAL,
        "keepalives_count": DB_KEEPALIVES_COUNT,
    }


def get_pool() -> Optional["ConnectionPool"]:
    """
    Get or initialize the database connection pool (Singleton).
    Returns None if pooling is disabled or unavailable.
    """
    global _pool, _pool_initialized
    
    if _pool_initialized:
        return _pool
    
    _pool_initialized = True
    
    if not POOL_AVAILABLE:
        logger.warning("⚠️ psycopg_pool NOT available. Install with: pip install psycopg-pool")
        return None
    
    if not DATABASE_URL:
        logger.error("❌ DATABASE_URL not set. Cannot initialize pool.")
        return None
    
    try:
        # DB_POOL_HEALTH_CHECK: When true, psycopg runs a lightweight SELECT 1
        # before handing out a pooled connection. Keep this opt-in because it
        # adds an extra DB round-trip on every pool acquire and can amplify load
        # during pool pressure. Prefer TCP keepalives + max_idle for normal stale
        # SSL protection; enable this only when diagnosing stale pooled sockets.
        USE_HEALTH_CHECK = get_bool("DB_POOL_HEALTH_CHECK", False)
        
        logger.info(
            f"🚀 Initializing DB Pool: min={DB_POOL_MIN}, max={DB_POOL_MAX}, timeout={DB_POOL_TIMEOUT}s, "
            f"max_lifetime={DB_POOL_MAX_LIFETIME}s, max_idle={DB_POOL_MAX_IDLE}s, "
            f"health_check={USE_HEALTH_CHECK}, keepalives_idle={DB_KEEPALIVES_IDLE}s"
        )
        _pool = ConnectionPool(
            conninfo=DATABASE_URL,
            min_size=DB_POOL_MIN,
            max_size=DB_POOL_MAX,
            timeout=DB_POOL_TIMEOUT,
            open=True,
            name="fashion_bot_pool",
            # Health check - ALWAYS validate connection before returning from pool
            check=ConnectionPool.check_connection if USE_HEALTH_CHECK else None,
            # Max connection lifetime - recycle connections after this many seconds
            max_lifetime=DB_POOL_MAX_LIFETIME,
            # Max idle time - close connections idle for this long (shorter = fewer stale connections)
            max_idle=DB_POOL_MAX_IDLE,
            # Reconnect timeout
            reconnect_timeout=DB_POOL_RECONNECT_TIMEOUT,
            # Production settings
            kwargs=_db_connection_kwargs()
        )
        logger.info(f"✅ DB Pool initialized {'WITH' if USE_HEALTH_CHECK else 'WITHOUT'} health checks")
        return _pool
    except Exception as e:
        logger.error(f"❌ Failed to initialize DB Pool: {e}")
        return None


async def get_async_pool() -> Optional["AsyncConnectionPool"]:
    """
    Get or initialize the async database connection pool (Singleton).
    Returns None if pooling is disabled or unavailable.

    No asyncio.Lock guarding the lazy init — the lock would bind to the
    first loop that touched it and break any later loop. With
    AsyncIOScheduler all crons share the API event loop, so the worst
    case here is a benign double-init race on cold start where one of two
    concurrent first-callers wins the assignment and the other's
    half-built pool is discarded by GC.
    """
    global _async_pool, _async_pool_initialized

    if _async_pool_initialized:
        return _async_pool

    _async_pool_initialized = True

    if AsyncConnectionPool is None:
        logger.warning("⚠️ psycopg_pool AsyncConnectionPool NOT available. Install with: pip install psycopg-pool")
        return None

    if not DATABASE_URL:
        logger.error("❌ DATABASE_URL not set. Cannot initialize async pool.")
        return None

    try:
        logger.info(
            f"🚀 Initializing Async DB Pool: min={DB_POOL_MIN}, max={DB_POOL_MAX}, "
            f"timeout={DB_POOL_TIMEOUT}s, max_lifetime={DB_POOL_MAX_LIFETIME}s, "
            f"max_idle={DB_POOL_MAX_IDLE}s, keepalives_idle={DB_KEEPALIVES_IDLE}s"
        )
        _stmt_timeout_ms = get_int("DB_STATEMENT_TIMEOUT_MS", 10000)

        async def _configure_conn(conn):
            await conn.execute(f"SET statement_timeout = {_stmt_timeout_ms}")

        # DB_POOL_HEALTH_CHECK: Mirrors the sync pool. When enabled, psycopg
        # runs SELECT 1 before every async acquire. Leave off by default because
        # high-throughput async paths can turn that pre-ping into meaningful DB
        # load; keepalives and max_idle handle the normal stale-socket case.
        _use_health_check = get_bool("DB_POOL_HEALTH_CHECK", False)

        _async_pool = AsyncConnectionPool(
            conninfo=DATABASE_URL,
            min_size=DB_POOL_MIN,
            max_size=DB_POOL_MAX,
            timeout=DB_POOL_TIMEOUT,
            open=False,
            name="fashion_bot_async_pool",
            check=AsyncConnectionPool.check_connection if _use_health_check else None,
            max_lifetime=DB_POOL_MAX_LIFETIME,
            max_idle=DB_POOL_MAX_IDLE,
            reconnect_timeout=DB_POOL_RECONNECT_TIMEOUT,
            kwargs=_db_connection_kwargs(),
            configure=_configure_conn,
        )
        await _async_pool.open()
        logger.info(
            f"✅ Async DB Pool initialized {'WITH' if _use_health_check else 'WITHOUT'} health checks"
        )
        return _async_pool
    except Exception as e:
        logger.error(f"❌ Failed to initialize async DB Pool: {e}")
        _async_pool = None
        return None


def get_pool_stats() -> Optional[Dict[str, Any]]:
    """
    Get current pool statistics for diagnostics.
    
    Returns:
        dict with pool stats, or None if pool not available
    """
    pool = get_pool()
    if not pool:
        return None
    
    try:
        stats = pool.get_stats()
        return {
            "pool_size": stats.get('pool_size', 0),
            "pool_available": stats.get('pool_available', 0),
            "pool_max": DB_POOL_MAX,
            "requests_waiting": stats.get('requests_waiting', 0),
            "usage_percent": round((stats.get('pool_size', 0) / DB_POOL_MAX) * 100, 1) if DB_POOL_MAX > 0 else 0
        }
    except Exception as e:
        logger.warning(f"Failed to get pool stats: {e}")
        return None


def _get_pool_stats_str(pool) -> str:
    """Get formatted pool stats string for logging."""
    try:
        stats = pool.get_stats()
        return f"[Pool: {stats.get('pool_size', '?')}/{DB_POOL_MAX} total, {stats.get('pool_available', '?')} available, {stats.get('requests_waiting', 0)} waiting]"
    except Exception:
        return "[Pool: stats unavailable]"


class PooledConnectionProxy:
    """
    Proxy that ensures pooled connections are ALWAYS returned to pool.
    Works as a context manager and handles cleanup on close().
    
    CRITICAL: If a connection is dead/broken, it is NOT returned to the pool.
    This prevents cascading failures from stale connections.
    """
    __slots__ = ('_conn', '_pool', '_closed', '_conn_id', '_is_broken', '_caller', '_acquired_at')
    
    _counter = 0  # Class-level counter for connection IDs
    
    def __init__(self, conn, pool):
        import time
        self._conn = conn
        self._pool = pool
        self._closed = False
        self._is_broken = False  # Track if connection is broken
        self._acquired_at = time.time()  # Track when connection was acquired
        PooledConnectionProxy._counter += 1
        self._conn_id = PooledConnectionProxy._counter
        
        # Capture caller for debugging
        import traceback
        stack = traceback.extract_stack()
        # Get the caller (skip internal frames)
        caller_info = "unknown"
        for frame in reversed(stack[:-3]):  # Skip last 3 frames (internal)
            if 'database_manager' not in frame.filename:
                caller_info = f"{frame.filename.split('/')[-1]}:{frame.lineno} in {frame.name}"
                break
        self._caller = caller_info
        
        # Log acquisition with pool stats and caller
        stats_str = _get_pool_stats_str(pool)
        logger.info(f"🔌 POOL ACQUIRED #{self._conn_id} by [{self._caller}] {stats_str}")

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Mark as broken if there was an exception that indicates connection issues
        if exc_type is not None:
            exc_str = str(exc_val).lower() if exc_val else ""
            if any(term in exc_str for term in ['ssl', 'connection', 'closed', 'lost', 'broken', 'server closed']):
                self._is_broken = True
                logger.warning(f"🔴 POOL #{self._conn_id}: Marking connection as broken due to: {exc_val}")
        self._release()
        return False  # Don't suppress exceptions

    def mark_broken(self):
        """Explicitly mark connection as broken - will not be returned to pool."""
        self._is_broken = True
        logger.warning(f"🔴 POOL #{self._conn_id}: Connection explicitly marked as broken")

    def _is_connection_alive(self) -> bool:
        """Quick check if connection is still alive."""
        try:
            # Check if connection is in a valid state
            if self._conn.closed:
                return False
            # Check transaction status - if it's UNKNOWN, connection is likely dead
            status = self._conn.info.transaction_status
            if status == psycopg.pq.TransactionStatus.UNKNOWN:
                return False
            return True
        except Exception:
            return False

    def _release(self):
        """Release connection back to pool. Pool will detect and handle broken connections."""
        if self._closed:
            return
        self._closed = True
        
        import time
        held_ms = int((time.time() - self._acquired_at) * 1000)
        
        try:
            # Check if connection is broken
            is_broken = self._is_broken or not self._is_connection_alive()
            
            if is_broken:
                logger.warning(f"🔴 POOL #{self._conn_id}: Connection is broken, will be replaced by pool")
            else:
                # Connection is alive - ensure clean state before returning to pool
                if self._conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                    logger.warning(f"🔄 POOL #{self._conn_id}: Rolling back uncommitted transaction before release")
                    try:
                        self._conn.rollback()
                    except Exception as e:
                        logger.warning(f"🔴 POOL #{self._conn_id}: Rollback failed: {e}")
                        is_broken = True
            
            # ALWAYS return to pool - pool will detect broken connections and replace them
            # This is critical: if we don't call putconn(), the pool loses track of this connection
            self._pool.putconn(self._conn)
            
            stats_str = _get_pool_stats_str(self._pool)
            if is_broken:
                logger.info(f"🔌 POOL RETURNED #{self._conn_id} (broken, will be replaced) {stats_str}")
            elif held_ms > 100:
                logger.warning(f"🔌 POOL RELEASED #{self._conn_id} SLOW ({held_ms}ms) by [{self._caller}] {stats_str}")
            else:
                logger.info(f"🔌 POOL RELEASED #{self._conn_id} ({held_ms}ms) {stats_str}")
            
        except Exception as e:
            # Even on error, try to return connection to pool so it can track it
            logger.warning(f"⚠️ POOL #{self._conn_id}: Release error: {e}")
            try:
                self._pool.putconn(self._conn)
            except Exception:
                # If putconn also fails, connection is truly lost - log it
                logger.error(f"❌ POOL #{self._conn_id}: Failed to return to pool, connection LEAKED")

    def close(self):
        """Manual close - releases to pool (doesn't actually close)."""
        self._release()

    def cursor(self, *args, **kwargs):
        """Get cursor with dict_row factory by default."""
        if 'row_factory' not in kwargs:
            kwargs['row_factory'] = dict_row
        return self._conn.cursor(*args, **kwargs)

    def transaction(self):
        """Support explicit transactions."""
        return self._conn.transaction()


_direct_conn_counter = 0

def _get_connection_internal():
    """
    Internal function to get a raw connection.
    Returns (connection_object, is_pooled) tuple.
    
    IMPORTANT: Does NOT fall back to direct connections.
    If pool fails, it raises the exception and lets @with_retry handle it.
    This ensures the pool can recover (health check discards bad connections).
    """
    global _direct_conn_counter
    
    if USE_DB_POOL:
        pool = get_pool()
        if pool:
            # Log pool state BEFORE trying to get connection
            try:
                stats = pool.get_stats()
                pool_size = stats.get('pool_size', 0)
                pool_available = stats.get('pool_available', 0)
                requests_waiting = stats.get('requests_waiting', 0)
                
                # Warn if pool is nearly exhausted
                if pool_available == 0:
                    logger.warning(f"⚠️ POOL PRESSURE: 0 available connections! [total={pool_size}/{DB_POOL_MAX}, waiting={requests_waiting}]")
                elif pool_available <= 2:
                    logger.info(f"🟡 Pool low: {pool_available} available [total={pool_size}/{DB_POOL_MAX}, waiting={requests_waiting}]")
            except Exception:
                pass
            
            import time
            start_time = time.time()
            
            # Let exceptions bubble up - the @with_retry decorator will handle them
            # This allows the pool's health check to do its job (discard bad connections)
            conn = pool.getconn()
            _record_connection_acquire()

            elapsed_ms = (time.time() - start_time) * 1000
            if elapsed_ms > 100:  # Log if getting connection took > 100ms
                logger.warning(f"🐌 SLOW POOL GETCONN: took {elapsed_ms:.0f}ms (health check may be slow)")
            return PooledConnectionProxy(conn, pool), True
    
    # Direct connection - only when pool is explicitly disabled or unavailable
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL not set")
    
    _direct_conn_counter += 1
    _record_connection_acquire()
    logger.info(f"🔌 DIRECT CONNECTION #{_direct_conn_counter} created (pool disabled)")
    conn = psycopg.connect(DATABASE_URL, **_db_connection_kwargs())
    return conn, False


def get_connection_acquire_count() -> int:
    """
    Total number of DB connection acquisitions (pooled + direct) since process start.
    Used for per-request DB call approximation via deltas.
    """
    return _connection_acquire_counter


def _is_connection_error(exc: Exception) -> bool:
    """Check if exception is a connection/SSL error that should trigger retry."""
    error_keywords = ['ssl', 'connection', 'closed', 'lost', 'broken', 'server closed', 'consuming input failed']
    exc_str = str(exc).lower()
    return any(keyword in exc_str for keyword in error_keywords)


# Public alias for call-sites that need to decide whether to re-raise (so the
# @awith_retry / @with_retry decorator can engage) vs swallow as application
# error. Keep `_is_connection_error` for backwards compatibility.
is_connection_error = _is_connection_error


def _is_pool_acquire_timeout(exc: Exception) -> bool:
    """
    Pool acquire timeout means the pool is under pressure, not necessarily poisoned.

    Retrying with backoff is useful, but force-resetting healthy connections during
    pressure can amplify the outage by closing connections that are actively in use.
    """
    exc_str = str(exc).lower()
    return "couldn't get a connection after" in exc_str or "could not get a connection after" in exc_str


class ConnectionContextManager:
    """
    Dual-mode connection that works both as:
    1. Context manager: with get_postgres_connection() as conn:
    2. Direct call: conn = get_postgres_connection(); ... conn.close()
    
    Automatically retries on connection errors (SSL closed, etc.)
    """
    __slots__ = ('_conn', '_is_pooled', '_closed', '_max_retries', '_t0', '_status')
    
    def __init__(self, max_retries: int = 2):
        self._max_retries = max_retries
        self._t0 = time.monotonic()
        self._status = "ok"
        self._conn, self._is_pooled = _get_connection_internal()
        self._closed = False
    
    def __enter__(self):
        return self._conn
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self._status = "error"
        self._cleanup()
        return False
    
    def __getattr__(self, name):
        # Allow direct use: conn = get_postgres_connection(); conn.cursor()
        return getattr(self._conn, name)
    
    def _cleanup(self):
        if self._closed:
            return
        self._closed = True
        try:
            if hasattr(self._conn, 'close'):
                self._conn.close()
        except Exception:
            pass
        try:
            mode = "pool" if self._is_pooled else "direct"
            _record_db_connection(
                mode=mode,
                status=self._status,
                duration_ms=(time.monotonic() - self._t0) * 1000.0,
            )
        except Exception:
            pass
    
    def close(self):
        """Manual close for backward compatibility."""
        self._cleanup()
    
    def cursor(self, *args, **kwargs):
        if 'row_factory' not in kwargs:
            kwargs['row_factory'] = dict_row
        return self._conn.cursor(*args, **kwargs)
    
    def transaction(self):
        return self._conn.transaction()


def with_retry(func):
    """
    Decorator to retry database operations on connection errors.
    Implements exponential backoff + jitter and pool reset on repeated failures.
    
    Usage:
        @with_retry
        def my_db_operation():
            with get_postgres_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(...)
                    return cur.fetchone()
    """
    import functools
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        global _consecutive_connection_errors
        max_retries = 3
        last_error = None
        base_delay = 0.2
        
        for attempt in range(max_retries + 1):
            try:
                result = func(*args, **kwargs)
                # Success! Reset error counter
                _consecutive_connection_errors = 0
                return result
            except Exception as e:
                last_error = e
                if _is_connection_error(e):
                    pool_acquire_timeout = _is_pool_acquire_timeout(e)
                    if not pool_acquire_timeout:
                        _consecutive_connection_errors += 1
                    
                    # If we've hit multiple connection errors across any calls, reset the pool
                    if not pool_acquire_timeout and _consecutive_connection_errors >= _CONNECTION_ERROR_THRESHOLD:
                        reset_pool()
                        # Extra pause after pool reset to allow DB/network to stabilize
                        time.sleep(1.0)
                    
                    if attempt < max_retries:
                        # Exponential backoff: 0.2s, 0.4s, 0.8s
                        delay = base_delay * (2 ** attempt)
                        # Cap it at 3 seconds
                        delay = min(delay, 3.0)
                        # Add jitter (0.7x to 1.3x)
                        delay *= random.uniform(0.7, 1.3)
                        
                        if pool_acquire_timeout:
                            logger.warning(f"🔄 DB pool acquire timeout on attempt {attempt + 1}, retrying in {delay:.2f}s: {e}")
                        else:
                            logger.warning(f"🔄 Connection error on attempt {attempt + 1}, retrying in {delay:.2f}s (Errors: {_consecutive_connection_errors}): {e}")
                        time.sleep(delay)
                        continue
                raise
        
        raise last_error
    
    return wrapper


def awith_retry(func):
    """
    Async decorator to retry database operations on connection errors.

    Mirrors `with_retry` but uses asyncio-friendly backoff and async pool reset.
    """
    import functools

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        global _consecutive_connection_errors
        max_retries = 3
        last_error = None
        base_delay = 0.2

        for attempt in range(max_retries + 1):
            try:
                result = await func(*args, **kwargs)
                _consecutive_connection_errors = 0
                return result
            except Exception as e:
                last_error = e
                if _is_connection_error(e):
                    pool_acquire_timeout = _is_pool_acquire_timeout(e)
                    if not pool_acquire_timeout:
                        _consecutive_connection_errors += 1

                    if not pool_acquire_timeout and _consecutive_connection_errors >= _CONNECTION_ERROR_THRESHOLD:
                        await reset_async_pool()
                        await asyncio.sleep(1.0)

                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt)
                        delay = min(delay, 3.0)
                        delay *= random.uniform(0.7, 1.3)
                        if pool_acquire_timeout:
                            logger.warning(
                                f"🔄 Async DB pool acquire timeout on attempt {attempt + 1}, retrying in {delay:.2f}s: {e}"
                            )
                        else:
                            logger.warning(
                                f"🔄 Async connection error on attempt {attempt + 1}, retrying in {delay:.2f}s "
                                f"(Errors: {_consecutive_connection_errors}): {e}"
                            )
                        await asyncio.sleep(delay)
                        continue
                raise

        raise last_error

    return wrapper


def get_postgres_connection() -> ConnectionContextManager:
    """
    Get a database connection.
    
    Works both ways:
    
    1. Context manager (RECOMMENDED - auto-closes):
        with get_postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
    
    2. Direct call (must close manually):
        conn = get_postgres_connection()
        try:
            cur = conn.cursor()
            cur.execute(...)
        finally:
            conn.close()
    
    When USE_DB_POOL=true: Uses connection pooling
    When USE_DB_POOL=false: Creates direct connections
    """
    return ConnectionContextManager()


DB_RETRY_MAX = get_int("DB_RETRY_MAX", 3)
DB_RETRY_BASE_DELAY = get_float("DB_RETRY_BASE_DELAY", 0.2)


async def async_db_execute_with_retry(fn, *, max_retries: int = None, base_delay: float = None):
    """
    Execute an async DB operation with exponential backoff retries.

    Args:
        fn: Async callable that performs the DB operation.
            Will be called with no arguments — callers should use a closure or partial.
        max_retries: Number of retries (default DB_RETRY_MAX=3).
        base_delay: Initial delay in seconds (default 0.2). Doubles each retry.

    Returns:
        The result of fn().

    Raises:
        The last exception if all retries are exhausted.
    """
    retries = max_retries if max_retries is not None else DB_RETRY_MAX
    delay = base_delay if base_delay is not None else DB_RETRY_BASE_DELAY
    last_err = None

    for attempt in range(1 + retries):
        try:
            return await fn()
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = delay * (2 ** attempt)
                logger.warning(
                    f"⚠️ DB operation failed (attempt {attempt + 1}/{1 + retries}), "
                    f"retrying in {wait:.1f}s: {e}"
                )
                await asyncio.sleep(wait)
            else:
                logger.error(f"❌ DB operation failed after {1 + retries} attempts: {e}")
    raise last_err


@asynccontextmanager
async def get_async_postgres_connection():
    """
    Get an async database connection.

    When USE_DB_POOL=true and AsyncConnectionPool is available: Uses async connection pooling
    When USE_DB_POOL=false or async pool is unavailable: Creates direct async connections
    """
    global _direct_conn_counter

    t0 = time.monotonic()
    mode = "pool"
    status = "ok"

    try:
        if USE_DB_POOL:
            pool = await get_async_pool()
            if pool:
                async with pool.connection() as conn:
                    _record_connection_acquire()
                    yield conn
                    return

        if not DATABASE_URL:
            raise ValueError("DATABASE_URL not set")

        mode = "direct"
        _direct_conn_counter += 1
        _record_connection_acquire()
        logger.info(f"🔌 DIRECT ASYNC CONNECTION #{_direct_conn_counter} created (pool disabled)")
        conn = await psycopg.AsyncConnection.connect(
            DATABASE_URL,
            **_db_connection_kwargs(),
        )
        try:
            yield conn
        finally:
            close_result = conn.close()
            if inspect.isawaitable(close_result):
                await close_result
    except Exception:
        status = "error"
        raise
    finally:
        _record_db_connection(mode=mode, status=status, duration_ms=(time.monotonic() - t0) * 1000.0)


class AutoClosingCursor:
    """
    Wrapper that auto-closes connection when cursor is closed.
    For backward compatibility with: conn, cur = get_postgres_cursor()
    """
    __slots__ = ('_conn', '_cur', '_closed')
    
    def __init__(self, conn, cur):
        self._conn = conn
        self._cur = cur
        self._closed = False
    
    def __getattr__(self, name):
        return getattr(self._cur, name)
    
    def __iter__(self):
        return iter(self._cur)
    
    def close(self):
        """Close cursor AND release connection."""
        if self._closed:
            return
        self._closed = True
        try:
            self._cur.close()
        except Exception:
            pass
        try:
            self._conn.close()  # This releases to pool if pooled
        except Exception:
            pass


def get_postgres_cursor() -> Tuple[Any, Any]:
    """
    Get a database connection and cursor.
    
    IMPORTANT: Always close both when done!
    
    Recommended pattern (context manager):
        with get_postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
    
    Legacy pattern (manual close required):
        conn, cur = get_postgres_cursor()
        try:
            cur.execute(...)
        finally:
            cur.close()
            conn.close()
    
    Returns:
        Tuple of (connection, cursor) - cursor auto-closes connection when closed
    """
    # Get connection (works with both pool and direct)
    if USE_DB_POOL:
        pool = get_pool()
        if pool:
            try:
                raw_conn = pool.getconn()
                conn = PooledConnectionProxy(raw_conn, pool)
                cur = conn.cursor(row_factory=dict_row)
                # Wrap cursor to auto-close connection
                auto_cur = AutoClosingCursor(conn, cur)
                return conn, auto_cur
            except Exception as e:
                logger.warning(f"⚠️ Pool error: {e}. Using direct connection.")
    
    # Direct connection fallback
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL not set")
    
    conn = psycopg.connect(DATABASE_URL, **_db_connection_kwargs())
    cur = conn.cursor()
    auto_cur = AutoClosingCursor(conn, cur)
    return conn, auto_cur


# ==================== DIRECT CONNECTION FOR CRON JOBS ====================

def get_direct_postgres_cursor():
    """
    Get a DIRECT database cursor (bypasses connection pool).
    
    USE THIS FOR:
    - Cron jobs
    - Background tasks
    - Long-running operations
    - Any operation that shouldn't compete with user traffic for pool slots
    
    IMPORTANT: Always close the connection when done!
    
    Usage:
        conn, cur = get_direct_postgres_cursor()
        try:
            cur.execute(...)
            # do work
        finally:
            if cur: cur.close()
            if conn: conn.close()
    
    Returns:
        Tuple of (connection, cursor)
    """
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL not set")
    
    global _direct_conn_counter
    _direct_conn_counter += 1
    
    logger.info(f"🔌 DIRECT (CRON) CONNECTION #{_direct_conn_counter} created")
    conn = psycopg.connect(DATABASE_URL, **_db_connection_kwargs())
    cur = conn.cursor()
    
    return conn, cur


# ==================== CHECKPOINTING ====================

class PostgresSaver:
    """
    Minimal Postgres checkpointer for LangGraph.
    Uses connection pooling efficiently - no held connections.
    """
    
    def __init__(self, conn=None):
        """
        Initialize checkpointer.
        Note: Table creation is lazy - happens on first save/load.
        """
        self._legacy_conn = conn
        self._table_ensured = False
        
        # LangGraph interface requirement
        self.config_specs = []
        try:
            from langchain_core.runnables import ConfigurableFieldSpec
            self.config_specs = [
                ConfigurableFieldSpec(
                    id="thread_id", 
                    annotation=str, 
                    name="Thread ID", 
                    is_shared=True
                ),
            ]
        except ImportError:
            pass

    def _ensure_table(self):
        """Lazy table creation - only when needed."""
        if self._table_ensured:
            return
        
        try:
            with get_postgres_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS checkpoints (
                            id SERIAL PRIMARY KEY,
                            key TEXT UNIQUE,
                            state JSONB,
                            metadata JSONB,
                            created_at TIMESTAMPTZ DEFAULT NOW()
                        );
                    """)
            self._table_ensured = True
        except Exception as e:
            logger.warning(f"Checkpoint table setup warning: {e}")

    # LangGraph Interface (minimal implementation)
    def get_tuple(self, config, **kwargs):
        return None

    def put(self, config, checkpoint, metadata, **kwargs):
        return config

    def get_next_version(self, version, item):
        return str(int(version or "0") + 1)

    # Custom save/load methods
    def save(self, key: str, state: dict):
        """Save state to checkpoint table."""
        self._ensure_table()
        try:
            with get_postgres_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO checkpoints (key, state)
                        VALUES (%s, %s)
                        ON CONFLICT (key) DO UPDATE SET state = EXCLUDED.state;
                    """, (key, Jsonb(state)))
        except Exception as e:
            logger.error(f"Checkpoint save error: {e}")

    def load(self, key: str) -> Optional[dict]:
        """Load state from checkpoint table."""
        self._ensure_table()
        try:
            with get_postgres_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT state FROM checkpoints WHERE key = %s;", (key,))
                    row = cur.fetchone()
                    return row['state'] if row else None
        except Exception as e:
            logger.error(f"Checkpoint load error: {e}")
            return None
