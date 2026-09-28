"""Unified tiered cache for identifier -> client_id resolution.

All external-identifier-to-``client_id`` lookups are routed through this
module so that every resolution path benefits from the same three-tier
cache (in-memory TTL -> Redis -> Postgres) and consistent invalidation.

Cache key convention
--------------------
All keys share the prefix ``cid:`` followed by an identifier type tag:

    cid:shop:{normalized_shop_domain}
    cid:gup_src:{gupshup_source_phone}
    cid:gup_app:{app_name}
    cid:gup_ent_app:{app_id}
    cid:domain:{normalized_website_domain}
    cid:name:{normalized_client_name}

This makes bulk invalidation simple: ``ainvalidate_tiered_cache_prefix("cid:")``
wipes every identity mapping from both tiers.

See ``design_docs/CLIENT_IDENTITY_CACHE.md`` for full design rationale.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Dict, Optional

from fashion_bot.database_manager import awith_retry, is_connection_error
from fashion_bot.env_loader import get_int
from fashion_bot.utils.tiered_cache import (
    aget_with_tiered_cache,
    ainvalidate_tiered_cache_key,
    ainvalidate_tiered_cache_prefix,
)

logger = logging.getLogger(__name__)

_TTL = get_int("CLIENT_IDENTITY_CACHE_TTL", 600)


# ── Redis helpers (shared across all resolution functions) ───────────────


async def _redis_client():
    from fashion_bot.utils.redis_client import get_shared_async_redis_client
    return await get_shared_async_redis_client()


async def _redis_get(key: str) -> Optional[str]:
    client = await _redis_client()
    if not client:
        return None
    raw = await client.get(key)
    return raw if raw else None


async def _redis_set(key: str, value: Optional[str], ttl: int) -> None:
    if not value:
        return
    client = await _redis_client()
    if client:
        await client.setex(key, ttl, value)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Shopify shop domain -> client_id
# ═══════════════════════════════════════════════════════════════════════════


@awith_retry
async def _db_client_id_by_shop_domain(shop_domain: str) -> Optional[str]:
    from fashion_bot.database_manager import get_async_postgres_connection
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM clients WHERE shopify_domain_name = %s LIMIT 1;",
                    (shop_domain,),
                )
                row = await cur.fetchone()
                return str(row["id"]) if row else None
    except Exception as e:
        if is_connection_error(e):
            raise
        logger.error(f"[CID_CACHE] DB lookup failed for shop_domain={shop_domain}: {e}")
        return None


async def aget_client_id_by_shop_domain(shop_domain: str) -> Optional[str]:
    """Resolve Shopify shop domain to client_id (tiered cache)."""
    if not shop_domain:
        return None
    normalized = shop_domain.strip().lower()
    key = f"cid:shop:{normalized}"
    value, _ = await aget_with_tiered_cache(
        cache_key=key,
        ttl_seconds=_TTL,
        load_from_source_fn=lambda: _db_client_id_by_shop_domain(normalized),
        get_from_redis_fn=lambda: _redis_get(key),
        set_to_redis_fn=lambda v: _redis_set(key, v, _TTL),
        cache_none=True,
    )
    return value


# ═══════════════════════════════════════════════════════════════════════════
# 2. Gupshup source phone -> client_id
# ═══════════════════════════════════════════════════════════════════════════


@awith_retry
async def _db_client_id_by_gupshup_source(gupshup_source: str) -> Optional[str]:
    from fashion_bot.database_manager import get_async_postgres_connection
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id AS client_id FROM clients WHERE gupshup_source_number = %s LIMIT 1;",
                    (gupshup_source,),
                )
                row = await cur.fetchone()
                if row and row.get("client_id"):
                    return str(row["client_id"])
                return None
    except Exception as e:
        if is_connection_error(e):
            raise
        logger.error(f"[CID_CACHE] DB lookup failed for gupshup_source={gupshup_source}: {e}")
        return None


async def aget_client_id_by_gupshup_source(gupshup_source: Optional[str] = None) -> Optional[str]:
    """Resolve Gupshup source phone to client_id (tiered cache).

    Returns ``None`` instead of ``""`` when no match is found.
    Callers that previously used ``aresolve_client_id()`` and expected
    ``""`` on miss should coalesce: ``result or ""``.
    """
    if not gupshup_source:
        return None
    key = f"cid:gup_src:{gupshup_source}"
    value, _ = await aget_with_tiered_cache(
        cache_key=key,
        ttl_seconds=_TTL,
        load_from_source_fn=lambda: _db_client_id_by_gupshup_source(gupshup_source),
        get_from_redis_fn=lambda: _redis_get(key),
        set_to_redis_fn=lambda v: _redis_set(key, v, _TTL),
        cache_none=True,
    )
    return value


# ═══════════════════════════════════════════════════════════════════════════
# 3. Gupshup APP_NAME -> client_id
# ═══════════════════════════════════════════════════════════════════════════


@awith_retry
async def _db_client_id_by_gupshup_app_name(app_name: str) -> Optional[str]:
    from fashion_bot.database_manager import get_async_postgres_connection
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT client_id
                    FROM client_configs
                    WHERE config_key = 'gupshup_details'
                    AND config_value::jsonb->>'APP_NAME' = %s
                    LIMIT 1
                    """,
                    (app_name,),
                )
                row = await cur.fetchone()
                if row and row.get("client_id"):
                    return str(row["client_id"])
                return None
    except Exception as e:
        if is_connection_error(e):
            raise
        logger.error(f"[CID_CACHE] DB lookup failed for app_name={app_name}: {e}")
        return None


