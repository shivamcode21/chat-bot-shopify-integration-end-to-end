import asyncio
import uuid
import logging
import datetime
import os
import json
import re
from typing import Dict, Any, List, Optional
from langchain_core.messages import AIMessage
import smtplib, ssl
from email.message import EmailMessage
import certifi
from fashion_bot.utils.redis_guard import RedisGuard
from fashion_bot.utils.tiered_cache import aget_with_tiered_cache

logger = logging.getLogger("meta_nodes")


_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def extract_email_candidate(*texts: str) -> Optional[str]:
    """Return the first email address in free text, lowercased, else ``None``.

    Companion to :func:`extract_webchat_phone_candidate`, for the other half of
    the "share your phone number or email" prompt. Lowercased because it is
    compared against contacts stored on earlier escalations, and customers do
    not retype their address with the same capitalisation twice.
    """
    for text in texts:
        if not text:
            continue
        match = _EMAIL_RE.search(str(text))
        if match:
            return match.group(0).strip().lower()
    return None


def extract_webchat_phone_candidate(*texts: str) -> Optional[str]:
    """Return a normalized 10-digit phone from free text when present.

    Only strips well-defined prefixes (a ``+91`` / ``91`` country code or a
    single leading ``0`` trunk code). Anything that does not resolve to
    *exactly* 10 digits — e.g. an 11-digit typo like ``80875053289`` — is
    rejected (returns ``None``) so the caller re-prompts the customer instead
    of silently fabricating a wrong number by truncating digits to the last
    10. Truncating an over-long run (the old ``digits[-10:]`` behaviour) turned
    a clear typo into a plausible-but-wrong number that got linked to the
    session and shadowed every later correction.
    """
    from fashion_bot.utils.phone_number_utils import normalize_phone_number

    for text in texts:
        if not text:
            continue
        for match in re.finditer(r"(?:\+?91[\s\-\.]*)?(?:\d[\s\-\.]*){10,}", str(text)):
            digits = normalize_phone_number(match.group(0))
            # Strip a well-defined country/trunk prefix only — never fabricate a
            # 10-digit number by blindly slicing an over-long digit run.
            if len(digits) == 12 and digits.startswith("91"):
                digits = digits[2:]
            elif len(digits) == 11 and digits.startswith("0"):
                digits = digits[1:]
            if len(digits) == 10 and digits.isdigit():
                return digits
    return None

# Redis configuration for agents_config caching
try:
    import redis
except ImportError:
    redis = None
    logger.warning("redis package not installed; agents_config caching will be disabled")

REDIS_URL = os.getenv("REDIS_URL") or os.getenv("REDIS_CONNECTION_STRING") or "redis://localhost:6379/0"
_redis_client = None
_redis_guard = RedisGuard()

def _get_redis_client():
    """Get or create Redis client for agents_config caching"""
    global _redis_client
    if _redis_client is None:
        if not redis:
            return None
        try:
            client_kwargs = {"decode_responses": True}
            try:
                if str(REDIS_URL).lower().startswith("rediss://"):
                    # Provide CA bundle for TLS verification (Upstash/External Redis over TLS)
                    client_kwargs["ssl_ca_certs"] = certifi.where()
                    # Optional: allow disabling verification for local testing only
                    insecure = (os.getenv("REDIS_SSL_INSECURE", "").lower() in ("1", "true", "yes"))
                    if insecure:
                        client_kwargs["ssl_cert_reqs"] = None
            except Exception:
                pass
            _redis_client = redis.Redis.from_url(REDIS_URL, **client_kwargs)
            # Ping once to validate
            _redis_client.ping()
            logger.info("Connected to Redis for agents_config caching")
        except Exception as ex:
            logger.error(f"Failed to connect to Redis for agents_config caching: {ex}")
            _redis_client = None
    return _redis_client

def _sanitize_redis_url(url: str) -> str:
    """Sanitizes a Redis URL for logging by removing sensitive information."""
    if "://" in url:
        parts = url.split("://", 1)
        return f"{parts[0]}://***"
    return url

from fashion_bot.database_manager import get_async_postgres_connection, awith_retry


# ==================== ASYNC AGENT PROMPT CACHING ====================

# Async Redis client is shared across config_manager / gupshup_webhook
# / utils via utils.redis_client — one process-wide instance.
from fashion_bot.utils.redis_client import get_shared_async_redis_client as _get_async_redis_client_utils  # noqa: E402


async def aget_agents_config_from_cache(client_id: str, agent_name: str) -> Optional[str]:
    """Async: get agent prompt from Redis cache."""
    try:
        client = await _get_async_redis_client_utils()
        if not client:
            return None
        cache_key = f"agents_config:{client_id}"
        cached_data = await client.get(cache_key)
        if cached_data:
            agents_config = json.loads(cached_data)
            for agent in agents_config:
                if agent.get("agent_name") == agent_name:
                    logger.debug(f"prompt cache hit: {agent_name}")
                    return agent.get("agent_prompt")
            return None
        return None
    except Exception as e:
        logger.error(f"❌ Error fetching agents_config from async Redis cache: {e}")
        return None


async def acache_agents_config(client_id: str, agents_config: List[Dict[str, Any]], ttl_seconds: int = 600) -> bool:
    """Async: cache agents_config data in Redis with TTL."""
    try:
        client = await _get_async_redis_client_utils()
        if not client:
            return False
        cache_key = f"agents_config:{client_id}"
        cached_data = json.dumps(agents_config)
        await client.setex(cache_key, ttl_seconds, cached_data)
        logger.info(f"📦 Async cached agents_config for client {client_id} with {ttl_seconds}s TTL ({len(agents_config)} agents)")
        return True
    except Exception as e:
        logger.error(f"❌ Error async caching agents_config in Redis: {e}")
        return False


