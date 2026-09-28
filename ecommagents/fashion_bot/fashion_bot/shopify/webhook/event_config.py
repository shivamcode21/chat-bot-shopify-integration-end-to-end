"""
Shopify event configuration: template map and event key resolution.
"""
from typing import Dict, Any, Set
import os
import time
import logging

from fashion_bot.config_manager import resolve_client_id
from fashion_bot.database_manager import get_async_postgres_connection, awith_retry

logger = logging.getLogger(__name__)

# Simple cache for DB event keys
_EVENT_KEYS_CACHE = {}
_CACHE_TIMESTAMP = {}
_CACHE_TTL = 300  # 5 minutes

@awith_retry
async def _aget_event_keys_from_db(client_id: str, channel: str) -> Set[str]:
    """Load valid event keys from gupshup_templates table (async)."""
    client_id_str = str(client_id) if client_id else None
    cache_key = f"{client_id_str}:{channel}"

    # Check cache first (fast path - no DB needed)
    if cache_key in _EVENT_KEYS_CACHE:
        if time.time() - _CACHE_TIMESTAMP.get(cache_key, 0) < _CACHE_TTL:
            logger.debug(f"Event keys cache hit for {cache_key}")
            return _EVENT_KEYS_CACHE[cache_key]

    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT DISTINCT event_key
                FROM gupshup_templates
                WHERE client_id = %s AND channel = %s
            """, (client_id_str, channel))

            rows = await cur.fetchall()
            event_keys = {row['event_key'] for row in rows if row.get('event_key')}

            _EVENT_KEYS_CACHE[cache_key] = event_keys
            _CACHE_TIMESTAMP[cache_key] = time.time()

            logger.info(f"Loaded {len(event_keys)} event keys from DB for {client_id_str}/{channel}")
            return event_keys

# Map event keys to Gupshup template configuration
SHOPIFY_TEMPLATE_MAP: Dict[str, Dict[str, Any]] = {
    'VOIDED': {
        'template_id': 'a93171f9-31ab-4e63-8c32-5de54baabe9b',
        'image_url': 'https://fss.gupshup.io/0/public/0/0/gupshup/15557872987/4c0eebb2-4ed4-4bba-833b-d61497da8ad0/1756991802730_ad_1.jpg',
        'param_order': ['first_name', 'order_number']
    },
    'FULFILLED': {
        'template_id': '702bb0f5-ada0-45e6-8e64-a25717cffb43',
        'image_url': 'https://fss.gupshup.io/0/public/0/0/gupshup/15557872987/6a4b1ff1-caca-4726-b405-4e82dc884a3a/1756991603610_groove logo 1.jpg',
        'param_order': ['first_name', 'order_number']
    },
    'PAYMENT_PENDING_UNFULFILLED': {
        'template_id': 'bcbc10cc-929c-4722-9581-ba1398eb169d',
        'image_url': 'https://fss.gupshup.io/0/public/0/0/gupshup/15557872987/d565c583-42ef-4c22-9e7b-1ee42fadae9f/1756991400309_st_29.jpg',
        'param_order': ['first_name', 'order_number', 'total_price']
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

async def aresolve_event_key(
    payment_status: str,
    fulfillment_status: str,
    client_id: str = None,
    shipment_status: str = None,
) -> str:
    """
    Resolve event key dynamically (async) - constructs potential keys from input and checks if they exist in DB.
    Purely DB-driven, no hardcoded event names!
    """
    if client_id is None:
        client_id = os.getenv('CLIENT_ID', resolve_client_id())
    client_id_str = str(client_id) if client_id else None

    valid_keys = await _aget_event_keys_from_db(client_id_str, 'shopify')
    
    # Normalize input
    ps = (payment_status or '').upper().strip()
    fs = (fulfillment_status or 'UNFULFILLED').upper().strip()
    ss = (shipment_status or '').upper().strip()
    
    # Construct potential event keys to check (in priority order)
    potential_keys = []
    
    # 1. Check delivered shipment key before generic fulfillment keys.
    # Shopify order webhooks can carry delivery state inside
    # fulfillments[].shipment_status while top-level fulfillment_status remains
    # "fulfilled". Only "delivered" should override; other shipment states
    # continue using fulfillment/payment based templates as before.
    if ss == "DELIVERED":
        potential_keys.append(ss)

    # 2. Check fulfillment-based keys (e.g., 'FULFILLED', 'VOIDED')
    if fs:
        potential_keys.append(fs)
    
    # 3. Check payment status keys (e.g., 'VOIDED', 'PAID')
    if ps:
        potential_keys.append(ps)
    
    # 4. Check combined payment + fulfillment keys
    if ps and fs:
        # Try: PAYMENT_{STATUS}_{FULFILLMENT} e.g., PAYMENT_PENDING_UNFULFILLED
        potential_keys.append(f"PAYMENT_{ps}_{fs}")
        # Try: {PAYMENT}_{FULFILLMENT} e.g., PAID_UNFULFILLED
        potential_keys.append(f"{ps}_{fs}")

    # Return first key that exists in DB
    for key in potential_keys:
        if key and key in valid_keys:
            logger.info(f"Matched event key '{key}' from DB for {client_id_str}/shopify")
            return key
    
    # No match found
    logger.debug(
        f"No matching event key in DB for payment='{ps}', fulfillment='{fs}', "
        f"shipment='{ss}'"
    )
    return '' 