async def aget_client_id_by_gupshup_app_name(app_name: str) -> Optional[str]:
    """Resolve Gupshup APP_NAME to client_id (tiered cache)."""
    if not app_name:
        return None
    key = f"cid:gup_app:{app_name}"
    value, _ = await aget_with_tiered_cache(
        cache_key=key,
        ttl_seconds=_TTL,
        load_from_source_fn=lambda: _db_client_id_by_gupshup_app_name(app_name),
        get_from_redis_fn=lambda: _redis_get(key),
        set_to_redis_fn=lambda v: _redis_set(key, v, _TTL),
        cache_none=True,
    )
    return value


# ═══════════════════════════════════════════════════════════════════════════
# 3b. Gupshup enterprise app id -> client_id
# ═══════════════════════════════════════════════════════════════════════════


@awith_retry
async def _db_client_id_by_gupshup_enterprise_app(app_id: str) -> Optional[str]:
    from fashion_bot.database_manager import get_async_postgres_connection
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT client_id
                    FROM client_configs
                    WHERE config_key = 'gupshup_enterprise_app_details'
                    AND config_value::jsonb->>'app' = %s
                    LIMIT 1
                    """,
                    (app_id,),
                )
                row = await cur.fetchone()
                if row and row.get("client_id"):
                    return str(row["client_id"])
                return None
    except Exception as e:
        if is_connection_error(e):
            raise
        logger.error(
            f"[CID_CACHE] DB lookup failed for enterprise app_id={app_id}: {e}"
        )
        return None


async def aget_client_id_by_gupshup_enterprise_app(
    app_id: str,
) -> Optional[str]:
    """Resolve Gupshup enterprise app id to client_id (tiered cache)."""
    normalized = str(app_id or "").strip()
    if not normalized:
        return None
    key = f"cid:gup_ent_app:{normalized}"
    value, _ = await aget_with_tiered_cache(
        cache_key=key,
        ttl_seconds=_TTL,
        load_from_source_fn=lambda: _db_client_id_by_gupshup_enterprise_app(
            normalized
        ),
        get_from_redis_fn=lambda: _redis_get(key),
        set_to_redis_fn=lambda v: _redis_set(key, v, _TTL),
        cache_none=True,
    )
    return value


# ═══════════════════════════════════════════════════════════════════════════
# 4. Website domain -> client_id
# ═══════════════════════════════════════════════════════════════════════════


def _normalize_domain(domain: str) -> str:
    """Canonical bare domain: lowercase, no scheme, no www, no trailing slash."""
    d = domain.lower().replace("https://", "").replace("http://", "")
    d = d.replace("www.", "").rstrip("/")
    return d


@awith_retry
async def _db_client_id_by_website_domain(normalized: str) -> Optional[str]:
    from fashion_bot.database_manager import get_async_postgres_connection
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT c.id
                       FROM clients c
                       LEFT JOIN client_configs cc ON cc.client_id = c.id
                       WHERE TRIM(TRAILING '/' FROM
                             REPLACE(REPLACE(REPLACE(LOWER(c.domain),
                                     'https://',''), 'http://',''), 'www.','')
                             ) = %s
                       GROUP BY c.id
                       ORDER BY COUNT(cc.id) DESC
                       LIMIT 1""",
                    (normalized,),
                )
                row = await cur.fetchone()
                if row:
                    return str(row["id"])
                return None
    except Exception as e:
        if is_connection_error(e):
            raise
        logger.warning(f"[CID_CACHE] DB lookup failed for domain={normalized}: {e}")
        return None