@awith_retry
async def aget_agents_config_from_db(client_id: str) -> List[Dict[str, Any]]:
    """
    Async: fetch all agents_config from PostgreSQL for a specific client_id.
    Automatically retries on connection errors.
    """
    client_id_str = str(client_id).strip() if client_id else ""
    if not client_id_str:
        logger.warning("⚠️ Empty client_id provided to aget_agents_config_from_db, using default")
        from fashion_bot.config_manager import aresolve_client_id
        client_id = await aresolve_client_id()
    else:
        client_id = client_id_str

    if not client_id:
        logger.error("❌ No valid client_id available for aget_agents_config_from_db; skipping DB query")
        return []

    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT agent_name, agent_prompt, created_at, updated_at
                FROM agents_config
                WHERE client_id = %s
            """, (client_id,))
            results = await cur.fetchall()
            if results:
                agents_config = []
                for row in results:
                    agents_config.append({
                        "agent_name": row['agent_name'],
                        "agent_prompt": row['agent_prompt'],
                        "created_at": row['created_at'].isoformat() if row['created_at'] else None,
                        "updated_at": row['updated_at'].isoformat() if row['updated_at'] else None
                    })
                logger.info(f"📖 Async loaded {len(agents_config)} agents_config from database for client: {client_id}")
                return agents_config
            else:
                logger.warning(f"⚠️ No agents_config found in database for client: {client_id}")
                return []


async def aget_agent_prompt_with_caching(client_id: str, agent_name: str) -> Optional[str]:
    """
    Async: get agent prompt with tiered caching (memory -> Redis -> DB).
    Drop-in async replacement for get_agent_prompt_with_caching.
    """
    if not client_id:
        from fashion_bot.config_manager import aresolve_client_id
        client_id = await aresolve_client_id()
    client_id = str(client_id).strip() if client_id else ""
    if not client_id:
        logger.error(f"❌ No valid client_id for {agent_name}; cannot load prompt")
        return None

    cache_key = f"mem_prompt:{client_id}:{agent_name}"

    async def _load_from_db() -> Optional[str]:
        logger.info(f"🔄 Async cache miss for {agent_name}, fetching agents_config from database for client: {client_id}")
        agents_config = await aget_agents_config_from_db(client_id)
        if not agents_config:
            return None
        await acache_agents_config(client_id, agents_config, ttl_seconds=600)
        for agent in agents_config:
            if agent.get("agent_name") == agent_name:
                return agent.get("agent_prompt")
        return None

    prompt, source = await aget_with_tiered_cache(
        cache_key=cache_key,
        ttl_seconds=600,
        load_from_source_fn=_load_from_db,
        get_from_redis_fn=lambda: aget_agents_config_from_cache(client_id, agent_name),
    )
    if prompt:
        logger.debug(f"📖 Agent prompt loaded from {source} cache tier for {agent_name}, client={client_id}")
        return prompt
    logger.warning(f"⚠️ Agent {agent_name} not found for client: {client_id}")
    return None


# ==================== TAGS CACHING ====================

def get_tags_from_cache(client_id: str) -> Optional[Dict[str, str]]:
    """
    Get tags from Redis cache.
    
    Args:
        client_id: The client ID
        
    Returns:
        Tags dictionary if found in cache, None otherwise
    """
    try:
        client = _get_redis_client()
        if not client:
            return None
            
        cache_key = f"tags:{client_id}"
        guard_result = _redis_guard.execute(
            op_name="tags_cache_get",
            fn=lambda: client.get(cache_key),
            fallback=None,
        )
        cached_data = guard_result.value
        
        if cached_data:
            tags = json.loads(cached_data)
            logger.info(f"📖 Found tags in Redis cache for client: {client_id}")
            return tags
        else:
            logger.info(f"⚠️ No tags cache found in Redis for client: {client_id}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Error fetching tags from Redis cache: {str(e)}")
        return None


def cache_tags(client_id: str, tags: Dict[str, str], ttl_seconds: int = 600) -> bool:
    """
    Cache tags data in Redis with TTL.
    
    Args:
        client_id: The client ID
        tags: Tags dictionary
        ttl_seconds: Time to live in seconds (default: 600 = 10 minutes)
        
    Returns:
        True if cached successfully, False otherwise
    """
    try:
        client = _get_redis_client()
        if not client:
            return False
            
        cache_key = f"tags:{client_id}"
        cached_data = json.dumps(tags)
        
        guard_result = _redis_guard.execute(
            op_name="tags_cache_setex",
            fn=lambda: client.setex(cache_key, ttl_seconds, cached_data),
            fallback=False,
        )
        if guard_result.ok:
            logger.info(f"📦 Cached tags for client {client_id} with {ttl_seconds}s TTL ({len(tags)} tags)")
            return True
        logger.warning(f"⚠️ Failed to cache tags for client {client_id}: {guard_result.error}")
        return False
        
    except Exception as e:
        logger.error(f"❌ Error caching tags in Redis: {str(e)}")
        return False


async def aget_tags_with_caching(client_id: str = None) -> Dict[str, str]:
    """Async version of get_tags_with_caching using async tiered cache."""
    if not client_id:
        logger.warning("⚠️ aget_tags_with_caching called without client_id")

    cache_client_id = client_id or "default"
    cache_key = f"mem_tags:{cache_client_id}"

    async def _aload_from_db() -> Optional[Dict[str, str]]:
        logger.info(f"🔄 Async cache miss for tags, fetching from database for client: {cache_client_id}")
        try:
            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    if client_id:
                        await cur.execute("""
                            SELECT config_value
                            FROM client_configs
                            WHERE config_key = 'tags' AND client_id = %s
                            LIMIT 1
                        """, (client_id,))
                    else:
                        await cur.execute("""
                            SELECT config_value
                            FROM client_configs
                            WHERE config_key = 'tags'
                            LIMIT 1
                        """)

                    result = await cur.fetchone()

                    if not result:
                        return {}
                    config_value = result.get('config_value') if isinstance(result, dict) else result[0]
                    if not config_value:
                        return {}
                    return config_value if isinstance(config_value, dict) else json.loads(config_value)
        except Exception as e:
            logger.error(f"❌ Error async fetching tags from database: {str(e)}")
            return {}

    async def _aget_from_redis() -> Optional[Dict[str, str]]:
        try:
            client = await _get_async_redis_client_utils()
            cached_data = await client.get(f"tags:{cache_client_id}")
            if cached_data:
                return json.loads(cached_data)
            return None
        except Exception:
            return None

    async def _aset_to_redis(val: Dict[str, str]) -> None:
        try:
            client = await _get_async_redis_client_utils()
            await client.setex(f"tags:{cache_client_id}", 600, json.dumps(val or {}))
        except Exception:
            pass

    tags, source = await aget_with_tiered_cache(
        cache_key=cache_key,
        ttl_seconds=600,
        load_from_source_fn=_aload_from_db,
        get_from_redis_fn=_aget_from_redis,
        set_to_redis_fn=_aset_to_redis,
    )
    tags = tags or {}
    logger.debug(f"📖 Tags loaded from {source} tier (async) for client={cache_client_id}")
    return tags


# ==================== CONVERSION TAGS CACHING ====================

def get_conversion_tags_from_cache(client_id: str) -> Optional[Dict[str, str]]:
    """
    Get conversion tags from Redis cache.
    
    Args:
        client_id: The client ID
        
    Returns:
        Conversion tags dictionary if found in cache, None otherwise
    """
    try:
        client = _get_redis_client()
        if not client:
            return None
            
        cache_key = f"conversion_tags:{client_id}"
        cached_data = client.get(cache_key)
        
        if cached_data:
            tags = json.loads(cached_data)
            logger.info(f"📖 Found conversion_tags in Redis cache for client: {client_id}")
            return tags
        else:
            return None
            
    except Exception as e:
        logger.error(f"❌ Error fetching conversion_tags from Redis cache: {str(e)}")
        return None


def cache_conversion_tags(client_id: str, tags: Dict[str, str], ttl_seconds: int = 600) -> bool:
    """
    Cache conversion tags data in Redis with TTL.
    
    Args:
        client_id: The client ID
        tags: Conversion tags dictionary
        ttl_seconds: Time to live in seconds (default: 600 = 10 minutes)
        
    Returns:
        True if cached successfully, False otherwise
    """
    try:
        client = _get_redis_client()
        if not client:
            return False
            
        cache_key = f"conversion_tags:{client_id}"
        cached_data = json.dumps(tags)
        
        client.setex(cache_key, ttl_seconds, cached_data)
        logger.info(f"📦 Cached conversion_tags for client {client_id} with {ttl_seconds}s TTL ({len(tags)} tags)")
        return True
        
    except Exception as e:
        logger.error(f"❌ Error caching conversion_tags in Redis: {str(e)}")
        return False


async def aget_conversion_tags_with_caching(client_id: str = None) -> Dict[str, str]:
    """Async version of get_conversion_tags_with_caching."""
    if not client_id:
        logger.warning("⚠️ aget_conversion_tags_with_caching called without client_id")
        return {}

    # Step 1: Try async Redis cache
    try:
        redis_client = await _get_async_redis_client_utils()
        cache_key = f"conversion_tags:{client_id}"
        cached_data = await redis_client.get(cache_key)
        if cached_data:
            tags = json.loads(cached_data)
            logger.info(f"📖 Found conversion_tags in async Redis cache for client: {client_id}")
            return tags
    except Exception as e:
        logger.error(f"❌ Error async fetching conversion_tags from Redis: {str(e)}")

    # Step 2: Cache miss - fetch from database
    logger.info(f"🔄 Async cache miss for conversion_tags, fetching from database for client: {client_id}")
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT config_value
                    FROM client_configs
                    WHERE config_key = 'conversion_tags' AND client_id = %s
                    LIMIT 1
                """, (client_id,))

                result = await cur.fetchone()

                if result:
                    config_value = result.get('config_value') if isinstance(result, dict) else result[0]
                    if config_value:
                        tags = config_value if isinstance(config_value, dict) else json.loads(config_value)
                        logger.info(f"📖 Loaded {len(tags)} conversion_tags from database (async) for client: {client_id}")

                        # Step 3: Cache in Redis
                        try:
                            redis_client = await _get_async_redis_client_utils()
                            await redis_client.setex(f"conversion_tags:{client_id}", 600, json.dumps(tags))
                        except Exception:
                            pass

                        return tags

        logger.warning(f"⚠️ No conversion_tags found in database for client: {client_id}")
        return {}

    except Exception as e:
        logger.error(f"❌ Error async fetching conversion_tags from database: {str(e)}")
        return {}


