from __future__ import annotations

import logging
import threading
from typing import Awaitable, Callable, Dict, Optional, Tuple, TypeVar, Union

from cachetools import TTLCache

from fashion_bot.env_loader import get_bool, get_int

T = TypeVar("T")
_MISS = object()
logger = logging.getLogger(__name__)
LOG_TIERED_CACHE_READS = get_bool("LOG_TIERED_CACHE_READS", True)
LOG_TIERED_CACHE_MEMORY_HITS = get_bool("LOG_TIERED_CACHE_MEMORY_HITS", False)

# ── In-process counters (readable, unlike OTel) ────────────────────────────────
# GIL protects simple int increments; no lock needed.
_cache_stats: Dict[str, int] = {
    "memory_hits": 0,
    "redis_hits": 0,
    "db_hits": 0,
    "misses": 0,
}


def get_cache_stats() -> Dict[str, int]:
    """Return a snapshot of cache hit/miss counters."""
    return dict(_cache_stats)


def _inc_cache_stat(tier: str) -> None:
    _cache_stats[tier] = _cache_stats.get(tier, 0) + 1

# OpenTelemetry cache metrics (no-op when OTel is not initialized)
try:
    from opentelemetry import metrics as _otel_metrics
    _meter = _otel_metrics.get_meter("fashion_bot.tiered_cache", "1.0.0")
    _cache_reads_counter = _meter.create_counter(
        "cache_reads_total",
        description="Tiered cache read operations by source tier and key prefix",
        unit="1",
    )
except Exception:
    _cache_reads_counter = None


def _record_cache_read(cache_key: str, tier: str) -> None:
    """Increment OTel counter for cache read. Safe to call even if OTel is not configured."""
    if _cache_reads_counter is not None:
        prefix = cache_key.split(":")[0] if ":" in cache_key else "unknown"
        _cache_reads_counter.add(1, {"tier": tier, "key_prefix": prefix})


class InMemoryTTLCache:
    """
    Process-local best-effort cache.
    Uses standard `cachetools.TTLCache` internally.
    """

    def __init__(self, max_entries: Optional[int] = None):
        self.max_entries = max_entries if max_entries is not None else get_int("IN_MEMORY_CACHE_MAX_ENTRIES", 2000)
        self._lock = threading.Lock()
        self._buckets: Dict[int, TTLCache] = {}
        self._key_to_ttl: Dict[str, int] = {}

    def get(self, key: str):
        with self._lock:
            ttl_seconds = self._key_to_ttl.get(key)
            if ttl_seconds is None:
                return _MISS
            cache = self._buckets.get(ttl_seconds)
            if cache is None:
                self._key_to_ttl.pop(key, None)
                return _MISS
            value = cache.get(key, _MISS)
            if value is _MISS:
                self._key_to_ttl.pop(key, None)
            return value

    def set(self, key: str, value, ttl_seconds: int) -> None:
        ttl_seconds = max(1, int(ttl_seconds))
        with self._lock:
            if key in self._key_to_ttl:
                old_ttl = self._key_to_ttl[key]
                old_bucket = self._buckets.get(old_ttl)
                if old_bucket is not None:
                    old_bucket.pop(key, None)
            bucket = self._buckets.get(ttl_seconds)
            if bucket is None:
                bucket = TTLCache(maxsize=self.max_entries, ttl=ttl_seconds)
                self._buckets[ttl_seconds] = bucket
            bucket[key] = value
            self._key_to_ttl[key] = ttl_seconds

    def delete(self, key: str) -> None:
        with self._lock:
            ttl_seconds = self._key_to_ttl.pop(key, None)
            if ttl_seconds is not None:
                bucket = self._buckets.get(ttl_seconds)
                if bucket is not None:
                    bucket.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        removed = 0
        with self._lock:
            keys = [k for k in self._key_to_ttl.keys() if k.startswith(prefix)]
            for key in keys:
                ttl_seconds = self._key_to_ttl.pop(key, None)
                if ttl_seconds is None:
                    continue
                bucket = self._buckets.get(ttl_seconds)
                if bucket is not None:
                    bucket.pop(key, None)
                removed += 1
        return removed

    def clear(self) -> None:
        with self._lock:
            self._key_to_ttl.clear()
            self._buckets.clear()


_GLOBAL_MEMORY_CACHE = InMemoryTTLCache()


def get_with_tiered_cache(
    *,
    cache_key: str,
    ttl_seconds: int = 600,
    load_from_source_fn: Callable[[], Optional[T]],
    get_from_redis_fn: Optional[Callable[[], Optional[T]]] = None,
    set_to_redis_fn: Optional[Callable[[T], None]] = None,
    cache_none: bool = False,
) -> Tuple[Optional[T], str]:
    """
    Transparent tiered read flow:
    memory -> redis -> source (db/api) and backfill upper tiers.

    Returns:
      (value, source_tier) where source_tier in {"memory","redis","source","none"}.
    """
    mem_val = _GLOBAL_MEMORY_CACHE.get(cache_key)
    if mem_val is not _MISS:
        if LOG_TIERED_CACHE_READS and LOG_TIERED_CACHE_MEMORY_HITS:
            logger.debug(f"[TIERED_CACHE] key={cache_key} source=memory")
        _record_cache_read(cache_key, "memory")
        _inc_cache_stat("memory_hits")
        return mem_val, "memory"

    if get_from_redis_fn is not None:
        try:
            redis_val = get_from_redis_fn()
            if redis_val is not None:
                _GLOBAL_MEMORY_CACHE.set(cache_key, redis_val, ttl_seconds)
                if LOG_TIERED_CACHE_READS:
                    logger.debug(f"[TIERED_CACHE] key={cache_key} source=redis")
                _record_cache_read(cache_key, "redis")
                _inc_cache_stat("redis_hits")
                return redis_val, "redis"
        except Exception as err:
            logger.debug(f"[TIERED_CACHE] key={cache_key} source=redis error={err}")
            pass

    src_val = load_from_source_fn()
    if src_val is None and not cache_none:
        if LOG_TIERED_CACHE_READS:
            logger.debug(f"[TIERED_CACHE] key={cache_key} source=none")
        _record_cache_read(cache_key, "none")
        _inc_cache_stat("misses")
        return None, "none"

    _GLOBAL_MEMORY_CACHE.set(cache_key, src_val, ttl_seconds)
    if set_to_redis_fn is not None:
        try:
            set_to_redis_fn(src_val)
        except Exception as err:
            logger.debug(f"[TIERED_CACHE] key={cache_key} redis_backfill_error={err}")
            pass
    if LOG_TIERED_CACHE_READS:
        logger.debug(f"[TIERED_CACHE] key={cache_key} source=db")
    _record_cache_read(cache_key, "db")
    _inc_cache_stat("db_hits")
    return src_val, "source"