async def aget_client_id_by_website_domain(domain: str) -> Optional[str]:
    """Resolve a website domain to client_id (tiered cache)."""
    if not domain:
        return None
    normalized = _normalize_domain(domain)
    key = f"cid:domain:{normalized}"
    value, _ = await aget_with_tiered_cache(
        cache_key=key,
        ttl_seconds=_TTL,
        load_from_source_fn=lambda: _db_client_id_by_website_domain(normalized),
        get_from_redis_fn=lambda: _redis_get(key),
        set_to_redis_fn=lambda v: _redis_set(key, v, _TTL),
        cache_none=True,
    )
    return value


# ═══════════════════════════════════════════════════════════════════════════
# 5. Client name -> client_id
# ═══════════════════════════════════════════════════════════════════════════


@awith_retry
async def _db_client_id_by_client_name(name: str) -> Optional[str]:
    from fashion_bot.database_manager import get_async_postgres_connection
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM clients WHERE LOWER(name) = %s LIMIT 1;",
                    (name,),
                )
                row = await cur.fetchone()
                if row:
                    return str(row["id"])
                return None
    except Exception as e:
        if is_connection_error(e):
            raise
        logger.error(f"[CID_CACHE] DB lookup failed for client_name={name}: {e}")
        return None


async def aget_client_id_by_client_name(name: str) -> Optional[str]:
    """Resolve a client display name to client_id (tiered cache).

    Lookup is case-insensitive (both sides lowered).
    """
    if not name:
        return None
    normalized = name.strip().lower()
    key = f"cid:name:{normalized}"
    value, _ = await aget_with_tiered_cache(
        cache_key=key,
        ttl_seconds=_TTL,
        load_from_source_fn=lambda: _db_client_id_by_client_name(normalized),
        get_from_redis_fn=lambda: _redis_get(key),
        set_to_redis_fn=lambda v: _redis_set(key, v, _TTL),
        cache_none=True,
    )
    return value


# ═══════════════════════════════════════════════════════════════════════════
# 6. client_id -> display name (reverse map, for metric labelling)
# ═══════════════════════════════════════════════════════════════════════════
#
# Unlike the sections above (which resolve an external identifier to a
# client_id on a request), this is a small whole-table snapshot consumed by a
# *synchronous* hot path: the OTel LLM callback labels every metric with the
# originating client and wants a human-readable ``client_name`` alongside
# ``client_id``. Because the callback can't await, ``get_client_name_sync``
# reads a process-local snapshot and schedules a throttled async refresh on a
# miss — the snapshot itself is filled from the same memory→Redis→DB tiered
# cache as everything else here.

