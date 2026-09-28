"""
Event configuration for ShipRocket webhook events.
This file contains all the event mappings and templates for easy modification.
"""

from typing import Dict, List, Optional, Set
from dataclasses import dataclass
import os
import time
import logging

from fashion_bot.database_manager import get_async_postgres_connection, awith_retry
from fashion_bot.env_loader import get_env
from fashion_bot.rollbar_config import report_error

logger = logging.getLogger(__name__)

# Simple cache for DB event keys
_EVENT_KEYS_CACHE = {}
_CACHE_TIMESTAMP = {}
_CACHE_TTL = 300  # 5 minutes


def _shiprocket_legacy_route_allowed() -> bool:
    """Whether empty client_id from the legacy ShipRocket webhook URL is
    tolerated. Defaults to False — after the dashboard URL switch to the
    per-client form, any empty client_id is a real config bug and must
    escalate. Set ``SHIPROCKET_ALLOW_LEGACY_WEBHOOK=true`` only during a
    cut-over window.
    """
    val = get_env("SHIPROCKET_ALLOW_LEGACY_WEBHOOK", "")
    return str(val).strip().lower() in ("1", "true", "yes")


@awith_retry
async def _aget_event_keys_from_db(client_id: str, channel: str) -> Set[str]:
    """Load valid event keys from gupshup_templates table using client_id (async)."""
    client_id_str = str(client_id) if client_id else None
    cache_key = f"{client_id_str}:{channel}"

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

            logger.info(f"Loaded {len(event_keys)} event keys from DB for client {client_id_str}/{channel}")
            return event_keys

@dataclass
class EventTemplate:
    """Template configuration for an event"""
    event_name: str
    template_id: str
    message: str
    whatsapp_template: str = ""
    should_notify: bool = True
    priority: int = 1

# Event mapping based on shipment_status
SHIPMENT_EVENTS = {
    "OUT FOR DELIVERY": EventTemplate(
        event_name="OUT_FOR_DELIVERY",
        template_id="out_for_delivery",
        message="Your order is out for delivery! 🚚",
        should_notify=True,
        priority=1
    ),
    "DELIVERED": EventTemplate(
        event_name="DELIVERED",
        template_id="delivered",
        message="Order delivered!",
        whatsapp_template=(
            "Hey {customer_firstname}! 🙌 Your order *{order_number}* has been delivered successfully 📦💃 "
            "You just joined the super exclusive Groovee fam – you're now a proud owner of the LIMITED EDITION drip (with only ~200 pieces available in India)! 🎉🔥 "
            "Let’s connect on Insta and tag us when you flex that new fit! 📸💥 https://www.instagram.com/go_groovee "
            "Stay bold. Stay you. Team Groovee. ✌️\nShop More"
        ),
        should_notify=True,
        priority=1
    ),
    "DELIVERY ATTEMPTED": EventTemplate(
        event_name="DELIVERY_ATTEMPTED",
        template_id="delivery_attempted",
        message="Delivery was attempted but you weren't available. We'll try again! 🔄",
        should_notify=True,
        priority=2
    ),
    "RETURNED": EventTemplate(
        event_name="RETURNED",
        template_id="order_returned",
        message="Your order has been returned to our facility. 📦",
        should_notify=True,
        priority=2
    ),
    "CANCELLED": EventTemplate(
        event_name="CANCELLED",
        template_id="order_cancelled",
        message="Your order has been cancelled. 💔",
        should_notify=True,
        priority=3
    ),
    "IN TRANSIT": EventTemplate(
        event_name="IN_TRANSIT",
        template_id="in_transit",
        message="Your order is in transit and on its way to you! 🚛",
        should_notify=False,  # Don't notify for every transit update
        priority=4
    ),
    "PICKED UP": EventTemplate(
        event_name="PICKED_UP",
        template_id="picked_up",
        message="Your order has been picked up and is on its way! 📦",
        should_notify=True,
        priority=2
    ),
    "MANIFEST GENERATED": EventTemplate(
        event_name="MANIFEST_GENERATED",
        template_id="manifest_generated",
        message="Your order has been processed and is ready for shipping! 📋",
        should_notify=False,
        priority=5
    ),
    "COD ORDER DONE": EventTemplate(
        event_name="COD_ORDER_DONE",
        template_id="cod_order_done",
        message="Your COD order has been completed successfully! 💰",
        should_notify=True,
        priority=1
    )
}