# ==================== WEBSITE URLS CACHING ====================

def get_website_urls_from_cache(client_id: str) -> Optional[Dict[str, str]]:
    """
    Get website URLs from Redis cache.
    
    Args:
        client_id: The client ID
        
    Returns:
        Website URLs dictionary if found in cache, None otherwise
    """
    try:
        client = _get_redis_client()
        if not client:
            return None
            
        cache_key = f"website_urls:{client_id}"
        guard_result = _redis_guard.execute(
            op_name="website_urls_cache_get",
            fn=lambda: client.get(cache_key),
            fallback=None,
        )
        cached_data = guard_result.value
        
        if cached_data:
            urls = json.loads(cached_data)
            logger.info(f"📖 Found website_urls in Redis cache for client: {client_id}")
            return urls
        else:
            logger.info(f"⚠️ No website_urls cache found in Redis for client: {client_id}")
            return None
            
    except Exception as e:
        logger.error(f"❌ Error fetching website_urls from Redis cache: {str(e)}")
        return None


def cache_website_urls(client_id: str, urls: Dict[str, str], ttl_seconds: int = 600) -> bool:
    """
    Cache website URLs data in Redis with TTL.
    
    Args:
        client_id: The client ID
        urls: Website URLs dictionary
        ttl_seconds: Time to live in seconds (default: 600 = 10 minutes)
        
    Returns:
        True if cached successfully, False otherwise
    """
    try:
        client = _get_redis_client()
        if not client:
            return False
            
        cache_key = f"website_urls:{client_id}"
        cached_data = json.dumps(urls)
        
        guard_result = _redis_guard.execute(
            op_name="website_urls_cache_setex",
            fn=lambda: client.setex(cache_key, ttl_seconds, cached_data),
            fallback=False,
        )
        if guard_result.ok:
            logger.info(f"📦 Cached website_urls for client {client_id} with {ttl_seconds}s TTL")
            return True
        logger.warning(f"⚠️ Failed to cache website_urls for client {client_id}: {guard_result.error}")
        return False
        
    except Exception as e:
        logger.error(f"❌ Error caching website_urls in Redis: {str(e)}")
        return False