_ID_NAME_CACHE_KEY = "cid:idname:map"
# Process-local snapshot of {client_id: display_name}, kept fresh by the async
# refresh below so the sync accessor never blocks.
_ID_NAME_SNAPSHOT: Dict[str, str] = {}
_ID_NAME_LAST_REFRESH: float = 0.0
_ID_NAME_REFRESH_INTERVAL = get_int("CLIENT_ID_NAME_REFRESH_SECONDS", _TTL)
# Last-seen running event loop. The OTel LLM callback (the main consumer of
# get_client_name_sync) runs in a thread-pool thread with *no* running loop, so
# it can't schedule the async refresh itself. We capture the loop from on-loop
# callers (request entry via prewarm_client_name_cache) and use it to schedule
# refreshes thread-safely from those off-loop callers.
_EVENT_LOOP: Optional[asyncio.AbstractEventLoop] = None


@awith_retry
async def _db_client_id_to_name_map() -> Optional[Dict[str, str]]:
    from fashion_bot.database_manager import get_async_postgres_connection
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    'SELECT id, "name" FROM clients WHERE "name" IS NOT NULL;'
                )
                rows = await cur.fetchall()
        mapping: Dict[str, str] = {}
        for row in rows:
            if isinstance(row, dict):
                cid, name = row.get("id"), row.get("name")
            else:
                cid, name = row[0], row[1]
            if cid and name:
                mapping[str(cid)] = str(name)
        # Preserve "no data" as None so a transient empty read isn't cached.
        return mapping or None
    except Exception as e:
        if is_connection_error(e):
            raise
        logger.error(f"[CID_CACHE] id->name map lookup failed: {e}")
        return None


async def _idname_redis_get() -> Optional[Dict[str, str]]:
    raw = await _redis_get(_ID_NAME_CACHE_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) and data else None
    except Exception:
        return None


async def _idname_redis_set(value: Optional[Dict[str, str]]) -> None:
    if not value:
        return
    await _redis_set(_ID_NAME_CACHE_KEY, json.dumps(value), _TTL)


async def aget_client_id_to_name_map() -> Dict[str, str]:
    """Resolve the ``{client_id: display_name}`` map (tiered cache).

    Memory -> Redis -> Postgres per AGENTS.md §3. Also refreshes the
    process-local ``_ID_NAME_SNAPSHOT`` consumed by :func:`get_client_name_sync`.
    """
    global _ID_NAME_SNAPSHOT
    value, _ = await aget_with_tiered_cache(
        cache_key=_ID_NAME_CACHE_KEY,
        ttl_seconds=_TTL,
        load_from_source_fn=_db_client_id_to_name_map,
        get_from_redis_fn=_idname_redis_get,
        set_to_redis_fn=_idname_redis_set,
        cache_none=False,
    )
    if value:
        _ID_NAME_SNAPSHOT = value
    return value or {}


async def aget_client_name_by_id(client_id: str) -> Optional[str]:
    """Resolve a single client_id to its display name (async, tiered cache)."""
    if not client_id:
        return None
    return (await aget_client_id_to_name_map()).get(str(client_id))


async def _safe_refresh_id_name_map() -> None:
    try:
        await aget_client_id_to_name_map()
    except Exception as e:  # never let a background refresh surface to the caller
        logger.debug(f"[CID_CACHE] background id->name refresh failed: {e}")