# Additional event mappings based on specific status codes or activities
STATUS_CODE_EVENTS = {
    "5": EventTemplate(
        event_name="MANIFEST_GENERATED",
        template_id="manifest_generated",
        message="Your order has been processed and is ready for shipping! 📋",
        should_notify=False,
        priority=5
    ),
    "42": EventTemplate(
        event_name="PICKED_UP",
        template_id="picked_up",
        message="Your order has been picked up and is on its way! 📦",
        should_notify=True,
        priority=2
    ),
    "6": EventTemplate(
        event_name="SHIPPED",
        template_id="shipped",
        message="Your order has been shipped! 🚢",
        should_notify=True,
        priority=2
    ),
    "18": EventTemplate(
        event_name="IN_TRANSIT",
        template_id="in_transit",
        message="Your order is in transit and on its way to you! 🚛",
        should_notify=False,
        priority=4
    )
}

# Event priority levels for deduplication
EVENT_PRIORITIES = {
    "OUT_FOR_DELIVERY": 1,
    "DELIVERED": 1,
    "COD_ORDER_DONE": 1,
    "DELIVERY_ATTEMPTED": 2,
    "PICKED_UP": 2,
    "SHIPPED": 2,
    "RETURNED": 2,
    "CANCELLED": 3,
    "IN_TRANSIT": 4,
    "MANIFEST_GENERATED": 5
}

def get_event_template(shipment_status: str, status_code: Optional[str] = None) -> Optional[EventTemplate]:
    """
    Get event template based on shipment status and optional status code.
    
    Args:
        shipment_status: The shipment status from the webhook
        status_code: Optional status code from scans
        
    Returns:
        EventTemplate if found, None otherwise
    """
    # First try to get from shipment status
    if shipment_status in SHIPMENT_EVENTS:
        return SHIPMENT_EVENTS[shipment_status]
    
    # If not found, try status code
    if status_code and status_code in STATUS_CODE_EVENTS:
        return STATUS_CODE_EVENTS[status_code]
    
    return None

def get_all_event_names() -> List[str]:
    """Get all available event names for reference"""
    return list(SHIPMENT_EVENTS.keys()) + list(STATUS_CODE_EVENTS.keys())

def is_notifiable_event(event_name: str) -> bool:
    """Check if an event should trigger a notification"""
    if event_name in SHIPMENT_EVENTS:
        return SHIPMENT_EVENTS[event_name].should_notify
    return False 

# Simple template map and resolver for Shiprocket events
# Map event keys to Gupshup template configuration
SHIPROCKET_TEMPLATE_MAP = {
    'DELIVERED': {
        'template_id': '768d64f4-ebda-459a-a87f-b193db3f5bd9',
        'image_url': 'https://fss.gupshup.io/0/public/0/0/gupshup/15557872987/5036f64e-cf69-430a-b936-ba6a7c4970d7/1756991712584_8efac046-71f3-4ec2-98cd-45b79f43150b.jpg',
        'param_order': ['customer_firstname', 'order_number']
    }
}

async def aresolve_shiprocket_event_key(shipment_status: str, current_status: str = None, client_id: str = None) -> str:
    """
    Resolve event key dynamically (async) - constructs key from input and checks if it exists in DB.
    Uses client_id for multi-client support - NO hardcoded defaults.
    """
    if not client_id:
        # The legacy webhook URL has no {encoded_client_id} in the path
        # and falls through here. During the cut-over window we allow
        # this silently via SHIPROCKET_ALLOW_LEGACY_WEBHOOK=true. After
        # cut-over, an empty client_id is a real tenant-mapping bug —
        # log at ERROR and escalate so it's not silently dropped.
        if _shiprocket_legacy_route_allowed():
            logger.debug("Skipping shiprocket event-key resolution: client_id missing (legacy route allowed)")
        else:
            logger.error(
                "Shiprocket webhook hit with empty client_id and "
                "SHIPROCKET_ALLOW_LEGACY_WEBHOOK is off — webhook URL likely "
                "missing {encoded_client_id} segment; shipment notifications "
                "will be dropped (shipment_status=%s, current_status=%s)",
                shipment_status,
                current_status,
            )
            try:
                report_error(
                    "Shiprocket webhook missing client_id",
                    level="error",
                    shipment_status=shipment_status,
                    current_status=current_status,
                )
            except Exception:
                pass
        return ''

    valid_keys = await _aget_event_keys_from_db(client_id, 'shiprocket')
    
    # Construct potential event key from input (uppercase, stripped)
    s = (shipment_status or '').strip().upper()
    c = (current_status or '').strip().upper()
    
    # Try both shipment_status and current_status
    potential_keys = [s, c] if c else [s]
    
    # Return first key that exists in DB
    for key in potential_keys:
        if key and key in valid_keys:
            logger.info(f"Matched event key '{key}' from DB for client {client_id}/shiprocket")
            return key
    
    # No match found
    logger.debug(f"No matching event key in DB for client {client_id}: shipment_status='{s}', current_status='{c}'")
    return '' 