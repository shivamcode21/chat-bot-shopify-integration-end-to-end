"""
Event processor for Shopify webhook events.
"""
import logging
import asyncio
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
import time
from .event_config import SHOPIFY_TEMPLATE_MAP, aresolve_event_key
from .event_database import ShopifyEventDatabaseService
# Notification service removed; templates are sent directly via Gupshup sender
from fashion_bot.tool_helpers import ShopifyRateLimitError
from fashion_bot.gupshup_webhook import send_message
from .gupshup_template_sender import asend_shopify_gupshup_template_generic
from .templates_db import aget_client_template
import os
from dotenv import load_dotenv
import uuid

# Load environment variables
load_dotenv()

try:
    import redis
except Exception:
    redis = None
import certifi

REDIS_URL = os.getenv("REDIS_URL") or os.getenv("REDIS_CONNECTION_STRING") or "redis://localhost:6379/0"

# ============================================================================
# WEBHOOK PHONE NUMBER FILTER - DB-driven per-client whitelisting
# ============================================================================
# Numbers are stored in `webhook_whitelisted_numbers` table (per client + channel).
# If is_enabled = False OR no row exists for a client → whitelisting is bypassed
# (all webhooks are processed).
# ============================================================================

from fashion_bot.database_manager import get_async_postgres_connection, awith_retry

import re
from fashion_bot.core.factory import ServiceFactory
from fashion_bot.utils.http_client import get_shared_async_http_client

# Simple in-memory cache: { (client_id, channel): (numbers_set, is_enabled, expiry_ts) }
_whitelist_cache: Dict[Tuple[str, str], Tuple[set, bool, float]] = {}
_WHITELIST_CACHE_TTL = 300  # 5 minutes


def _normalize_phone(phone: str) -> str:
    """
    Normalize a phone number to its last 10 digits for comparison.
    
    This handles the mismatch between numbers stored with country code (e.g. 919611703832)
    and numbers from Shopify without country code (e.g. 9611703832).
    
    Examples:
        '919611703832'  → '9611703832'
        '9611703832'    → '9611703832'
        '+91-9611703832' → '9611703832'
        '09611703832'   → '9611703832'
    """
    digits = re.sub(r'\D', '', phone)  # strip everything non-digit
    if len(digits) >= 10:
        return digits[-10:]  # last 10 digits = local number
    return digits  # fallback for short numbers (shouldn't happen)


def _extract_shipment_status(webhook_data: Any) -> str:
    """Return the latest Shopify fulfillment shipment_status, or empty string."""
    if not isinstance(webhook_data, dict):
        return ""

    direct_status = webhook_data.get("shipment_status")
    if direct_status:
        return str(direct_status).strip().lower()

    fulfillments = webhook_data.get("fulfillments")
    if not isinstance(fulfillments, list):
        return ""

    for fulfillment in reversed(fulfillments):
        if not isinstance(fulfillment, dict):
            continue
        shipment_status = fulfillment.get("shipment_status")
        if shipment_status:
            return str(shipment_status).strip().lower()
    return ""


async def _aget_whitelisted_numbers(client_id: str, channel: str = "shopify") -> Tuple[set, bool]:
    """Fetch whitelisted numbers for a client+channel from DB (with in-memory cache) (async)."""
    cache_key = (client_id or "", channel)
    cached = _whitelist_cache.get(cache_key)
    if cached:
        numbers_set, is_enabled, expiry = cached
        if time.time() < expiry:
            return numbers_set, is_enabled

    try:
        numbers_set, is_enabled = await _afetch_whitelisted_numbers_from_db(client_id, channel)
        _whitelist_cache[cache_key] = (numbers_set, is_enabled, time.time() + _WHITELIST_CACHE_TTL)
        return numbers_set, is_enabled
    except Exception as e:
        logger.warning(f"[WHITELIST] Failed to fetch whitelisted numbers for client {client_id}, channel {channel}: {e}")
        return set(), False


@awith_retry
async def _afetch_whitelisted_numbers_from_db(client_id: str, channel: str) -> Tuple[set, bool]:
    """Fetch whitelisted numbers from client_whitelisted_numbers table (async)."""
    if not client_id:
        return set(), False

    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT numbers, is_enabled
                FROM client_whitelisted_numbers
                WHERE client_id = %s AND channel = %s
                LIMIT 1
            """, (client_id, channel))
            row = await cur.fetchone()

            if not row:
                return set(), False

            is_enabled = bool(row.get("is_enabled", False))
            raw_numbers = row.get("numbers", "")
            if not raw_numbers:
                return set(), is_enabled

            numbers_set = {
                _normalize_phone(num)
                for num in raw_numbers.split(",") if num.strip()
            }
            logger.info(f"[WHITELIST] Loaded {len(numbers_set)} numbers for client={client_id}, channel={channel}, enabled={is_enabled}")
            return numbers_set, is_enabled


async def _ashould_process_shopify_webhook(phone: str, client_id: str = None) -> bool:
    """Async filter function: should this Shopify webhook be processed?"""
    numbers_set, is_enabled = await _aget_whitelisted_numbers(client_id, "shopify")

    if not is_enabled:
        return True

    if not numbers_set:
        return True

    return await _ais_test_phone_number(phone, client_id)


async def _ais_test_phone_number(phone: str, client_id: str = None) -> bool:
    """Check if a phone number is in the whitelisted numbers list (async)."""
    if not phone:
        return False

    numbers_set, is_enabled = await _aget_whitelisted_numbers(client_id, "shopify")

    if not is_enabled or not numbers_set:
        return False

    normalized = _normalize_phone(phone)
    return normalized in numbers_set
# ============================================================================


async def _afetch_whitelisted_numbers_from_db(client_id: str, channel: str) -> Tuple[set, bool]:
    """Async fetch of whitelisted numbers from client_whitelisted_numbers."""
    if not client_id:
        return set(), False

    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT numbers, is_enabled
                FROM client_whitelisted_numbers
                WHERE client_id = %s AND channel = %s
                LIMIT 1
                """,
                (client_id, channel),
            )
            row = await cur.fetchone()
            if not row:
                return set(), False

            is_enabled = bool(row.get("is_enabled", False))
            raw_numbers = row.get("numbers", "")
            if not raw_numbers:
                return set(), is_enabled

            numbers_set = {
                _normalize_phone(num)
                for num in raw_numbers.split(",") if num.strip()
            }
            logger.info(f"[WHITELIST] Loaded {len(numbers_set)} numbers for client={client_id}, channel={channel}, enabled={is_enabled}")
            return numbers_set, is_enabled