def _maybe_schedule_id_name_refresh() -> None:
    """Fire a throttled, best-effort async refresh of the id->name snapshot.

    Works from two contexts:

    * **On the event loop** (e.g. request entry via
      :func:`prewarm_client_name_cache`): schedules with ``create_task`` and
      records the loop so off-loop callers can reuse it.
    * **Off the loop** (e.g. the OTel LLM callback's thread-pool thread, which
      has no running loop): schedules on the last-seen loop via
      ``run_coroutine_threadsafe``.

    No-ops if no loop has ever been observed (cold process with no traffic yet).
    """
    global _ID_NAME_LAST_REFRESH, _EVENT_LOOP
    now = time.monotonic()
    if now - _ID_NAME_LAST_REFRESH < _ID_NAME_REFRESH_INTERVAL:
        return

    try:
        loop = asyncio.get_running_loop()
        _EVENT_LOOP = loop
        _ID_NAME_LAST_REFRESH = now
        loop.create_task(_safe_refresh_id_name_map())
        return
    except RuntimeError:
        pass  # not on a loop (callback executor thread) — use the captured one

    loop = _EVENT_LOOP
    if loop is not None and loop.is_running():
        _ID_NAME_LAST_REFRESH = now
        try:
            asyncio.run_coroutine_threadsafe(_safe_refresh_id_name_map(), loop)
        except Exception as e:
            logger.debug(f"[CID_CACHE] threadsafe id->name refresh failed: {e}")


def prewarm_client_name_cache() -> None:
    """Capture the running loop and warm the id->name snapshot (throttled).

    Call from an on-loop, per-request entry point (e.g.
    ``set_request_client_id``) so the snapshot is populated and the loop is
    recorded for the off-loop OTel callback to reuse.
    """
    _maybe_schedule_id_name_refresh()


def get_client_name_sync(client_id: Optional[str]) -> str:
    """Best-effort, non-blocking ``client_id -> display_name`` for metric labels.

    Returns the cached display name, or ``"unknown"`` on a miss (mirroring the
    ``client_id`` label's own fallback). A miss schedules a throttled async
    refresh so subsequent calls resolve once the snapshot warms up. Safe to
    call from synchronous hot paths (e.g. the OTel LLM callback).
    """
    if not client_id or client_id == "unknown":
        return "unknown"
    name = _ID_NAME_SNAPSHOT.get(str(client_id))
    if name:
        return name
    _maybe_schedule_id_name_refresh()
    return "unknown"


# ═══════════════════════════════════════════════════════════════════════════
# Invalidation helpers
# ═══════════════════════════════════════════════════════════════════════════


async def ainvalidate_shop_domain(shop_domain: str) -> None:
    """Bust the cache entry for a specific shop domain."""
    await ainvalidate_tiered_cache_key(f"cid:shop:{shop_domain.strip().lower()}")


async def ainvalidate_gupshup_source(gupshup_source: str) -> None:
    await ainvalidate_tiered_cache_key(f"cid:gup_src:{gupshup_source}")


async def ainvalidate_gupshup_app_name(app_name: str) -> None:
    await ainvalidate_tiered_cache_key(f"cid:gup_app:{app_name}")


async def ainvalidate_gupshup_enterprise_app(app_id: str) -> None:
    await ainvalidate_tiered_cache_key(f"cid:gup_ent_app:{str(app_id).strip()}")


async def ainvalidate_website_domain(domain: str) -> None:
    await ainvalidate_tiered_cache_key(f"cid:domain:{_normalize_domain(domain)}")


async def ainvalidate_client_name(name: str) -> None:
    await ainvalidate_tiered_cache_key(f"cid:name:{name.strip().lower()}")
    # The reverse id->name snapshot also depends on client names.
    await ainvalidate_tiered_cache_key(_ID_NAME_CACHE_KEY)


async def ainvalidate_all() -> int:
    """Bust every ``cid:*`` key from memory and Redis."""
    return await ainvalidate_tiered_cache_prefix("cid:")


# Mapping from user-facing identifier_type strings to invalidation functions.
# Used by the admin endpoint.
INVALIDATION_DISPATCH = {
    "shop_domain": ainvalidate_shop_domain,
    "gupshup_source": ainvalidate_gupshup_source,
    "app_name": ainvalidate_gupshup_app_name,
    "enterprise_app": ainvalidate_gupshup_enterprise_app,
    "domain": ainvalidate_website_domain,
    "client_name": ainvalidate_client_name,
}
