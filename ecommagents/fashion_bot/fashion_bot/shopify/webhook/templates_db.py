import logging
import json
from typing import Optional, Dict, Any, List
from fashion_bot.database_manager import get_async_postgres_connection, get_direct_postgres_cursor
from fashion_bot.utils.redis_client import get_shared_async_redis_client
from fashion_bot.utils.tiered_cache import (
    aget_with_tiered_cache,
    invalidate_tiered_cache_key,
    invalidate_tiered_cache_prefix,
)

logger = logging.getLogger(__name__)
TEMPLATE_CACHE_TTL_SECONDS = 1800  # 30 minutes
_TEMPLATE_CACHE_PREFIX = "tpl"


def _template_cache_key(client_id: str, channel: str, event_key: str) -> str:
    return f"{_TEMPLATE_CACHE_PREFIX}:{str(client_id or '').strip()}:{str(channel or '').strip()}:{str(event_key or '').strip()}"


def _template_cache_prefix(client_id: str, channel: Optional[str] = None) -> str:
    base = f"{_TEMPLATE_CACHE_PREFIX}:{str(client_id or '').strip()}:"
    if channel:
        return f"{base}{str(channel).strip()}:"
    return base


async def _aredis_delete_by_prefix(prefix: str) -> int:
    """Best-effort async Redis prefix delete with SCAN."""
    client = await get_shared_async_redis_client()
    if not client:
        return 0

    deleted = 0
    cursor = 0
    pattern = f"{prefix}*"
    while True:
        cursor, keys = await client.scan(cursor=cursor, match=pattern, count=100)
        if keys:
            deleted += await client.delete(*keys)
        cursor = int(cursor)
        if cursor == 0:
            break
    return int(deleted or 0)


async def ainvalidate_client_template_cache(
    client_id: str,
    channel: Optional[str] = None,
    event_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Invalidate template cache for:
    - exact key (client + channel + event_key), or
    - channel prefix (client + channel), or
    - full client prefix.
    """
    if not client_id:
        return {"ok": False, "reason": "client_id_required", "memory_removed": 0, "redis_removed": 0}

    memory_removed = 0
    redis_removed = 0

    if channel and event_key:
        cache_key = _template_cache_key(client_id, channel, event_key)
        invalidate_tiered_cache_key(cache_key)
        memory_removed = 1
        redis_client = await get_shared_async_redis_client()
        if redis_client:
            redis_removed = int(await redis_client.delete(cache_key) or 0)
        return {"ok": True, "scope": "key", "cache_key": cache_key, "memory_removed": memory_removed, "redis_removed": redis_removed}

    prefix = _template_cache_prefix(client_id, channel=channel)
    memory_removed = int(invalidate_tiered_cache_prefix(prefix) or 0)
    redis_removed = await _aredis_delete_by_prefix(prefix)
    return {"ok": True, "scope": "prefix", "prefix": prefix, "memory_removed": memory_removed, "redis_removed": redis_removed}

DDL_TEMPLATES = """
CREATE TABLE IF NOT EXISTS gupshup_templates (
  id SERIAL PRIMARY KEY,
  client_id VARCHAR(128) NOT NULL,
  channel VARCHAR(32) NOT NULL DEFAULT 'shopify',
  event_key VARCHAR(128) NOT NULL,
  template_id VARCHAR(128) NOT NULL,
  template_name VARCHAR(128),
  image_url TEXT,
  param_order TEXT, -- comma-separated
  created_at TIMESTAMPTZ DEFAULT NOW()
);
"""

DDL_TEMPLATE_META = """
CREATE TABLE IF NOT EXISTS gupshup_template_catalog (
  id SERIAL PRIMARY KEY,
  client_id VARCHAR(128) NOT NULL,
  name VARCHAR(255) NOT NULL,
  language_code VARCHAR(32) NOT NULL,
  category VARCHAR(64) NOT NULL,
  template_id VARCHAR(128), -- returned by Gupshup after approval
  raw_definition JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (client_id, name, language_code)
);
"""

def ensure_tables():
    """Ensure tables exist. Uses direct connection for startup operations."""
    conn = None
    cur = None
    try:
        # Use direct connection for startup (bypasses pool)
        conn, cur = get_direct_postgres_cursor()
        cur.execute(DDL_TEMPLATES)
        # Ensure channel column exists
        cur.execute("ALTER TABLE gupshup_templates ADD COLUMN IF NOT EXISTS channel VARCHAR(32) NOT NULL DEFAULT 'shopify';")
        # Ensure template_name column exists
        cur.execute("ALTER TABLE gupshup_templates ADD COLUMN IF NOT EXISTS template_name VARCHAR(128);")
        # Ensure unique index on (client_id, channel, event_key)
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS gupshup_templates_client_channel_event_key_idx ON gupshup_templates(client_id, channel, event_key);")
        cur.execute(DDL_TEMPLATE_META)
    finally:
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


async def aupsert_client_template(
    client_id: str,
    channel: str,
    event_key: str,
    template_id: str,
    image_url: Optional[str],
    param_order: List[str],
    template_name: Optional[str] = None,
) -> bool:
    """Async upsert template for a client with channel."""
    client_id_str = str(client_id) if client_id else None

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO gupshup_templates(client_id, channel, event_key, template_id, template_name, image_url, param_order)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (client_id, channel, event_key)
                    DO UPDATE SET template_id = EXCLUDED.template_id, template_name = EXCLUDED.template_name, image_url = EXCLUDED.image_url, param_order = EXCLUDED.param_order
                    """,
                    (client_id_str, channel, event_key, template_id, template_name, image_url, ",".join(param_order or [])),
                )
                try:
                    await ainvalidate_client_template_cache(
                        client_id=client_id_str or "",
                        channel=channel,
                        event_key=event_key,
                    )
                except Exception as cache_err:
                    logger.warning(
                        "[TEMPLATE_LOOKUP] cache invalidation warning for client_id=%s channel=%s event_key=%s: %s",
                        client_id_str,
                        channel,
                        event_key,
                        cache_err,
                    )
                return True
    except Exception as e:
        logger.error(f"Async upsert client template error: {e}")
        return False