async def aget_website_urls_with_caching(client_id: str = None) -> Dict[str, str]:
    """Async version of get_website_urls_with_caching."""
    cache_client_id = client_id or "default"
    cache_key = f"mem_website_urls:{cache_client_id}"

    async def _aload_from_db() -> Optional[Dict[str, str]]:
        logger.info(f"🔄 Async cache miss for website_urls, fetching from database for client: {cache_client_id}")
        try:
            from fashion_bot.config_manager import aget_config, aget_shopify_config

            shopify_config = await aget_shopify_config(client_id)
            shopify_url = shopify_config.get("shop_url", "") if shopify_config else ""
            website_config = await aget_config('website_urls', client_id=client_id)

            if isinstance(website_config, dict):
                website_url = website_config.get("website_url",
                             website_config.get("website",
                             website_config.get("home",
                             list(website_config.values())[0] if website_config else "")))
                return {
                    "shopify_url": f"https://{shopify_url}" if shopify_url and not shopify_url.startswith("http") else shopify_url,
                    "website_url": website_url,
                    **website_config
                }
            if isinstance(website_config, str):
                return {
                    "shopify_url": f"https://{shopify_url}" if shopify_url and not shopify_url.startswith("http") else shopify_url,
                    "website_url": website_config
                }
            return {
                "shopify_url": f"https://{shopify_url}" if shopify_url and not shopify_url.startswith("http") else shopify_url,
                "website_url": ""
            }
        except Exception as e:
            logger.error(f"❌ Error async fetching website_urls: {str(e)}")
            return {}

    async def _aget_from_redis() -> Optional[Dict[str, str]]:
        try:
            client = await _get_async_redis_client_utils()
            cached_data = await client.get(f"website_urls:{cache_client_id}")
            if cached_data:
                return json.loads(cached_data)
            return None
        except Exception:
            return None

    async def _aset_to_redis(val: Dict[str, str]) -> None:
        try:
            client = await _get_async_redis_client_utils()
            await client.setex(f"website_urls:{cache_client_id}", 600, json.dumps(val or {}))
        except Exception:
            pass

    urls, source = await aget_with_tiered_cache(
        cache_key=cache_key,
        ttl_seconds=600,
        load_from_source_fn=_aload_from_db,
        get_from_redis_fn=_aget_from_redis,
        set_to_redis_fn=_aset_to_redis,
    )
    urls = urls or {}
    logger.debug(f"📖 Website URLs loaded from {source} tier (async) for client={cache_client_id}")
    return urls


# ==================== ORDER UPDATE RULES CACHING ====================

# IMPORTANT: The REAL rules live in the DB (client_configs table,
# config_key = 'order_update_rules').  Each client can have different
# rules depending on their adapters and business logic.
#
# Seed / update DB rules with:
#   python scripts/seed_order_update_rules.py --client-id <UUID>
#
# This hardcoded fallback is a SAFETY NET — it is only used when BOTH
# Redis and DB are unreachable.  Keep it conservative (block terminal
# statuses for all update types).
_FALLBACK_ORDER_UPDATE_RULES: Dict[str, Any] = {
    "name":    {"direct_update_statuses": ["NEW"], "blocked_statuses": ["DELIVERED", "CANCELLED", "RTO", "UNDELIVERED"], "blocked_message": "Update not allowed for current order status."},
    "email":   {"direct_update_statuses": ["NEW"], "blocked_statuses": ["DELIVERED", "CANCELLED", "RTO", "UNDELIVERED"], "blocked_message": "Update not allowed for current order status."},
    "address": {"direct_update_statuses": ["NEW"], "blocked_statuses": ["DELIVERED", "CANCELLED", "RTO", "UNDELIVERED"], "blocked_message": "Update not allowed for current order status."},
    "phone":   {"direct_update_statuses": ["NEW"], "blocked_statuses": ["DELIVERED", "CANCELLED", "RTO", "UNDELIVERED"], "blocked_message": "Update not allowed for current order status."},
    "size":    {"direct_update_statuses": ["NEW"], "blocked_statuses": ["DELIVERED", "CANCELLED", "RTO", "UNDELIVERED"], "blocked_message": "Update not allowed for current order status."},
    "product": {"direct_update_statuses": ["NEW"], "blocked_message": "Update not allowed for current order status."},
}