async def _aget_whitelisted_numbers(client_id: str, channel: str = "shopify") -> Tuple[set, bool]:
    """Async cache-backed whitelist lookup."""
    cache_key = (client_id or "", channel)
    cached = _whitelist_cache.get(cache_key)
    if cached:
        numbers_set, is_enabled, expiry = cached
        if time.time() < expiry:
            return numbers_set, is_enabled

    try:
        numbers_set, is_enabled = await _afetch_whitelisted_numbers_from_db(client_id, channel)
        _whitelist_cache[cache_key] = (numbers_set, is_enabled, time.time() + _WHITELIST_CACHE_TTL)
        return numbers_set, is_enabled
    except Exception as e:
        logger.warning(f"[WHITELIST] Failed to fetch whitelisted numbers for client {client_id}, channel {channel}: {e}")
        return set(), False


async def _ashould_process_shopify_webhook(phone: str, client_id: str = None) -> bool:
    """Async whitelist check for webhook processing."""
    numbers_set, is_enabled = await _aget_whitelisted_numbers(client_id, "shopify")

    if not is_enabled:
        return True
    if not numbers_set:
        return True
    if not phone:
        return False

    normalized = _normalize_phone(phone)
    return normalized in numbers_set

# ── Async Redis client for event dedup (singleton) ──────────────────────────
_async_redis_client_for_events = None

async def _aget_redis_client_for_events():
    """Get async Redis client for event dedup. Singleton, lazy-init."""
    global _async_redis_client_for_events
    if _async_redis_client_for_events is not None:
        return _async_redis_client_for_events
    try:
        import redis.asyncio as aioredis
    except Exception:
        return None
    try:
        client_kwargs = {"decode_responses": True}
        if str(REDIS_URL).lower().startswith("rediss://"):
            client_kwargs["ssl_ca_certs"] = certifi.where()
            insecure = (os.getenv("REDIS_SSL_INSECURE", "").lower() in ("1", "true", "yes"))
            if insecure:
                client_kwargs["ssl_cert_reqs"] = None
        client = aioredis.Redis.from_url(REDIS_URL, **client_kwargs)
        await client.ping()
        _async_redis_client_for_events = client
        return client
    except Exception:
        return None

from fashion_bot.config_manager import aget_shopify_config

logger = logging.getLogger(__name__)

# ============================================================================
# IN-MEMORY DEDUPLICATION CACHE - First line of defense
# ============================================================================
# Cache of recently processed events: {(order_id, event_key): timestamp}
_PROCESSED_EVENTS_CACHE = {}
_CACHE_TTL_SECONDS = 600  # Keep in cache for 10 minutes
_CACHE_MAX_SIZE = 10000  # Prevent memory bloat

# Redis cache TTL (second layer)
_REDIS_CACHE_TTL_SECONDS = 86400  # Keep in Redis for 1 day

def _cleanup_event_cache():
    """Remove expired entries from cache"""
    current_time = time.time()
    expired_keys = [
        k for k, v in _PROCESSED_EVENTS_CACHE.items() 
        if current_time - v > _CACHE_TTL_SECONDS
    ]
    for k in expired_keys:
        _PROCESSED_EVENTS_CACHE.pop(k, None)
    
    # If still too large, remove oldest entries
    if len(_PROCESSED_EVENTS_CACHE) > _CACHE_MAX_SIZE:
        sorted_items = sorted(_PROCESSED_EVENTS_CACHE.items(), key=lambda x: x[1])
        to_remove = len(_PROCESSED_EVENTS_CACHE) - _CACHE_MAX_SIZE
        for k, _ in sorted_items[:to_remove]:
            _PROCESSED_EVENTS_CACHE.pop(k, None)

def _is_recently_processed(order_id: str, event_key: str, client_id: str) -> bool:
    """Check if event was recently processed (in-memory cache) with client_id isolation"""
    cache_key = (client_id, order_id, event_key)
    current_time = time.time()
    
    # Periodic cleanup
    if len(_PROCESSED_EVENTS_CACHE) % 100 == 0:
        _cleanup_event_cache()
    
    if cache_key in _PROCESSED_EVENTS_CACHE:
        cached_time = _PROCESSED_EVENTS_CACHE[cache_key]
        if current_time - cached_time < _CACHE_TTL_SECONDS:
            return True
    
    return False

def _mark_as_processed(order_id: str, event_key: str, client_id: str):
    """Mark event as processed in in-memory cache with client_id isolation"""
    cache_key = (client_id, order_id, event_key)
    _PROCESSED_EVENTS_CACHE[cache_key] = time.time()
# ============================================================================