async def aget_with_tiered_cache(
    *,
    cache_key: str,
    ttl_seconds: int = 600,
    load_from_source_fn: Callable[[], Awaitable[Optional[T]]],
    get_from_redis_fn: Optional[Callable[[], Awaitable[Optional[T]]]] = None,
    set_to_redis_fn: Optional[Callable[[T], Awaitable[None]]] = None,
    cache_none: bool = False,
) -> Tuple[Optional[T], str]:
    """
    Async version of get_with_tiered_cache.

    Transparent tiered read flow:
    memory -> redis (async) -> source (async db/api) and backfill upper tiers.

    Returns:
      (value, source_tier) where source_tier in {"memory","redis","source","none"}.
    """
    # Memory tier is sync (process-local, thread-safe dict)
    mem_val = _GLOBAL_MEMORY_CACHE.get(cache_key)
    if mem_val is not _MISS:
        if LOG_TIERED_CACHE_READS and LOG_TIERED_CACHE_MEMORY_HITS:
            logger.debug(f"[TIERED_CACHE] key={cache_key} source=memory")
        _record_cache_read(cache_key, "memory")
        _inc_cache_stat("memory_hits")
        return mem_val, "memory"

    if get_from_redis_fn is not None:
        try:
            redis_val = await get_from_redis_fn()
            if redis_val is not None:
                _GLOBAL_MEMORY_CACHE.set(cache_key, redis_val, ttl_seconds)
                if LOG_TIERED_CACHE_READS:
                    logger.debug(f"[TIERED_CACHE] key={cache_key} source=redis")
                _record_cache_read(cache_key, "redis")
                _inc_cache_stat("redis_hits")
                return redis_val, "redis"
        except Exception as err:
            logger.debug(f"[TIERED_CACHE] key={cache_key} source=redis error={err}")

    src_val = await load_from_source_fn()
    if src_val is None and not cache_none:
        if LOG_TIERED_CACHE_READS:
            logger.debug(f"[TIERED_CACHE] key={cache_key} source=none")
        _record_cache_read(cache_key, "none")
        _inc_cache_stat("misses")
        return None, "none"

    _GLOBAL_MEMORY_CACHE.set(cache_key, src_val, ttl_seconds)
    if set_to_redis_fn is not None:
        try:
            await set_to_redis_fn(src_val)
        except Exception as err:
            logger.debug(f"[TIERED_CACHE] key={cache_key} redis_backfill_error={err}")
    if LOG_TIERED_CACHE_READS:
        logger.debug(f"[TIERED_CACHE] key={cache_key} source=db")
    _record_cache_read(cache_key, "db")
    _inc_cache_stat("db_hits")
    return src_val, "source"


def invalidate_tiered_cache_key(cache_key: str) -> None:
    _GLOBAL_MEMORY_CACHE.delete(cache_key)


def invalidate_tiered_cache_prefix(prefix: str) -> int:
    return _GLOBAL_MEMORY_CACHE.delete_prefix(prefix)


def clear_tiered_cache() -> None:
    _GLOBAL_MEMORY_CACHE.clear()


# ── Async invalidation (memory + Redis) ─────────────────────────────────


async def _get_redis_for_invalidation():
    """Lazy import to avoid circular deps at module level."""
    try:
        from fashion_bot.utils.redis_client import get_shared_async_redis_client
        return await get_shared_async_redis_client()
    except Exception:
        return None


async def ainvalidate_tiered_cache_key(cache_key: str) -> None:
    """Clear a key from both memory and Redis tiers."""
    _GLOBAL_MEMORY_CACHE.delete(cache_key)
    try:
        client = await _get_redis_for_invalidation()
        if client:
            await client.delete(cache_key)
    except Exception as err:
        logger.warning(f"[TIERED_CACHE] Redis delete failed for key={cache_key}: {err}")


async def ainvalidate_tiered_cache_prefix(prefix: str) -> int:
    """Clear all keys matching *prefix* from memory and Redis."""
    removed = _GLOBAL_MEMORY_CACHE.delete_prefix(prefix)
    try:
        client = await _get_redis_for_invalidation()
        if client:
            cursor = "0"
            while True:
                cursor, keys = await client.scan(cursor=cursor, match=f"{prefix}*", count=200)
                if keys:
                    await client.delete(*keys)
                    removed += len(keys)
                if cursor == 0 or cursor == "0":
                    break
    except Exception as err:
        logger.warning(f"[TIERED_CACHE] Redis prefix delete failed for prefix={prefix}: {err}")
    return removed


async def aclear_tiered_cache() -> None:
    """Clear the entire memory tier. Redis keys are left to expire by TTL."""
    _GLOBAL_MEMORY_CACHE.clear()