async def aget_order_update_rules(client_id: Optional[str] = None) -> Dict[str, Any]:
    """Async version of get_order_update_rules."""
    cache_client_id = client_id or "default"

    # 1. Try async Redis
    try:
        redis_client = await _get_async_redis_client_utils()
        cache_key = f"order_update_rules:{cache_client_id}"
        cached = await redis_client.get(cache_key)
        if cached:
            logger.debug(f"📖 order_update_rules async cache hit for {cache_client_id}")
            return json.loads(cached)
    except Exception as e:
        logger.warning(f"⚠️ Async Redis read error for order_update_rules: {e}")

    # 2. Try DB via async config helper
    try:
        from fashion_bot.config_manager import aget_config
        raw = await aget_config("order_update_rules", client_id=client_id)
        if raw:
            rules = json.loads(raw) if isinstance(raw, str) else raw
            # Cache in async Redis
            try:
                rc = await _get_async_redis_client_utils()
                await rc.setex(
                    f"order_update_rules:{cache_client_id}",
                    600,
                    json.dumps(rules),
                )
            except Exception:
                pass
            logger.info(f"📖 Loaded order_update_rules from DB (async) for {cache_client_id}")
            return rules
    except Exception as e:
        logger.warning(f"⚠️ Async DB read error for order_update_rules: {e}")

    logger.warning(f"⚠️ Using FALLBACK order_update_rules for {cache_client_id}")
    return _FALLBACK_ORDER_UPDATE_RULES


def get_trace_id(state: Any) -> str:
    """Get trace ID from state or generate a new one."""
    if hasattr(state, 'get'):
        return state.get('trace_id', str(uuid.uuid4())[:8])
    return str(uuid.uuid4())[:8]

def log_with_trace_id(state: Any, message: str, level: str = "info", client_id: str = None):
    """Log message. Trace ID auto-injected by TraceIdFilter in formatter."""
    getattr(logger, level, logger.info)(message)

def check_missing_vars(state: Any, required_vars: List[Dict]) -> List[Dict]:
    missing = []
    for var in required_vars:
        var_name = var["var_name"]
        state_map = var["state_map"]
        if not state.get(state_map):
            missing.append(var)
    return missing

def merge_customer_messages(missing_vars: List[Dict]) -> str:
    messages = [var["customer_message"] for var in missing_vars]
    if len(messages) == 1:
        return messages[0]
    else:
        return "I need a few details first. " + " Also, ".join(messages)

def format_for_whatsapp(message: str) -> str:
    # Add WhatsApp-friendly formatting
    lower_msg = message.lower()
    
    # More specific matching to avoid false positives
    if "order status" in lower_msg or "tracking" in lower_msg or "order placed" in lower_msg:
        message = "📦 " + message
    elif "delivery" in lower_msg or "shipping" in lower_msg:
        message = "🚚 " + message
    elif "return" in lower_msg or "exchange" in lower_msg:
        message = "🔄 " + message
    elif "size guide" in lower_msg or "size chart" in lower_msg or "what size" in lower_msg or "which size" in lower_msg:
        # Only add size emoji for actual size guide/recommendation queries
        message = "📏 " + message
    elif "discount" in lower_msg or "offer" in lower_msg or "coupon" in lower_msg:
        message = "� " + message
    if any(word in message.lower() for word in ["help", "assist", "support", "information"]):
        if not message.endswith("!") and not message.endswith("?"):
            message += " 😊"
    if len(message) > 550:
        if "collections" not in message.lower() and "size guide" not in message.lower() and "size chart" not in message.lower() and "categories" not in message.lower() and "coupon" not in message.lower() and "multiple products" not in message.lower():
            message = message[:547] + "..."
    return message



def get_current_environment() -> str:
    """
    Detect current environment from various sources
    
    Priority order:
    1. ENVIRONMENT env var
    2. NODE_ENV env var (for Node.js compatibility)
    3. DEPLOY_ENV env var
    4. Default to 'development' if none found
    
    Returns:
        Environment: Current environment
    """
    env_vars = ["ENVIRONMENT", "NODE_ENV", "DEPLOY_ENV"]
    
    for var in env_vars:
        env_value = os.getenv(var, "").lower()
        if env_value in ["production", "prod"]:
            return "production"
        elif env_value in ["staging", "stage"]:
            return "staging"
        elif env_value in ["development", "dev", "local"]:
            return "development"
    
    # Default to development
    logger.info("No environment specified, defaulting to 'development'")
    return "development"

def _safe_call(tool_fn, *args, **kwargs):
    fn = tool_fn.fn if hasattr(tool_fn, "fn") else tool_fn
    return fn(*args, **kwargs)