async def get_shopify_order_with_retry(order_id: str, max_retries: int = 3, initial_delay: float = 4.0, client_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Fetch Shopify order with retry logic and exponential backoff.
    
    This handles the race condition where Shopify fires webhooks before 
    their internal API has the order data ready.
    
    Timing with initial_delay=4.0:
    - Initial wait: 4s
    - Attempt 1
    - If fail, wait: 4s (2^0)
    - Attempt 2
    - If fail, wait: 8s (2^1)
    - Attempt 3
    Total: ~16-20 seconds worst case
    
    Args:
        order_id: The order ID to fetch
        max_retries: Maximum number of retry attempts (default: 3)
        initial_delay: Initial delay in seconds before first fetch (default: 4.0)
        client_id: Client ID for multi-tenant Shopify config lookup (optional)
        
    Returns:
        Order dict if found, None otherwise
    """
    logger = logging.getLogger(__name__)
    
    # Log client_id upfront for debugging multi-tenant issues
    print(f"\n[ORDER_FETCH] ⏳ Waiting {initial_delay}s before fetching order {order_id} (avoiding race condition) [client_id={client_id}]")
    logger.info(f"[ORDER_FETCH] Initial delay of {initial_delay}s for order {order_id}, client_id={client_id}")
    if not client_id:
        logger.warning(f"[ORDER_FETCH] ⚠️ client_id is None/empty for order {order_id} — will fall back to default Shopify config!")
    await asyncio.sleep(initial_delay)
    
    state = {"client_id": client_id}
    order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
    
    for attempt in range(1, max_retries + 1):
        try:
            print(f"[ORDER_FETCH] Attempt {attempt}/{max_retries}: Fetching order {order_id} from Shopify API [client_id={client_id}]")
            logger.info(f"[ORDER_FETCH] Attempt {attempt}/{max_retries} for order {order_id}, client_id={client_id}")
            
            order = await order_service.aget_order_details(order_id, state=state)
            
            if order:
                print(f"[ORDER_FETCH] ✅ Order {order_id} found successfully on attempt {attempt}")
                logger.info(f"[ORDER_FETCH] Order {order_id} found on attempt {attempt}")
                return order
            else:
                print(f"[ORDER_FETCH] ⚠️  Order {order_id} not found on attempt {attempt}")
                logger.warning(f"[ORDER_FETCH] Order {order_id} not found on attempt {attempt}")
                
                # Don't retry if this was the last attempt
                if attempt < max_retries:
                    # Exponential backoff: 4s, 8s (keeps total time under Shopify's timeout)
                    backoff_delay = initial_delay * (2 ** (attempt - 1))
                    print(f"[ORDER_FETCH] ⏳ Retrying in {backoff_delay}s...")
                    logger.info(f"[ORDER_FETCH] Retrying in {backoff_delay}s")
                    await asyncio.sleep(backoff_delay)
        
        except ShopifyRateLimitError as rle:
            # Shopify 429 rate limit — back off using Retry-After header value
            retry_after = max(rle.retry_after, 2.0)  # At least 2s
            print(f"[ORDER_FETCH] 🚦 Rate limited by Shopify on attempt {attempt}/{max_retries} for order {order_id}. Backing off {retry_after}s...")
            logger.warning(f"[ORDER_FETCH] 429 rate limited on attempt {attempt} for order {order_id}, backing off {retry_after}s")
            
            if attempt < max_retries:
                await asyncio.sleep(retry_after)
            # If last attempt and rate limited, still return None below
                    
        except Exception as e:
            print(f"[ORDER_FETCH] ❌ Error fetching order {order_id} on attempt {attempt}: {e}")
            logger.error(f"[ORDER_FETCH] Error on attempt {attempt} for order {order_id}: {e}", exc_info=True)
            
            if attempt < max_retries:
                backoff_delay = initial_delay * (2 ** (attempt - 1))
                print(f"[ORDER_FETCH] ⏳ Retrying in {backoff_delay}s...")
                await asyncio.sleep(backoff_delay)
    
    # All retries exhausted
    print(f"[ORDER_FETCH] ❌ Failed to fetch order {order_id} after {max_retries} attempts")
    logger.error(f"[ORDER_FETCH] Failed to fetch order {order_id} after {max_retries} attempts")
    return None

async def _fetch_shopify_image_url(product_id: Optional[int], variant_id: Optional[int], client_id: Optional[str] = None) -> str:
    """Fetch the best image URL for a given product/variant using Shopify Admin REST API.
    Strategy:
    - If variant_id present, GET /variants/{variant_id}.json to get image_id
    - Then GET /products/{product_id}.json and match images[].id == image_id to return images[].src
    - Fallbacks: product["image"]["src"] or first images[].src
    Returns empty string on failure.
    """
    logger = logging.getLogger(__name__)
    try:
        if not product_id and not variant_id:
            print(f"[IMAGE_FETCH] ❌ No product_id or variant_id provided")
            logger.warning(f"[IMAGE_FETCH] No product_id or variant_id provided")
            return ""
        
        cfg = await aget_shopify_config(client_id=client_id)
        access_token = (cfg or {}).get('access_token')
        shop_url = (cfg or {}).get('shop_url')
        if not access_token or not shop_url:
            print(f"[IMAGE_FETCH] ❌ Missing Shopify credentials (access_token or shop_url)")
            logger.error(f"[IMAGE_FETCH] Missing Shopify credentials (access_token or shop_url)")
            return ""
        client = await get_shared_async_http_client()
        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        }
        image_id = None
        api_version = "2024-07"
        if variant_id:
            try:
                v_url = f"https://{shop_url}/admin/api/{api_version}/variants/{variant_id}.json"
                logger.debug(f"[IMAGE_FETCH] Fetching variant: {v_url}")
                v_res = await client.get(v_url, headers=headers, timeout=12)
                if v_res.status_code == 200:
                    image_id = (v_res.json().get("variant") or {}).get("image_id")
                    logger.debug(f"[IMAGE_FETCH] Variant {variant_id} has image_id: {image_id}")
                else:
                    logger.warning(f"[IMAGE_FETCH] Variant API returned status {v_res.status_code}")
            except Exception as e:
                logger.warning(f"[IMAGE_FETCH] Error fetching variant {variant_id}: {e}")
        prod_data = None
        if product_id:
            try:
                p_url = f"https://{shop_url}/admin/api/{api_version}/products/{product_id}.json"
                logger.debug(f"[IMAGE_FETCH] Fetching product: {p_url}")
                p_res = await client.get(p_url, headers=headers, timeout=12)
                if p_res.status_code == 200:
                    prod_data = p_res.json().get("product") or {}
                    total_images = len(prod_data.get("images") or [])
                    logger.debug(f"[IMAGE_FETCH] Product {product_id} has {total_images} images")
                else:
                    logger.warning(f"[IMAGE_FETCH] Product API returned status {p_res.status_code}")
            except Exception as e:
                logger.warning(f"[IMAGE_FETCH] Error fetching product {product_id}: {e}")
        if not prod_data:
            logger.warning(f"[IMAGE_FETCH] No product data available for product_id={product_id}")
            return ""
        # Prefer matching image_id
        if image_id:
            for img in (prod_data.get("images") or []):
                try:
                    if str(img.get("id")) == str(image_id):
                        image_url = img.get("src") or ""
                        print(f"[IMAGE_FETCH] ✅ Found variant-specific image (variant_id={variant_id}, image_id={image_id})")
                        print(f"  → {image_url}")
                        logger.info(f"[IMAGE_FETCH] Found variant-specific image: {image_url}")
                        return image_url
                except Exception:
                    continue
        # Fallback featured image
        try:
            featured = prod_data.get("image") or {}
            if featured.get("src"):
                image_url = featured.get("src")
                print(f"[IMAGE_FETCH] ⚠️  Using featured product image (variant had no specific image)")
                print(f"  → {image_url}")
                logger.info(f"[IMAGE_FETCH] Using featured product image: {image_url}")
                return image_url
        except Exception as exc:
            logger.warning(f"[IMAGE_FETCH] ⚠️ Error fetching featured image: {exc}")
        # Fallback first image
        imgs = prod_data.get("images") or []
        if imgs:
            image_url = imgs[0].get("src") or ""
            print(f"[IMAGE_FETCH] ⚠️  Using first product image (no featured image)")
            print(f"  → {image_url}")
            logger.info(f"[IMAGE_FETCH] Using first product image: {image_url}")
            return image_url
        print(f"[IMAGE_FETCH] ❌ Product {product_id} has no images available")
        logger.warning(f"[IMAGE_FETCH] Product {product_id} has no images available")
        return ""
    except Exception as e:
        print(f"[IMAGE_FETCH] ❌ Unexpected error: {e}")
        logger.error(f"[IMAGE_FETCH] Unexpected error: {e}", exc_info=True)
        return ""


def fill_template(template: str, order) -> str:
    def safe_get(d, key, default=""):
        return d[key] if isinstance(d, dict) and key in d else default
    return template.format(
        customer_firstname=(
            safe_get(order, 'customer_name')
            or safe_get(safe_get(order, 'shipping_address', {}), 'first_name')
            or safe_get(safe_get(order, 'customer', {}), 'first_name')
            or "Customer"
        ),
        order_number=(
            safe_get(order, 'order_id')
            or safe_get(order, 'order_number')
            or safe_get(order, 'name')
            or "Order"
        ),
        total_price=(
            safe_get(order, 'total')
            or safe_get(order, 'total_price')
            or "N/A"
        ),
    )


def extract_shopify_name_phone(order: Dict[str, Any]) -> Dict[str, str]:
    def safe_get(d, key, default=""):
        return d[key] if isinstance(d, dict) and key in d else default
    shipping = safe_get(order, 'shipping_address', {})
    customer = safe_get(order, 'customer', {})
    default_addr = safe_get(customer, 'default_address', {})
    first_name = (
        safe_get(shipping, 'first_name')
        or safe_get(customer, 'first_name')
        or safe_get(default_addr, 'first_name')
        or "Customer"
    )
    phone = (
        safe_get(shipping, 'phone')
        or safe_get(default_addr, 'phone')
        or safe_get(customer, 'phone')
        or safe_get(order, 'phone')
        or ""
    )
    return {
        'first_name': first_name,
        'phone': phone
    }

# Mapping of event to template config
SHOPIFY_TEMPLATE_MAP = {
    'VOIDED': {
        'template_id': 'a93171f9-31ab-4e63-8c32-5de54baabe9b',
        'image_url': 'https://fss.gupshup.io/0/public/0/0/gupshup/15557872987/4c0eebb2-4ed4-4bba-833b-d61497da8ad0/1756991802730_ad_1.jpg',
        'param_order': ['first_name', 'order_number']
    },
    'FULFILLED': {
        'template_id': '702bb0f5-ada0-45e6-8e64-a25717cffb43',
        'image_url': 'https://fss.gupshup.io/0/public/0/0/gupshup/15557872987/6a4b1ff1-caca-4726-b405-4e82dc884a3a/1756991603610_groove%2520logo%25201.jpg',
        'param_order': ['first_name', 'order_number']
    },
    'PAYMENT_PENDING_UNFULFILLED': {
        'template_id': 'bcbc10cc-929c-4722-9581-ba1398eb169d',
        'image_url': 'https://fss.gupshup.io/0/public/0/0/gupshup/15557872987/d565c583-42ef-4c22-9e7b-1ee42fadae9f/1756991400309_st_29.jpg',
        'param_order': ['first_name', 'order_number', 'total_price']
    },
    'PAYMENT_PENDING': {
        'template_id': 'bcbc10cc-929c-4722-9581-ba1398eb169d',
        'image_url': 'https://fss.gupshup.io/0/public/0/0/gupshup/15557872987/d565c583-42ef-4c22-9e7b-1ee42fadae9f/1756991400309_st_29.jpg',
        'param_order': ['customer_firstname', 'order_number', 'total_price']
    },
    'PAID_UNFULFILLED': {
        'template_id': '7b28b362-9801-4507-abae-b95a9ed65301',
        'image_url': 'https://fss.gupshup.io/0/public/0/0/gupshup/15557872987/ea4b4bdb-5c43-430a-86f7-9b3dc4ae1cbf/1756991289635_resized_ad_1.jpg',
        'param_order': ['first_name', 'order_number', 'total_price']
    },
    'PARTIALLY_PAID_UNFULFILLED': {
        'template_id': '7b28b362-9801-4507-abae-b95a9ed65301',
        'image_url': 'https://fss.gupshup.io/0/public/0/0/gupshup/15557872987/ea4b4bdb-5c43-430a-86f7-9b3dc4ae1cbf/1756991289635_resized_ad_1.jpg',
        'param_order': ['first_name', 'order_number', 'total_price']
    }
}

class ShopifyEventProcessor:
    def __init__(self):
        # Lazy DB service init prevents worker boot from failing on transient DB issues.
        self._db_service: Optional[ShopifyEventDatabaseService] = None
        # Templates are sent directly; no notification service needed

    def _get_db_service(self) -> ShopifyEventDatabaseService:
        if self._db_service is None:
            self._db_service = ShopifyEventDatabaseService()
        return self._db_service
    
    def _create_duplicate_response(self, order_id: str, event_key: str, layer: str, message: str) -> Dict[str, Any]:
        """Create a standardized response for blocked duplicates"""
        return {
            'success': True,
            'message': message,
            'order_id': order_id,
            'event_name': event_key,
            'notification_sent': False,
            'dedup_layer': layer
        }
    
    def _check_memory_cache(self, order_id: str, event_key: str, client_id: str) -> Optional[Dict[str, Any]]:
        """
        LAYER 1: Check in-memory cache for recent processing with client_id.
        Fastest check (~1ms), catches duplicates in same instance.
        
        Returns: Duplicate response dict if found, None otherwise
        """
        if _is_recently_processed(order_id, event_key, client_id):
            logger.info(f"[DEDUP-LAYER-1] [CLIENT: {client_id}] 🚫 DUPLICATE BLOCKED (in-memory cache) - Order: {order_id}, Event: {event_key}")
            return self._create_duplicate_response(
                order_id, event_key, 'memory_cache',
                'Duplicate event blocked by in-memory cache'
            )
        
        logger.info(f"[DEDUP-LAYER-1] [CLIENT: {client_id}] ✅ Not in cache, proceeding - Order: {order_id}, Event: {event_key}")
        return None
    
    async def _check_redis_cache(self, order_id: str, event_key: str, client_id: str) -> Optional[Dict[str, Any]]:
        """
        LAYER 2: Check Redis cache for recent processing with client_id.
        Slower than memory (~5-20ms) but survives restarts and works across instances.
        TTL: 1 day

        Returns: Duplicate response dict if found, None otherwise
        """
        redis_client = await _aget_redis_client_for_events()
        if not redis_client:
            logger.warning(f"[DEDUP-LAYER-2] Redis not available, skipping Redis cache check")
            return None

        cache_key = f"shopify_event:{client_id}:{order_id}:{event_key}"

        try:
            exists = await redis_client.exists(cache_key)
            if exists:
                logger.info(f"[DEDUP-LAYER-2] [CLIENT: {client_id}] 🚫 DUPLICATE BLOCKED (Redis cache) - Order: {order_id}, Event: {event_key}")
                return self._create_duplicate_response(
                    order_id, event_key, 'redis_cache',
                    'Duplicate event blocked by Redis cache'
                )

            logger.info(f"[DEDUP-LAYER-2] [CLIENT: {client_id}] ✅ Not in Redis cache, proceeding - Order: {order_id}, Event: {event_key}")
            return None

        except Exception as e:
            logger.warning(f"[DEDUP-LAYER-2] [CLIENT: {client_id}] Redis cache check failed: {e}, proceeding to DB check")
            return None

    async def _mark_in_redis(self, order_id: str, event_key: str, client_id: str):
        """Mark event as processed in Redis cache with client_id"""
        redis_client = await _aget_redis_client_for_events()
        if not redis_client:
            return

        cache_key = f"shopify_event:{client_id}:{order_id}:{event_key}"

        try:
            await redis_client.setex(cache_key, _REDIS_CACHE_TTL_SECONDS, "1")
            logger.info(f"[DEDUP-LAYER-2] [CLIENT: {client_id}] ✅ Marked in Redis cache: {order_id}, {event_key}")
        except Exception as e:
            logger.warning(f"[DEDUP-LAYER-2] [CLIENT: {client_id}] Failed to mark in Redis: {e}")
    
    async def _claim_database_layer(self, order_id: str, event_key: str, webhook_data: Dict[str, Any], client_id: str) -> Optional[Dict[str, Any]]:
        """
        LAYER 3: Atomically claim the event in the database.

        This is a single INSERT ... ON CONFLICT DO NOTHING RETURNING id rather
        than a SELECT-then-mark-later check: the row insert itself is the gate,
        performed before any notification is sent. Whichever concurrent caller's
        INSERT actually creates the row is the only one that proceeds to send;
        every other caller — including a near-simultaneous duplicate webhook
        delivery — is blocked here. Previously the DB was only marked as
        processed *after* the notification had already been sent, leaving a
        window where two deliveries could both pass the check and both send.

        Returns: Duplicate response dict if another caller already claimed
        this event, None if this caller won the claim and should proceed.
        """
        logger.info(f"[DEDUP-LAYER-3] [CLIENT: {client_id}] Claiming database row for order: {order_id}, event: {event_key}")

        claim_start = time.time()
        claimed = await self._get_db_service().atry_claim_event(
            order_id, event_key, webhook_data, client_id
        )
        claim_time = (time.time() - claim_start) * 1000

        logger.info(f"[DEDUP-LAYER-3] [CLIENT: {client_id}] DB claim completed in {claim_time:.2f}ms, claimed: {claimed}")

        if not claimed:
            logger.info(f"[DEDUP-LAYER-3] [CLIENT: {client_id}] 🚫 DUPLICATE BLOCKED (database) - Order: {order_id}, Event: {event_key}")
            # Mark in memory cache even for DB-detected duplicates
            _mark_as_processed(order_id, event_key, client_id)
            return self._create_duplicate_response(
                order_id, event_key, 'database',
                'Duplicate event - no action taken (DB check)'
            )

        logger.info(f"[DEDUP-LAYER-3] [CLIENT: {client_id}] ✅ Claimed row, proceeding with processing")
        return None
    
    async def _process_and_mark_event(self, order_id: str, event_key: str, webhook_data: Dict[str, Any], client_id: str) -> Dict[str, Any]:
        """
        Process the event and record its snapshot with client_id.
        Called only after this caller has won the atomic database claim in
        _claim_database_layer().
        """
        # Mark as processing in memory cache BEFORE actual processing
        _mark_as_processed(order_id, event_key, client_id)
        logger.info(f"[DEDUP-LAYER-1] [CLIENT: {client_id}] ✅ Marked in memory cache: {order_id}, {event_key}")

        # Mark in Redis cache
        await self._mark_in_redis(order_id, event_key, client_id)

        # Process the event
        logger.info(f"[PROCESSING] [CLIENT: {client_id}] Starting event processing for order: {order_id}, event: {event_key}")
        try:
            processing_result = await self._process_event(event_key, webhook_data, client_id)
        except Exception:
            # Release the DB claim so a genuine webhook redelivery (same
            # event_hash) can retry instead of being silently dropped forever.
            await self._get_db_service().arelease_event_claim(order_id, event_key, webhook_data, client_id)
            raise

        # Update the audit log (notification_sent tracking); the dedup claim
        # itself was already taken in _claim_database_layer, before sending.
        logger.info(f"[PROCESSING] [CLIENT: {client_id}] Updating audit log in database: {order_id}, {event_key}")
        await self._get_db_service().amark_event_processed(
            order_id,
            event_key,
            webhook_data,
            client_id,
        )

        logger.info(f"[PROCESSING] [CLIENT: {client_id}] ✅ Event processing completed - Order: {order_id}, Notification sent: {processing_result.get('notification_sent', False)}")
        
        return {
            'success': True,
            'message': 'Event processed successfully',
            'order_id': order_id,
            'event_name': event_key,
            'notification_sent': processing_result.get('notification_sent', False),
            'details': processing_result,
            'dedup_layer': 'none_new_event',
            'client_id': client_id
        }
    
    async def process_webhook_event(self, webhook_data: Dict[str, Any], client_id: str = None) -> Dict[str, Any]:
        """
        Main webhook processing method with 3-layer deduplication and multi-client support.
        
        Flow:
        1. Validate and extract webhook data
        2. Check memory cache (fastest, 10 min TTL)
        3. Check Redis cache (cross-instance, 1 day TTL)
        4. Atomically claim the event in the database (persistent, race-safe gate)
        5. Process event and send notification
        6. Mark as processed in all layers
        
        Args:
            webhook_data: The webhook data
            client_id: The client ID if available
        """
        try:
            # Extract and validate webhook data
            order_id = webhook_data.get('name')
            payment_status = webhook_data.get('financial_status', '').lower()
            fulfillment_status = webhook_data.get('fulfillment_status', '').lower() if webhook_data.get('fulfillment_status') else 'unfulfilled'
            shipment_status = _extract_shipment_status(webhook_data)
            
            # Normalize: If order is cancelled, override payment_status to 'voided'
            if webhook_data.get('cancelled_at') is not None:
                payment_status = 'voided'
                fulfillment_status = 'voided'  # Use VOIDED as the primary key for cancelled orders
                shipment_status = ''
            
            logger.info(
                f"[DEDUP-CHECK] [CLIENT: {client_id}] Processing webhook for "
                f"order: {order_id}, payment: {payment_status}, "
                f"fulfillment: {fulfillment_status}, shipment: {shipment_status or 'N/A'}"
            )
            
            if not order_id or not payment_status:
                logger.warning(f"Missing required fields in Shopify webhook data: {webhook_data}")
                return {'success': False, 'error': 'Missing required fields', 'order_id': order_id}
            
            # Resolve event type (purely DB-driven, no hardcoded logic)
            event_key = await aresolve_event_key(
                payment_status,
                fulfillment_status,
                client_id,
                shipment_status=shipment_status,
            )
            logger.info(f"[DEDUP-CHECK] Event key resolved: {event_key} for order {order_id}")

            # An order already marked delivered must never get the FULFILLED
            # "on its way" template, no matter what re-triggered this webhook
            # (e.g. a self-inflicted orders/updated from our own note writes).
            if event_key == 'FULFILLED' and shipment_status == 'delivered':
                logger.info(
                    f"[SUPPRESS] Order {order_id} shipment_status=delivered — "
                    f"skipping in-transit notification for FULFILLED event"
                )
                return {
                    'success': True,
                    'message': 'Order already delivered, in-transit notification suppressed',
                    'order_id': order_id,
                }

            # ============================================================================
            # DEMO OVERRIDE: For test numbers, use PAYMENT_PENDING instead of UNFULFILLED variants
            # ============================================================================
            phone_for_override = None
            if webhook_data.get('shipping_address'):
                phone_for_override = webhook_data.get('shipping_address', {}).get('phone')
            if not phone_for_override and webhook_data.get('customer'):
                phone_for_override = webhook_data.get('customer', {}).get('phone')
            
            if phone_for_override and await _ais_test_phone_number(phone_for_override, client_id):
                if event_key == 'PAYMENT_PENDING_UNFULFILLED':
                    logger.info(f"[DEMO-OVERRIDE] Overriding event_key from {event_key} to PAYMENT_PENDING for test number {phone_for_override}")
                    event_key = 'PAYMENT_PENDING'
            # ============================================================================
            
            if not event_key:
                logger.info(
                    f"No event template for payment_status={payment_status}, "
                    f"fulfillment_status={fulfillment_status}, "
                    f"shipment_status={shipment_status or 'N/A'}"
                )
                return {'success': True, 'message': 'No event template found', 'order_id': order_id}

            # LAYER 1: Check in-memory cache
            memory_result = self._check_memory_cache(order_id, event_key, client_id)
            if memory_result:
                memory_result['client_id'] = client_id
                return memory_result

            # LAYER 2: Check Redis cache
            redis_result = await self._check_redis_cache(order_id, event_key, client_id)
            if redis_result:
                redis_result['client_id'] = client_id
                return redis_result

            # LAYER 3: Atomically claim the event in the database
            db_result = await self._claim_database_layer(order_id, event_key, webhook_data, client_id)
            if db_result:
                db_result['client_id'] = client_id
                return db_result

            # This caller won the claim - process the event
            return await self._process_and_mark_event(order_id, event_key, webhook_data, client_id)
                
        except Exception as e:
            logger.error(f"Error processing Shopify webhook event: {e}", exc_info=True)
            return {'success': False, 'error': str(e), 'order_id': webhook_data.get('id', 'Unknown')}
    async def _process_event(self, event_key: str, webhook_data: Dict[str, Any], client_id: str) -> Dict[str, Any]:
        """Process event with client_id for template lookup"""
        try:
            order_id = webhook_data.get('name')
            result = {'event_name': event_key, 'notification_sent': False, 'processing_time': datetime.now().isoformat()}
            # Send WhatsApp template directly (no notification service)
            # Use retry logic to handle Shopify's eventual consistency race condition
            # Conservative timing: 4s + 4s + 8s = 16s total (allows more time for Shopify's eventual consistency)
            order = await get_shopify_order_with_retry(order_id, max_retries=3, initial_delay=4.0, client_id=client_id)
            
            if not order:
                print(f"[ORDER_FETCH] ❌ Order {order_id} not found after retries - skipping notification")
                logger.error(f"[ORDER_FETCH] Order {order_id} not found after retries - skipping notification")
                result['notification_sent'] = False
                result['error'] = 'Order not found in Shopify API after retries'
                return result
            
            phone = None
            if order:
                phone = (order.get('shipping_address', {}) or {}).get('phone')
                if not phone:
                    phone = (order.get('customer', {}) or {}).get('phone')
            
            # ============================================================================
            # STAGING TESTING FILTER: Check if webhook should be processed for this number
            # Reads from DB table `webhook_whitelisted_numbers` per client+channel.
            # If is_enabled=False or no row → all webhooks processed (no filter).
            # ============================================================================
            if phone and not await _ashould_process_shopify_webhook(phone, client_id):
                logger.info(f"[STAGING FILTER] Shopify webhook skipped for order {order_id}, phone not whitelisted for client {client_id}")
                result['notification_sent'] = False
                result['filter_reason'] = 'Phone number not in whitelisted numbers'
                return result
            # ============================================================================
            
            sent = False
            if order and phone:
                name_phone = extract_shopify_name_phone(order)
                first_name = name_phone['first_name']
                order_number = (
                    order.get('name')
                    or str(order.get('order_number') or "Order")
                )
                total_price = (
                    order.get('total')
                    or order.get('total_price')
                    or "N/A"
                )

                # --- Build derived fields (lazy, computed only if needed) --------
                # product_details: comma-joined titles with variant info
                # e.g. "Anti Yellow Magsafe Case - Iphone 14 Plus (CRYSTAL WHITE), Tempered Glass"
                def _build_product_details() -> str:
                    items = order.get('line_items') or webhook_data.get('line_items') or []
                    parts = []
                    for li in items:
                        title = li.get('title') or li.get('name') or 'Unknown'
                        variant = li.get('variant_title')
                        if variant:
                            parts.append(f"{title} ({variant})")
                        else:
                            parts.append(title)
                    return ", ".join(parts) if parts else "N/A"

                # delivery_address: one-line formatted shipping address
                def _build_delivery_address() -> str:
                    sa = order.get('shipping_address') or {}
                    parts = [
                        sa.get('address1', ''),
                        sa.get('address2', ''),
                        sa.get('city', ''),
                        sa.get('zip', ''),
                        sa.get('province', ''),
                    ]
                    return ", ".join(p.strip() for p in parts if p and p.strip()) or "N/A"

                # tracking_url: URL from the latest fulfillment
                # e.g. "https://shiprocket.co/tracking/34791166906872"
                def _build_tracking_url() -> str:
                    fulfillments = order.get('fulfillments') or []
                    if fulfillments:
                        latest = fulfillments[-1]
                        url = latest.get('tracking_url') or ''
                        if url:
                            return url
                        # Fallback: build from tracking_urls array
                        urls = latest.get('tracking_urls') or []
                        if urls:
                            return urls[0]
                    return "N/A"

                # tracking_number: AWB number from the latest fulfillment
                def _build_tracking_number() -> str:
                    fulfillments = order.get('fulfillments') or []
                    if fulfillments:
                        latest = fulfillments[-1]
                        numbers = latest.get('tracking_numbers') or []
                        if numbers:
                            return numbers[0]
                        return latest.get('tracking_number') or ''
                    return "N/A"

                # courier_name: tracking company from fulfillment
                def _build_courier_name() -> str:
                    fulfillments = order.get('fulfillments') or []
                    if fulfillments:
                        return fulfillments[-1].get('tracking_company') or 'N/A'
                    return "N/A"

                from fashion_bot.utils.template_param_resolver import (
                    build_template_params_from_context,
                )
                from fashion_bot.utils.whatsapp_api_version import (
                    WHATSAPP_API_VERSION_ENTERPRISE,
                    aget_whatsapp_api_version,
                )

                template_context = {
                    "customer_name": first_name,
                    "order_id": order_number,
                    "order_value": total_price,
                    "product_details": _build_product_details,
                    "delivery_address": _build_delivery_address,
                    "tracking_link": _build_tracking_url,
                    "tracking_number": _build_tracking_number,
                    "courier_name": _build_courier_name,
                }
                is_enterprise_template_flow = (
                    await aget_whatsapp_api_version(client_id)
                    == WHATSAPP_API_VERSION_ENTERPRISE
                )
                # -----------------------------------------------------------------

                # DB-first lookup (using client_id instead of tenant_id)
                cfg_db = await aget_client_template(client_id, 'shopify', event_key)
                if cfg_db:
                    param_order = cfg_db.get('param_order') or []
                    if is_enterprise_template_flow:
                        params = build_template_params_from_context(param_order, template_context)
                    else:
                        params = []
                        for key in param_order:
                            if key == 'first_name':
                                params.append(first_name)
                            elif key == 'name':
                                params.append(first_name)
                            elif key == 'order_number':
                                params.append(order_number)
                            elif key == 'total_price':
                                params.append(total_price)
                            elif key == 'customer_firstname':
                                params.append(first_name)
                            elif key == 'product_details':
                                params.append(_build_product_details())
                            elif key == 'delivery_address':
                                params.append(_build_delivery_address())
                            elif key == 'tracking_url':
                                params.append(_build_tracking_url())
                            elif key == 'tracking_number':
                                params.append(_build_tracking_number())
                            elif key == 'courier_name':
                                params.append(_build_courier_name())
                            else:
                                # If key is not a dynamic placeholder, treat it as a static string param
                                params.append(key)
                    # Check DB image_url: if NULL or empty, skip image entirely
                    db_image_url = (cfg_db.get('image_url') or '').strip()
                    if not db_image_url:
                        final_image_url = None
                        print(f"[IMAGE_FETCH] ℹ️  Order {order_id}: No image_url in DB, sending text-only template")
                        logger.info(f"[IMAGE_FETCH] Order {order_id}: No image_url in DB, sending text-only template")
                    else:
                        # Build dynamic image URL from webhook line_items
                        line_items = webhook_data.get('line_items') or []
                        first_item = line_items[0] if line_items else {}
                        product_id = first_item.get('product_id')
                        variant_id = first_item.get('variant_id')
                        
                        print(f"[IMAGE_FETCH] Order {order_id}: Fetching image for product_id={product_id}, variant_id={variant_id}")
                        logger.info(f"[IMAGE_FETCH] Order {order_id}: Fetching image for product_id={product_id}, variant_id={variant_id}")
                        
                        dynamic_image_url = await _fetch_shopify_image_url(product_id, variant_id, client_id)
                        final_image_url = dynamic_image_url or db_image_url
                        
                        if dynamic_image_url:
                            print(f"[IMAGE_FETCH] ✅ Order {order_id}: Using dynamic Shopify image: {dynamic_image_url}")
                            logger.info(f"[IMAGE_FETCH] Order {order_id}: Using dynamic Shopify image: {dynamic_image_url}")
                        else:
                            print(f"[IMAGE_FETCH] ⚠️  Order {order_id}: Dynamic fetch failed, using DB fallback: {db_image_url}")
                            logger.warning(f"[IMAGE_FETCH] Order {order_id}: Dynamic fetch failed, using DB fallback: {db_image_url}")
                    
                    resp = await asend_shopify_gupshup_template_generic(
                        phone,
                        template_id=cfg_db['template_id'],
                        params=params,
                        image_url=final_image_url,
                        client_id=client_id,
                        event_key=event_key,
                        template_name=cfg_db.get('template_name'),
                        order_id=order_id  # Pass order_id for demo buttons
                    )
                    sent = resp is not None
                    if sent:
                        print(f"[IMAGE_FETCH] 📤 Image sent to customer {phone}: {final_image_url}")
                        print(f"{'='*80}\n")
                        img_log = (final_image_url or "N/A")[:100]
                        logger.info(f"Gupshup template (DB) sent to {phone} for Shopify order {order_id} with image: {img_log}...")
                else:
                    # Hardcoded SHOPIFY_TEMPLATE_MAP is Groovee-specific.
                    # Only use it for the default (Groovee) client to avoid
                    # sending wrong-brand templates to other clients.
                    from fashion_bot.config_manager import resolve_client_id as _resolve_cid
                    resolved_cid = _resolve_cid()
                    if client_id and client_id != resolved_cid:
                        logger.warning(
                            f"[TEMPLATE_SKIP] No DB template for client {client_id}, "
                            f"event {event_key}, order {order_id}. "
                            f"Skipping hardcoded fallback (non-default client)."
                        )
                    else:
                        tmpl_cfg = SHOPIFY_TEMPLATE_MAP.get(event_key)
                        if tmpl_cfg:
                            if is_enterprise_template_flow:
                                params = build_template_params_from_context(
                                    tmpl_cfg['param_order'], template_context
                                )
                            else:
                                params = []
                                for key in tmpl_cfg['param_order']:
                                    if key == 'first_name':
                                        params.append(first_name)
                                    elif key == 'name':
                                        params.append(first_name)
                                    elif key == 'order_number':
                                        params.append(order_number)
                                    elif key == 'total_price':
                                        params.append(total_price)
                                    elif key == 'customer_firstname':
                                        params.append(first_name)
                                    elif key == 'product_details':
                                        params.append(_build_product_details())
                                    elif key == 'delivery_address':
                                        params.append(_build_delivery_address())
                                    elif key == 'tracking_url':
                                        params.append(_build_tracking_url())
                                    elif key == 'tracking_number':
                                        params.append(_build_tracking_number())
                                    elif key == 'courier_name':
                                        params.append(_build_courier_name())
                                    else:
                                        # If key is not a dynamic placeholder, treat it as a static string param
                                        params.append(key)
                            resp = await asend_shopify_gupshup_template_generic(
                                phone,
                                template_id=tmpl_cfg['template_id'],
                                params=params,
                                image_url=tmpl_cfg['image_url'],
                                client_id=client_id,
                                event_key=event_key,
                                template_name=tmpl_cfg.get('template_name'),
                                order_id=order_id  # Pass order_id for demo buttons
                            )
                            sent = resp is not None
                            if sent:
                                logger.info(f"Gupshup template (map) sent to {phone} for Shopify order {order_id}")
                        else:
                            # Fallback to text message template if no mapping
                            message = None
                            if event_key in SHOPIFY_TEMPLATE_MAP:
                                message = fill_template(event_key, order or {})
                            if message:
                                await send_message(phone, message)
                                sent = True
                                logger.info(f"WhatsApp text message sent to {phone} for order {order_id}")
            else:
                logger.warning(f"No phone number found for Shopify order {order_id}, WhatsApp not sent.")
            result['notification_sent'] = sent
            if sent:
                await self._get_db_service().amark_notification_sent(order_id, event_key, client_id)
            return result
        except Exception as e:
            logger.error(f"Error processing Shopify event {event_key}: {e}")
            return {'event_name': event_key, 'error': str(e), 'notification_sent': False}
    async def _extract_user_id(self, webhook_data: Dict[str, Any]) -> Optional[str]:
        # Implement user identification logic if needed
        return None 