async def aget_client_template(client_id: str, channel: str, event_key: str) -> Optional[Dict[str, Any]]:
    """Async template lookup for request-path webhook delivery."""
    client_id_str = str(client_id) if client_id else None

    try:
        cache_key = _template_cache_key(client_id_str or "", channel, event_key)

        async def _load_from_db() -> Optional[Dict[str, Any]]:
            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    logger.info(
                        f"[TEMPLATE_LOOKUP] Querying DB: client_id={client_id_str}, channel={channel}, event_key={event_key}"
                    )
                    await cur.execute(
                        "SELECT template_id, template_name, image_url, param_order FROM gupshup_templates WHERE client_id=%s AND channel=%s AND event_key=%s",
                        (client_id_str, channel, event_key),
                    )
                    row = await cur.fetchone()
                    logger.info(f"[TEMPLATE_LOOKUP] Query result: {row}")
                    if not row:
                        await cur.execute(
                            "SELECT channel, event_key FROM gupshup_templates WHERE client_id=%s",
                            (client_id_str,),
                        )
                        available = await cur.fetchall()
                        logger.warning(
                            f"[TEMPLATE_LOOKUP] No template found. Available templates for client {client_id_str}: {available}"
                        )
                        return None
                    return {
                        "template_id": row["template_id"],
                        "template_name": row["template_name"],
                        "image_url": row["image_url"],
                        "param_order": row["param_order"].split(",") if row["param_order"] else [],
                    }

        async def _redis_get() -> Optional[Dict[str, Any]]:
            client = await get_shared_async_redis_client()
            if not client:
                return None
            raw = await client.get(cache_key)
            if not raw:
                return None
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) and parsed else None
            except Exception:
                return None

        async def _redis_set(value: Optional[Dict[str, Any]]) -> None:
            if not value:
                return
            client = await get_shared_async_redis_client()
            if client:
                await client.setex(cache_key, TEMPLATE_CACHE_TTL_SECONDS, json.dumps(value))

        value, source = await aget_with_tiered_cache(
            cache_key=cache_key,
            ttl_seconds=TEMPLATE_CACHE_TTL_SECONDS,
            load_from_source_fn=_load_from_db,
            get_from_redis_fn=_redis_get,
            set_to_redis_fn=_redis_set,
            cache_none=False,
        )
        logger.info("[TEMPLATE_LOOKUP] cache source=%s key=%s", source, cache_key)
        if value:
            logger.info(
                "[TEMPLATE_LOOKUP] resolved client_id=%s channel=%s event_key=%s "
                "template_id=%s image_url=%s source=%s",
                client_id_str,
                channel,
                event_key,
                value.get("template_id"),
                value.get("image_url") or "N/A",
                source,
            )
            print(
                f"[TEMPLATE_LOOKUP] client_id={client_id_str} channel={channel} "
                f"event_key={event_key} image_url={value.get('image_url') or '(empty)'} "
                f"source={source}"
            )
        return value
    except Exception as e:
        logger.error(f"[TEMPLATE_LOOKUP] Error querying template: {e}", exc_info=True)
        return None


async def aget_client_template_image_url_direct(
    client_id: str,
    channel: Optional[str],
    event_key: Optional[str],
    template_id: Optional[str] = None,
) -> Optional[str]:
    """Read template image_url directly from Postgres, bypassing caches."""
    client_id_str = str(client_id) if client_id else None
    if not client_id_str:
        return None

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                if channel and event_key:
                    await cur.execute(
                        """
                        SELECT image_url
                        FROM gupshup_templates
                        WHERE client_id=%s AND channel=%s AND event_key=%s
                        """,
                        (client_id_str, channel, event_key),
                    )
                    row = await cur.fetchone()
                    image_url = (row or {}).get("image_url") if row else None
                    if image_url:
                        return str(image_url).strip() or None

                if template_id:
                    await cur.execute(
                        """
                        SELECT image_url
                        FROM gupshup_templates
                        WHERE client_id=%s AND template_id=%s
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (client_id_str, template_id),
                    )
                    row = await cur.fetchone()
                    image_url = (row or {}).get("image_url") if row else None
                    if image_url:
                        return str(image_url).strip() or None
        return None
    except Exception as e:
        logger.error(
            "[TEMPLATE_LOOKUP] direct image_url lookup failed client_id=%s "
            "channel=%s event_key=%s template_id=%s err=%s",
            client_id_str,
            channel,
            event_key,
            template_id,
            e,
            exc_info=True,
        )
        return None


# Backward compatibility aliases
aupsert_tenant_template = aupsert_client_template
aget_tenant_template = aget_client_template


async def aupsert_client_template_catalog(
    client_id: str,
    name: str,
    language_code: str,
    category: str,
    template_id: Optional[str],
    raw_definition: Dict[str, Any],
) -> bool:
    """Async upsert template catalog for a client."""
    client_id_str = str(client_id) if client_id else None

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO gupshup_template_catalog(client_id, name, language_code, category, template_id, raw_definition)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (client_id, name, language_code)
                    DO UPDATE SET category = EXCLUDED.category, template_id = EXCLUDED.template_id, raw_definition = EXCLUDED.raw_definition
                    """,
                    (client_id_str, name, language_code, category, template_id, raw_definition),
                )
                return True
    except Exception as e:
        logger.error(f"Async upsert client template catalog error: {e}")
        return False