def should_fetch_template_messages(state: Dict[str, Any], inactivity_threshold_minutes: int = 30) -> bool:
    """
    Determine whether to fetch template messages based on conversation state.
    
    This function implements a smart check to balance context retrieval with DB call efficiency.
    
    Conditions to fetch template messages:
    1. No messages or very few messages (new conversation)
    2. All messages are from the user (no bot responses yet - effectively new conversation)
    3. OR the last message is older than the inactivity threshold (user returned after a gap)
    4. OR the current message is a short confirmation-like response (could be replying to a template)
    
    This catches the scenario where:
    - User chats with agent
    - User creates an order
    - User receives a template message (broadcast/transactional)
    - User returns to chat - we need to fetch template context
    
    Args:
        state: The SupportState dictionary containing messages and last_message_at
        inactivity_threshold_minutes: Minutes of inactivity after which to refresh template context (default: 30)
        
    Returns:
        True if template messages should be fetched, False otherwise
    """
    messages = state.get("messages", [])
    
    # Condition 1: New conversation or very few messages
    if len(messages) <= 1:
        logger.info("should_fetch_template_messages: True (new conversation, messages <= 1)")
        return True
    
    # Condition 4 (check early): Current message is a short confirmation-like response
    # These are typically responses to template buttons (COD confirmation, delivery confirmation, etc.)
    if messages:
        current_msg = messages[-1]
        current_content = getattr(current_msg, 'content', '') if hasattr(current_msg, 'content') else str(current_msg)
        current_lower = current_content.lower().strip()
        
        # Short confirmation-like responses that could be template button clicks
        template_response_words = [
            'confirm', 'yes', 'ok', 'okay', 'no', 'cancel', 'reschedule',
            'ha', 'haan', 'ji', 'nahi', 'nope', 'proceed', 'done', 'sure',
            'not now', 'later', 'tomorrow', 'acha', 'theek', 'thik'
        ]
        
        # Only fetch if message is short (likely a button click, not a detailed query)
        if len(current_lower) <= 10:
            for word in template_response_words:
                if current_lower == word or current_lower.startswith(word + ' ') or current_lower.endswith(' ' + word):
                    logger.info(f"should_fetch_template_messages: True (short confirmation-like response: '{current_content}')")
                    return True
    
    # Condition 2: Check if there are any bot responses in the conversation
    # If all messages are human/user messages, this is effectively a new conversation
    # (This handles the case where the same message was added twice due to webhook flow)
    has_bot_response = False
    for msg in messages:
        # Check for AI/bot message types
        msg_type = getattr(msg, 'type', None)
        if msg_type in ('ai', 'assistant', 'bot', 'system'):
            has_bot_response = True
            break
        # Also check for AIMessage class
        if hasattr(msg, '__class__') and 'AIMessage' in msg.__class__.__name__:
            has_bot_response = True
            break
    
    if not has_bot_response:
        logger.info(f"should_fetch_template_messages: True (no bot responses in {len(messages)} messages - effectively new conversation)")
        return True
    
    # Condition 3: Check if last message is older than threshold using state.last_message_at
    try:
        last_message_at_str = state.get("last_message_at")
        
        if not last_message_at_str:
            # No timestamp available - don't fetch to avoid unnecessary DB calls
            logger.debug("should_fetch_template_messages: False (no last_message_at in state)")
            return False
        
        # Parse ISO timestamp
        try:
            last_message_at = datetime.datetime.fromisoformat(last_message_at_str)
        except ValueError:
            logger.warning(f"Invalid last_message_at format: {last_message_at_str}")
            return False
        
        # Use UTC for consistent comparison
        now = datetime.datetime.now(datetime.timezone.utc)
        
        # Ensure last_message_at is timezone-aware (UTC)
        if last_message_at.tzinfo is None:
            # Assume naive datetime is UTC (as set by state_cache.py)
            last_message_at = last_message_at.replace(tzinfo=datetime.timezone.utc)
        
        time_diff = now - last_message_at
        threshold = datetime.timedelta(minutes=inactivity_threshold_minutes)
        
        if time_diff > threshold:
            logger.info(f"should_fetch_template_messages: True (last message {time_diff.total_seconds()/60:.1f} mins old, threshold: {inactivity_threshold_minutes} mins)")
            return True
        else:
            logger.debug(f"should_fetch_template_messages: False (last message {time_diff.total_seconds()/60:.1f} mins old, within threshold)")
            return False
            
    except Exception as e:
        logger.warning(f"Error checking last_message_at in should_fetch_template_messages: {e}")
        # On error, be conservative and don't fetch
        return False


def send_email(from_address, to_addresses, cc_addresses, subject, message, html_body=None):
    # SMTP settings are read from environment variables; the previous hardcoded
    # values are kept as fallback defaults so existing deployments that have not
    # yet set the env vars keep working unchanged. Move SMTP_PASSWORD into a
    # secret and drop the default token once every environment sets it.
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.zeptomail.in")
    username = os.environ.get("SMTP_USERNAME", "emailapikey")
    token = os.environ.get(
        "SMTP_PASSWORD",
        "PHtE6r0FEOno2md88hcHsPGwFML3PIgt9bxkeABCsYcRD/cHH00H/94vkmfl/xl7XfUREfOcwYw7577Ku7iFJ23sYTwdX2qyqK3sx/VYSPOZsbq6x00atV8fcETZVofrcNFr3CHUs93ZNA==",
    )
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
    except (TypeError, ValueError):
        port = 587

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = from_address
    msg['To'] = to_addresses if isinstance(to_addresses, str) else ", ".join(to_addresses)
    if cc_addresses:
        msg['Cc'] = cc_addresses if isinstance(cc_addresses, str) else ", ".join(cc_addresses)
    msg.set_content(message)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    try:
        if port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_server, port, context=context) as server:
                server.login(username, token)
                server.send_message(msg)
        elif port == 587:
            with smtplib.SMTP(smtp_server, port) as server:
                server.starttls()
                server.login(username, token)
                server.send_message(msg)
        else:
            print("Use port 465 or 587 only.")
            return

        print("✅ Email sent successfully!")
    except Exception as e:
        print(f"❌ Failed to send email: {e}")


async def asend_email(from_address, to_addresses, cc_addresses, subject, message, html_body=None):
    """Async entry point for sending email.

    ``smtplib`` is a synchronous library (and ``aiosmtplib`` is not a
    dependency), so the blocking send is offloaded to a worker thread via
    ``asyncio.to_thread`` to keep the event loop unblocked (AGENTS.md §1).
    Prefer this over calling ``send_email`` directly from async code.
    """
    await asyncio.to_thread(
        send_email, from_address, to_addresses, cc_addresses, subject, message, html_body
    )


