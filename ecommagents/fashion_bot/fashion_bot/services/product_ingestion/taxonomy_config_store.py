"""
Async CRUD for the client_taxonomy_config table.

Stores per-client taxonomy definitions (categories, subcategories,
attribute schemas, etc.) that feed into the product attribute extractor
prompt and allow clients to customise the classification vocabulary.

Reads go through three-tier cache (memory → Redis → Postgres) per
AGENTS.md §3. Writes invalidate the cache so subsequent reads pick up
the new values after the memory TTL expires.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fashion_bot.database_manager import get_async_postgres_connection

logger = logging.getLogger(__name__)

_TAXONOMY_CACHE_TTL_SECONDS = 600
_TAXONOMY_REDIS_TTL_SECONDS = 3600

DEFAULT_CATEGORIES = [
    "topwear", "bottomwear", "footwear", "outerwear", "bags",
    "accessories", "innerwear", "phone_cases", "skincare", "other",
]

DEFAULT_OCCASIONS = [
    "casual", "party", "formal", "work", "festive",
    "date-night", "sport", "lounge", "wedding",
]

DEFAULT_STYLES = [
    "slim-fit", "wide-leg", "cropped", "oversized",
    "high-waist", "a-line",
]

DEFAULT_VIBES = [
    "elegant", "streetwear", "bohemian", "minimalist",
    "vintage", "edgy",
]

DEFAULT_SEGMENTS = ["women", "men", "unisex", "kids"]

DEFAULT_COLOR_FAMILIES = [
    "black", "white", "blue", "red", "green", "pink",
    "brown", "grey", "beige", "multicolor", "metallic",
]

DEFAULT_PATTERNS = [
    "solid", "striped", "floral", "checkered", "printed", "abstract",
]

DEFAULT_FITS = [
    "slim", "regular", "relaxed", "oversized", "tailored",
]

DEFAULT_PAIRING_TAGS = [
    "heels", "blazer", "sneakers", "clutch", "jeans",
    "t-shirt", "hoodie", "jacket", "boots", "scarf",
]


async def _aget_taxonomy_config_from_db(client_id: str) -> Optional[Dict[str, Any]]:
    """Raw DB read — callers should use ``aget_taxonomy_config`` instead."""
    query = "SELECT * FROM client_taxonomy_config WHERE client_id = %s"
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, (client_id,))
                row = await cur.fetchone()
                return dict(row) if row else None
    except Exception as e:
        logger.error(f"[TAXONOMY] get failed for {client_id}: {e}")
        return None


async def aget_taxonomy_config(client_id: str) -> Optional[Dict[str, Any]]:
    """Return the taxonomy config for a client via three-tier cache.

    Cache tiers: in-memory (10 min) → Redis (1 hour) → Postgres.
    """
    if not client_id:
        return None

    cache_key = f"taxonomy_config:{client_id}"
    redis_key = f"taxonomy_config:{client_id}"

    async def _get_from_redis() -> Optional[Dict[str, Any]]:
        try:
            from fashion_bot.utils.redis_client import get_shared_async_redis_client
            redis = await get_shared_async_redis_client()
            if redis is None:
                return None
            raw = await redis.get(redis_key)
            if raw:
                return json.loads(raw)
        except Exception:
            pass
        return None

    async def _set_to_redis(val: Dict[str, Any]) -> None:
        try:
            from fashion_bot.utils.redis_client import get_shared_async_redis_client
            redis = await get_shared_async_redis_client()
            if redis is not None:
                await redis.set(redis_key, json.dumps(val, default=str), ex=_TAXONOMY_REDIS_TTL_SECONDS)
        except Exception:
            pass

    async def _load_from_db() -> Optional[Dict[str, Any]]:
        return await _aget_taxonomy_config_from_db(client_id)

    try:
        from fashion_bot.utils.tiered_cache import aget_with_tiered_cache

        val, source = await aget_with_tiered_cache(
            cache_key=cache_key,
            ttl_seconds=_TAXONOMY_CACHE_TTL_SECONDS,
            load_from_source_fn=_load_from_db,
            get_from_redis_fn=_get_from_redis,
            set_to_redis_fn=_set_to_redis,
        )
        if val:
            logger.debug(f"[TAXONOMY] config loaded from {source} for {client_id}")
        return val
    except Exception as e:
        logger.warning(f"[TAXONOMY] tiered cache failed for {client_id}: {e}; falling back to DB")
        return await _aget_taxonomy_config_from_db(client_id)


async def aupsert_taxonomy_config(client_id: str, config: Dict[str, Any]) -> bool:
    """Create or update taxonomy config for a client.

    *config* may contain any subset of:
      categories, subcategory_mapping, attribute_schema, occasions,
      styles, vibes, segments, color_families, patterns
    """
    jsonb_fields = [
        "categories", "subcategory_mapping", "attribute_schema",
        "occasions", "styles", "vibes", "segments",
        "color_families", "patterns", "fits", "pairing_tags",
    ]

    columns = ["client_id", "updated_at"]
    values: list = [client_id, datetime.now(timezone.utc)]

    for field in jsonb_fields:
        if field in config:
            columns.append(field)
            values.append(json.dumps(config[field]))

    if len(columns) == 2:
        return False

    placeholders = ", ".join(["%s"] * len(values))
    col_str = ", ".join(columns)

    update_parts = [f"{c} = EXCLUDED.{c}" for c in columns if c != "client_id"]
    update_str = ", ".join(update_parts)

    query = f"""
        INSERT INTO client_taxonomy_config ({col_str})
        VALUES ({placeholders})
        ON CONFLICT (client_id) DO UPDATE
            SET {update_str}
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, values)
                await _ainvalidate_taxonomy_cache(client_id)
                return True
    except Exception as e:
        logger.error(f"[TAXONOMY] upsert failed for {client_id}: {e}")
        return False


async def adelete_taxonomy_config(client_id: str) -> bool:
    """Delete taxonomy config for a client (revert to defaults)."""
    query = "DELETE FROM client_taxonomy_config WHERE client_id = %s"
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, (client_id,))
                await _ainvalidate_taxonomy_cache(client_id)
                return True
    except Exception as e:
        logger.error(f"[TAXONOMY] delete failed for {client_id}: {e}")
        return False


async def _ainvalidate_taxonomy_cache(client_id: str) -> None:
    """Bust memory and Redis cache for a client's taxonomy config."""
    cache_key = f"taxonomy_config:{client_id}"
    try:
        from fashion_bot.utils.tiered_cache import ainvalidate_tiered_cache_key
        await ainvalidate_tiered_cache_key(cache_key)
    except Exception:
        pass


def get_defaults() -> Dict[str, Any]:
    """Return the built-in default taxonomy (used when no DB row exists)."""
    return {
        "categories": DEFAULT_CATEGORIES,
        "subcategory_mapping": {},
        "attribute_schema": {},
        "occasions": DEFAULT_OCCASIONS,
        "styles": DEFAULT_STYLES,
        "vibes": DEFAULT_VIBES,
        "segments": DEFAULT_SEGMENTS,
        "color_families": DEFAULT_COLOR_FAMILIES,
        "patterns": DEFAULT_PATTERNS,
        "fits": DEFAULT_FITS,
        "pairing_tags": DEFAULT_PAIRING_TAGS,
    }
