"""
Tag management module for fashion bot
Handles loading and accessing tags from PostgreSQL configuration with Redis caching
"""
import json
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Per-client tags dictionary cache (in-memory)
# Key: client_id, Value: tags dict
_TAGS_CACHE: Dict[str, Dict[str, str]] = {}

def get_tags_dict(client_id: str = None) -> Dict[str, str]:
    """
    Get the loaded tags dictionary for a client (from in-memory cache only).

    Args:
        client_id: Client ID for multi-tenant support

    Returns:
        Tags dictionary for the client
    """
    cache_key = client_id or "default"
    return _TAGS_CACHE.get(cache_key, {})


async def aensure_tags_loaded(client_id: str = None) -> Dict[str, str]:
    """Async version of ensure_tags_loaded."""
    global _TAGS_CACHE

    cache_key = client_id or "default"

    if cache_key in _TAGS_CACHE and _TAGS_CACHE[cache_key]:
        return _TAGS_CACHE[cache_key]

    try:
        from fashion_bot.utils.utils import aget_tags_with_caching

        tags = await aget_tags_with_caching(client_id)

        if tags:
            _TAGS_CACHE[cache_key] = tags
            logger.info(f"🏷️ Loaded {len(tags)} tags (async) for client {cache_key}: {list(tags.keys())}")
            return tags
        else:
            logger.warning(f"⚠️ No tags found for client {cache_key}, using empty dictionary")
            _TAGS_CACHE[cache_key] = {}
            return {}

    except Exception as e:
        logger.error(f"❌ Failed to async load tags for client {cache_key}: {e}")
        _TAGS_CACHE[cache_key] = {}
        return {}