@awith_retry
async def aget_recent_template_messages(phone_number: str, client_id: str = None, limit: int = 3) -> List[str]:
    """
    Async: fetch the last N messages from template_delivery_logs for a phone number.
    Uses async postgres connection. Automatically retries on connection errors.
    """
    if not phone_number:
        return []

    # Normalize phone number - try multiple formats
    phone_variants = []
    clean_phone = phone_number.lstrip('+')
    phone_variants.append(phone_number)
    if phone_number.startswith('+'):
        phone_variants.append(clean_phone)
    if clean_phone.startswith('91') and len(clean_phone) > 10:
        phone_variants.append(clean_phone[2:])
        phone_variants.append(f"+{clean_phone}")
    if len(clean_phone) == 10:
        phone_variants.append(f"91{clean_phone}")
        phone_variants.append(f"+91{clean_phone}")
    if len(clean_phone) == 12 and clean_phone.startswith('91'):
        phone_variants.append(f"+{clean_phone}")
        phone_variants.append(clean_phone[2:])
    phone_variants = list(dict.fromkeys(phone_variants))

    client_id_str = str(client_id) if client_id else None
    placeholders = ", ".join(["%s"] * len(phone_variants))

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                if client_id_str:
                    query = f"""
                        SELECT template_message, sent_at
                        FROM template_delivery_logs
                        WHERE phone_number IN ({placeholders})
                        AND client_id = %s
                        AND sent_at >= NOW() - INTERVAL '10 days'
                        ORDER BY sent_at DESC
                        LIMIT %s
                    """
                    await cur.execute(query, (*phone_variants, client_id_str, limit))
                else:
                    query = f"""
                        SELECT template_message, sent_at
                        FROM template_delivery_logs
                        WHERE phone_number IN ({placeholders})
                        AND sent_at >= NOW() - INTERVAL '10 days'
                        ORDER BY sent_at DESC
                        LIMIT %s
                    """
                    await cur.execute(query, (*phone_variants, limit))

                rows = await cur.fetchall()
                if rows:
                    messages = []
                    for row in rows:
                        msg = row.get('template_message') if isinstance(row, dict) else (row[0] if row else None)
                        if msg:
                            messages.append(msg)
                    logger.info(f"📨 Async found {len(messages)} template messages for phone {phone_number}")
                    return messages
                return []
    except Exception as e:
        logger.error(f"❌ Async error fetching template messages: {e}")
        return []


# Matches the tracking-link placeholder the order_status_handler prompt
# template carries, e.g. "track the order here: [tracking_url]".
_TRACKING_URL_PLACEHOLDER_RE = re.compile(r"\[tracking_url\]", re.IGNORECASE)

# Drops a dangling "track ... here: [tracking_url]" lead-in when no real URL is
# available, so the customer never sees raw template syntax.
_TRACKING_URL_SENTENCE_RE = re.compile(
    r"\s*You can track (?:the|your) order here:\s*\[tracking_url\]\.?",
    re.IGNORECASE,
)


def _extract_tracking_url_from_steps(intermediate_steps) -> str:
    """Pull a real carrier tracking URL out of tool observations.

    Understands both order_status tool shapes:
      - get_order_details -> obs["tracking"]["tracking_url"]
      - get_recent_orders -> obs["orders"][*]["tracking_url"]
    Returns the first non-empty http(s) URL found, else "".
    """
    for step in (intermediate_steps or []):
        if len(step) < 2:
            continue
        obs = step[1]
        if not isinstance(obs, dict):
            continue
        tracking = obs.get("tracking")
        if isinstance(tracking, dict):
            url = (tracking.get("tracking_url") or "").strip()
            if url.startswith("http"):
                return url
        url = (obs.get("tracking_url") or "").strip()
        if url.startswith("http"):
            return url
        for order in (obs.get("orders") or []):
            if isinstance(order, dict):
                url = (order.get("tracking_url") or "").strip()
                if url.startswith("http"):
                    return url
    return ""


def _resolve_tracking_url_placeholder(text, intermediate_steps, state=None):
    """Deterministic safety net for a leaked ``[tracking_url]`` placeholder.

    The order_status_handler prompt template asks the LLM to swap
    ``[tracking_url]`` for the real tracking link. That substitution is purely
    LLM-driven and fails when the model lacks the URL (e.g. it answered from
    get_recent_orders, which historically omitted it) — leaking the raw
    placeholder to the customer. Here we fill it from tool output when possible,
    or strip the dangling phrasing when no URL is available.
    """
    if not isinstance(text, str) or not text:
        return text
    if not _TRACKING_URL_PLACEHOLDER_RE.search(text):
        return text

    real_url = _extract_tracking_url_from_steps(intermediate_steps)
    if real_url:
        resolved = _TRACKING_URL_PLACEHOLDER_RE.sub(real_url, text)
        if state is not None:
            log_with_trace_id(
                state, "🔗 Substituted leaked [tracking_url] placeholder with real tracking link"
            )
        return resolved

    cleaned = _TRACKING_URL_SENTENCE_RE.sub("", text)
    cleaned = _TRACKING_URL_PLACEHOLDER_RE.sub("", cleaned)
    if state is not None:
        log_with_trace_id(
            state,
            "⚠️ [tracking_url] placeholder present but no tracking URL available; removed placeholder",
            "warning",
        )
    return cleaned.strip()


# Any http(s) URL in a string. Trailing sentence punctuation is trimmed
# separately so "…/tracking/123." compares equal to a tool's "…/tracking/123".
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_URL_TRAILING_PUNCT = ".,;:!?)]}>\"'*"

# Depth cap for the walk below — tool results are shallow; this only stops a
# pathological structure from costing real time.
_URL_SCAN_MAX_DEPTH = 6


def _normalize_url(url: str) -> str:
    """Comparison key for a URL: punctuation-trimmed, slash-trimmed, casefolded."""
    return url.rstrip(_URL_TRAILING_PUNCT).rstrip("/").casefold()


def _walk_urls(node, out: List[str], depth: int = 0) -> None:
    """Collect every http(s) URL found in any string inside a tool result."""
    if depth > _URL_SCAN_MAX_DEPTH:
        return
    if isinstance(node, str):
        out.extend(_URL_RE.findall(node))
    elif isinstance(node, dict):
        for value in node.values():
            _walk_urls(value, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk_urls(item, out, depth + 1)


def collect_tool_result_urls(intermediate_steps) -> List[str]:
    """Every URL the tools actually returned this turn.

    Walks the whole observation rather than named keys, so it covers every tool
    shape without a per-tool list to keep in sync — ``tracking.tracking_url``,
    ``tracking.parcels[*]``, ``orders[*]``, and anything added later.
    """
    urls: List[str] = []
    for step in (intermediate_steps or []):
        if len(step) < 2:
            continue
        _walk_urls(step[1], urls)
    return urls


def strip_unsourced_urls(text, allowed_urls, state=None):
    """Remove any URL from the reply that we did not give the agent.

    The order_status agent quotes tracking links. On 2026-08-24 it quoted one no
    tool had returned — ``https://shiprocket.co/tracking/1234567890``, a
    placeholder waybill sent to a paying customer — because it applied the
    prompt's "Out for Delivery" script to a status it had read in conversation
    history, with no order data in the turn to fill the link from.

    ``allowed_urls`` is everything we handed the model: this turn's tool results
    plus any URL written into its own prompt (two tenants' order_status prompts
    carry one — Rare Rabbit's returns portal, Concept Groove's Instagram — and
    those must survive). A URL in neither set was invented.

    The check is deliberately deterministic — set membership over what we
    supplied. A guard against model fabrication must not itself be a model, or
    its failure modes correlate with the thing it guards.

    It only ever REMOVES; it never substitutes a "better" link, because choosing
    one would be guessing which parcel the sentence meant. The surrounding
    sentence is left exactly as written — no prose surgery, which would mean
    pattern-matching English in a bot that also answers in Hindi and Hinglish.
    """
    if not isinstance(text, str) or not text:
        return text
    allowed = {_normalize_url(u) for u in (allowed_urls or [])}

    removed = []

    def _replace(match):
        url = match.group(0)
        trimmed = url.rstrip(_URL_TRAILING_PUNCT)
        if _normalize_url(trimmed) in allowed:
            return url
        removed.append(trimmed)
        # Keep the trailing punctuation the model wrote; drop only the URL.
        return url[len(trimmed):]

    cleaned = _URL_RE.sub(_replace, text)
    if not removed:
        return text

    for url in removed:
        log_with_trace_id(
            state,
            f"⚠️ Removed unsourced URL {url} — no tool returned it and it is not "
            f"in the agent prompt ({len(allowed)} allowed URL(s) this turn)",
            "warning",
        )
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


_SUPPORT_CONTACT_PLACEHOLDER = "{{support_team_contact_details}}"

# Match the placeholder along with a leading connector ("at ", "on ", "via ",
# "through ", ":") so the fallback path can drop the whole clause instead of
# leaving a dangling "at " that reads as "contact the support team directly at
# our support team" — the bug flagged by QA on 2026-08-12 across 15 Concept
# Groove replies where vendor_contact_details was unset.
_SUPPORT_CONTACT_CLAUSE_RE = re.compile(
    r"\s*(?:\bat\b|\bon\b|\bvia\b|\bthrough\b|:)\s*"
    + re.escape(_SUPPORT_CONTACT_PLACEHOLDER)
)


def _strip_support_contact_clause(prompt: str) -> str:
    """Remove the placeholder AND any leading connector word.

    Turns "You can contact the support team directly at {{...}} for tracking"
    into "You can contact the support team directly for tracking" — grammatical
    even when no vendor contact data is configured. Falls back to plain
    placeholder removal if the connector pattern doesn't match.
    """
    cleaned = _SUPPORT_CONTACT_CLAUSE_RE.sub("", prompt)
    if _SUPPORT_CONTACT_PLACEHOLDER in cleaned:
        cleaned = cleaned.replace(_SUPPORT_CONTACT_PLACEHOLDER, "")
    return cleaned


async def resolve_support_contact_placeholder(prompt: str, client_id: Optional[str] = None) -> str:
    """
    Replace ``{{support_team_contact_details}}`` in *prompt* with actual
    vendor contact details from ``client_configs`` (tiered cache).

    Returns the prompt unchanged when the placeholder is absent.
    When no vendor contact data is configured, the entire "at {{...}}" clause
    is silently dropped so the customer never sees the awkward "contact the
    support team directly at our support team" fallback (see QA report 2026-08-12).
    """
    if _SUPPORT_CONTACT_PLACEHOLDER not in prompt:
        return prompt

    try:
        from fashion_bot.config_manager import aget_config

        contact_data = await aget_config("vendor_contact_details", client_id=client_id)

        if contact_data:
            if isinstance(contact_data, str):
                contact_data = json.loads(contact_data)

            email = (contact_data.get("support email id") or "").strip()
            phones = (contact_data.get("support phone numbers") or "").strip()

            parts = [p for p in (email, phones) if p]
            if parts:
                return prompt.replace(_SUPPORT_CONTACT_PLACEHOLDER, " or ".join(parts))

        logger.debug(
            "vendor_contact_details not configured for client_id=%s; stripping placeholder clause",
            client_id,
        )
        return _strip_support_contact_clause(prompt)

    except Exception as e:
        logger.debug(f"Failed to resolve support_team_contact_details placeholder: {e}")
        return _strip_support_contact_clause(prompt)
