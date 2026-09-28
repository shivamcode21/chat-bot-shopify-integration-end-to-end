"""
Event processor for ShipRocket webhook events.
Orchestrates event handling, deduplication, and notifications.
"""

import logging
import asyncio
from typing import Dict, Any, Optional, List
from datetime import datetime
import time
from fashion_bot.shiprocket.webhook.event_config import get_event_template, EventTemplate, aresolve_shiprocket_event_key
from fashion_bot.shiprocket.webhook.event_database import EventDatabaseService
# Notification service removed; sending done directly via Gupshup sender
from fashion_bot.shopify.webhook.templates_db import aget_client_template
import os

try:
    import redis.asyncio as redis_async
except Exception:
    redis_async = None
import certifi

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL") or os.getenv("REDIS_CONNECTION_STRING") or "redis://localhost:6379/0"

# ============================================================================
# IN-MEMORY DEDUPLICATION CACHE - First line of defense
# ============================================================================
_PROCESSED_EVENTS_CACHE = {}
_CACHE_TTL_SECONDS = 600  # Keep in cache for 10 minutes
_CACHE_MAX_SIZE = 10000  # Prevent memory bloat

# Redis cache TTL (second layer)
_REDIS_CACHE_TTL_SECONDS = 86400  # Keep in Redis for 1 day
_async_redis_client = None

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
    """Check if event was recently processed (in-memory cache) - with client_id"""
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
    """Mark event as processed in in-memory cache - with client_id"""
    cache_key = (client_id, order_id, event_key)
    _PROCESSED_EVENTS_CACHE[cache_key] = time.time()

async def _get_redis_client_for_events():
    """Get async Redis client for simple caching (no locks)."""
    global _async_redis_client

    if _async_redis_client is not None:
        return _async_redis_client
    if not redis_async:
        return None
    try:
        client_kwargs = {"decode_responses": True}
        if str(REDIS_URL).lower().startswith("rediss://"):
            client_kwargs["ssl_ca_certs"] = certifi.where()
            insecure = (os.getenv("REDIS_SSL_INSECURE", "").lower() in ("1", "true", "yes"))
            if insecure:
                client_kwargs["ssl_cert_reqs"] = None
        client = redis_async.Redis.from_url(REDIS_URL, **client_kwargs)
        await client.ping()
        _async_redis_client = client
        return client
    except Exception:
        _async_redis_client = None
        return None
# ============================================================================

def fill_template(template: str, order) -> str:
    def safe_get(d, key, default=""):
        return d[key] if isinstance(d, dict) and key in d else default
    return template.format(
        customer_firstname=safe_get(order, 'customer_name', "Customer"),
        order_number=safe_get(order, 'order_id', "") or safe_get(order, 'order_number', "") or "Order",
        total_price=safe_get(order, 'total', "") or safe_get(order, 'total_price', "") or "N/A",
    )

def extract_customer_name_and_phone(order: dict):
    shipping = order.get('shipping_address', {}) if isinstance(order, dict) else {}
    customer = order.get('customer', {}) if isinstance(order, dict) else {}
    default_address = customer.get('default_address', {}) if isinstance(customer, dict) else {}
    # Name
    name = (
        (shipping.get('first_name', '') + ' ' + shipping.get('last_name', '')).strip()
        or shipping.get('name')
        or (customer.get('first_name', '') + ' ' + customer.get('last_name', '')).strip()
        or default_address.get('name')
        or customer.get('name')
        or "Customer"
    )
    # Phone
    phone = (
        shipping.get('phone')
        or default_address.get('phone')
        or customer.get('phone')
        or order.get('phone')
        or ""
    )
    if phone:
        phone = phone.replace(" ", "")
        if phone.startswith("+91"):
            phone = phone
        elif phone.startswith("91") and len(phone) == 12:
            phone = "+" + phone
        elif len(phone) == 10:
            phone = "+91" + phone
        elif len(phone) > 10 and not phone.startswith("+"):
            phone = "+" + phone
    return name, phone

class ShipRocketEventProcessor:
    """Main processor for ShipRocket webhook events with 3-layer deduplication"""
    
    def __init__(self):
        self.db_service = EventDatabaseService()
        # Notification service removed; sending done directly via Gupshup sender
    
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
        LAYER 1: Check in-memory cache for recent processing.
        Fastest check (~1ms), catches duplicates in same instance.
        TTL: 10 minutes
        
        Args:
            order_id: Order ID
            event_key: Event key
            client_id: Client ID
        
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
        LAYER 2: Check Redis cache for recent processing.
        Slower than memory (~5-20ms) but survives restarts and works across instances.
        TTL: 1 day
        
        Args:
            order_id: Order ID
            event_key: Event key
            client_id: Client ID
        
        Returns: Duplicate response dict if found, None otherwise
        """
        redis_client = await _get_redis_client_for_events()
        if not redis_client:
            logger.warning(f"[DEDUP-LAYER-2] Redis not available, skipping Redis cache check")
            return None
        
        cache_key = f"shiprocket_event:{client_id}:{order_id}:{event_key}"
        
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
        """Mark event as processed in Redis cache - with client_id"""
        redis_client = await _get_redis_client_for_events()
        if not redis_client:
            return
        
        cache_key = f"shiprocket_event:{client_id}:{order_id}:{event_key}"
        
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
        delivery — is blocked here.

        Args:
            order_id: Order ID
            event_key: Event key
            webhook_data: Webhook data
            client_id: Client ID

        Returns: Duplicate response dict if another caller already claimed
        this event, None if this caller won the claim and should proceed.
        """
        logger.info(f"[DEDUP-LAYER-3] [CLIENT: {client_id}] Claiming database row for order: {order_id}, event: {event_key}")

        claim_start = time.time()
        claimed = await self.db_service.atry_claim_event(
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
        Process the event and record its snapshot.
        Called only after this caller has won the atomic database claim in
        _claim_database_layer().

        Args:
            order_id: Order ID
            event_key: Event key
            webhook_data: Webhook data
            client_id: Client ID
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
            await self.db_service.arelease_event_claim(order_id, event_key, webhook_data, client_id)
            raise

        # Update the audit log (notification_sent tracking); the dedup claim
        # itself was already taken in _claim_database_layer, before sending.
        logger.info(f"[PROCESSING] [CLIENT: {client_id}] Updating audit log in database: {order_id}, {event_key}")
        await self.db_service.amark_event_processed(order_id, event_key, webhook_data, client_id)

        logger.info(f"[PROCESSING] [CLIENT: {client_id}] ✅ Event processing completed - Order: {order_id}, Notification sent: {processing_result.get('notification_sent', False)}")
        
        return {
            'success': True,
            'message': 'Event processed successfully',
            'order_id': order_id,
            'event_name': event_key,
            'notification_sent': processing_result.get('notification_sent', False),
            'client_id': client_id,
            'details': processing_result,
            'dedup_layer': 'none_new_event'
        }
    
    async def process_webhook_event(self, webhook_data: Dict[str, Any], client_id: str = None) -> Dict[str, Any]:
        """
        Main webhook processing method with 3-layer deduplication and multi-client support.
        
        Flow:
        1. Validate and extract webhook data
        2. Save shipment history
        3. Check memory cache (fastest, 10 min TTL)
        4. Check Redis cache (cross-instance, 1 day TTL)
        5. Atomically claim the event in the database (persistent, race-safe gate)
        6. Process event and send notification
        7. Mark as processed in all layers
        
        Args:
            webhook_data: The webhook data
            client_id: The client ID if available
        """
        from fashion_bot.config_manager import aresolve_client_id
        if client_id is None:
            client_id = await aresolve_client_id()

        try:
            # Extract key information
            order_id = webhook_data.get('order_id')
            awb = webhook_data.get('awb')
            shipment_status = webhook_data.get('shipment_status')
            current_status = webhook_data.get('current_status')
            
            if not all([order_id, awb, shipment_status]):
                logger.warning(f"[CLIENT: {client_id}] Missing required fields in webhook data: {webhook_data}")
                return {
                    'success': False,
                    'error': 'Missing required fields',
                    'order_id': order_id,
                    'awb': awb,
                    'client_id': client_id
                }
            
            print(f"\n[SHIPROCKET_EVENT] 📋 Processing webhook - Order: {order_id}, AWB: {awb}, Status: {shipment_status}")
            logger.info(f"[DEDUP-CHECK] [CLIENT: {client_id}] Processing Shiprocket webhook for order: {order_id}, AWB: {awb}, Status: {shipment_status}")
            
            # Save shipment status to history (before deduplication to ensure all status changes are recorded)
            await self._save_shipment_status_history(webhook_data, client_id)
            
            # Resolve event key using client_id
            event_key = await aresolve_shiprocket_event_key(shipment_status, current_status, client_id)
            print(f"[SHIPROCKET_EVENT] Event key resolved: {event_key} for order {order_id}")
            logger.info(f"[DEDUP-CHECK] [CLIENT: {client_id}] Event key resolved: {event_key} for order {order_id}")
            
            if not event_key:
                print(f"[SHIPROCKET_EVENT] ⚠️  No event template found for status: {shipment_status}")
                logger.info(f"[CLIENT: {client_id}] No event template found for status: {shipment_status}")
                return {
                    'success': True,
                    'message': 'No event template found',
                    'order_id': order_id,
                    'status': shipment_status,
                    'client_id': client_id
                }
            
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
            logger.error(f"Error processing Shiprocket webhook event: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e),
                'order_id': webhook_data.get('order_id', 'Unknown'),
                'awb': webhook_data.get('awb', 'Unknown')
            }
    
    async def _save_shipment_status_history(self, webhook_data: Dict[str, Any], client_id: str) -> None:
        """
        Save shipment status history from webhook data.
        Uses batch insert to minimize database connections.
        
        Args:
            webhook_data: The webhook data
            client_id: The client ID
        """
        try:
            order_id = webhook_data.get('order_id')
            awb = webhook_data.get('awb')
            current_status = webhook_data.get('current_status')
            current_timestamp = webhook_data.get('current_timestamp')
            
            # Collect all statuses to save in a single batch
            statuses_to_save = []
            
            # Add current status
            if order_id and awb and current_status:
                statuses_to_save.append({
                    'order_id': order_id,
                    'awb': awb,
                    'status': current_status,
                    'status_code': None,
                    'location': None,
                    'activity': None,
                    'timestamp': current_timestamp
                })
            
            # Add scan history if available. ShipRocket sometimes sends
            # "scans": null, so .get(..., []) is not enough — coerce to list.
            scans = webhook_data.get('scans') or []
            for scan in scans:
                if not isinstance(scan, dict):
                    continue
                if scan.get('date') and scan.get('status'):
                    statuses_to_save.append({
                        'order_id': order_id,
                        'awb': awb,
                        'status': scan['status'],
                        'status_code': scan.get('sr-status'),
                        'location': scan.get('location'),
                        'activity': scan.get('activity'),
                        'timestamp': scan['date']
                    })
            
            # Save all statuses in a single batch (1 connection instead of N)
            if statuses_to_save:
                await self.db_service.asave_shipment_status_batch(statuses_to_save, client_id=client_id)
                    
        except Exception as e:
            logger.error(f"Error saving shipment status history for client {client_id}: {e}")
    
    async def _process_event(self, event_key: str, 
                           webhook_data: Dict[str, Any],
                           client_id: str = None) -> Dict[str, Any]:
        """
        Process a specific event.
        
        Args:
            event_key: The event key
            webhook_data: The webhook data
            client_id: The client ID
            
        Returns:
            Processing result
        """
        from fashion_bot.config_manager import aresolve_client_id
        if client_id is None:
            client_id = await aresolve_client_id()
        try:
            order_id = webhook_data.get('order_id')
            result = {
                'event_name': event_key,
                'notification_sent': False,
                'processing_time': datetime.now().isoformat()
            }
            
            # Send WhatsApp template using database configuration (client_id based)
            sent = False
            phone = None
            try:
                from fashion_bot.core.factory import ServiceFactory
                from fashion_bot.shipping.webhook.gupshup_template_sender import (
                    asend_gupshup_template_generic,
                )
                from fashion_bot.shopify.webhook.event_processor import _fetch_shopify_image_url

                state = {"client_id": client_id}
                order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
                sr_order = await order_service.aget_order_details(order_id, state=state)
                if sr_order:
                    customer_full_name, phone = extract_customer_name_and_phone(sr_order)
                    
                    # Extract first name only from full name
                    customer_firstname = customer_full_name.split()[0] if customer_full_name else 'Customer'
                    order_number = fill_template("{order_number}", sr_order)
                    order_value = (
                        sr_order.get('total')
                        or sr_order.get('total_price')
                        or sr_order.get('current_total_price')
                        or "N/A"
                    )

                    def _build_tracking_link() -> str:
                        fulfillments = sr_order.get('fulfillments') or []
                        if fulfillments:
                            latest = fulfillments[-1]
                            url = latest.get('tracking_url') or ''
                            if url:
                                return url
                            urls = latest.get('tracking_urls') or []
                            if urls:
                                return urls[0]
                        return (
                            sr_order.get('tracking_url')
                            or sr_order.get('shipments', {}).get('tracking_url')
                            or "N/A"
                        )

                    from fashion_bot.utils.template_param_resolver import (
                        build_template_params_from_context,
                    )
                    from fashion_bot.utils.whatsapp_api_version import (
                        WHATSAPP_API_VERSION_ENTERPRISE,
                        aget_whatsapp_api_version,
                    )

                    template_context = {
                        "customer_name": customer_firstname or "Customer",
                        "order_id": order_number or "Order",
                        "order_value": order_value,
                        "tracking_link": _build_tracking_link,
                    }
                    is_enterprise_template_flow = (
                        await aget_whatsapp_api_version(client_id)
                        == WHATSAPP_API_VERSION_ENTERPRISE
                    )
                    
                    logger.info(f"[CLIENT: {client_id}] Order: {order_number} phone=***{str(phone)[-4:] if phone else '?'}")
                    
                    # Fetch template from database using client_id
                    print(f"[TEMPLATE_LOOKUP] 🔍 Looking up template for event_key: {event_key}, channel: shiprocket")
                    logger.info(f"[CLIENT: {client_id}] Looking up template for event_key: {event_key}, channel: shiprocket")
                    cfg_db = await aget_client_template(client_id, 'shiprocket', event_key)
                    print(f"[TEMPLATE_LOOKUP] Template found: {bool(cfg_db)}")
                    logger.info(f"[CLIENT: {client_id}] Template lookup result: {cfg_db}")
                    
                    if cfg_db:
                        # Use template from database
                        template_id = cfg_db.get('template_id')
                        db_image_url = cfg_db.get('image_url', '')
                        param_order = cfg_db.get('param_order', [])
                        
                        print(f"[TEMPLATE_LOOKUP] ✅ Using template_id: {template_id}, params: {param_order}")
                        logger.info(f"[CLIENT: {client_id}] Using template_id: {template_id}, params: {param_order}")
                        
                        if is_enterprise_template_flow:
                            params = build_template_params_from_context(
                                param_order, template_context
                            )
                        else:
                            # Build params based on param_order from DB
                            params = []
                            for param_key in param_order:
                                if param_key == 'first_name':
                                    params.append(customer_firstname or 'Customer')
                                elif param_key == 'name':
                                    params.append(customer_firstname or 'Customer')
                                elif param_key == 'order_number':
                                    params.append(order_number or 'Order')
                                else:
                                    # If key is not a dynamic placeholder, treat it as a static string param
                                    params.append(param_key)
                        
                        logger.info(f"[CLIENT: {client_id}] Template params: {params}")
                        
                        # Check DB image_url: if NULL or empty, skip image entirely
                        db_image_url = (cfg_db.get('image_url') or '').strip()
                        if not db_image_url:
                            final_image_url = None
                            print(f"[IMAGE_FETCH] ℹ️  No image_url in DB, sending text-only template")
                            logger.info(f"[CLIENT: {client_id}] No image_url in DB, sending text-only template")
                        else:
                            dynamic_image_url = ''
                            try:
                                line_items = sr_order.get('line_items', [])
                                if line_items:
                                    first_item = line_items[0]
                                    product_id = first_item.get('product_id')
                                    variant_id = first_item.get('variant_id')
                                    
                                    if product_id or variant_id:
                                        print(f"[IMAGE_FETCH] 🖼️  Fetching image for product_id={product_id}, variant_id={variant_id}")
                                        dynamic_image_url = await _fetch_shopify_image_url(product_id, variant_id, client_id)
                                        if dynamic_image_url:
                                            print(f"[IMAGE_FETCH] ✅ Dynamic image fetched successfully")
                                        else:
                                            print(f"[IMAGE_FETCH] ⚠️  Dynamic image fetch returned empty")
                                        logger.info(f"[CLIENT: {client_id}] Dynamic image fetched: {bool(dynamic_image_url)}")
                            except Exception as img_error:
                                print(f"[IMAGE_FETCH] ❌ Failed to fetch dynamic image: {img_error}")
                                logger.warning(f"[CLIENT: {client_id}] Failed to fetch dynamic image: {img_error}")
                            
                            final_image_url = dynamic_image_url or db_image_url
                            print(f"[IMAGE_FETCH] Using {'dynamic' if dynamic_image_url else 'DB fallback'} image URL")
                            logger.info(f"[CLIENT: {client_id}] Using {'dynamic' if dynamic_image_url else 'DB'} image URL")
                        
                        # Send using generic function with DB config
                        print(f"[NOTIFICATION] 📤 Sending template to {phone} - Template ID: {template_id}")
                        resp = await asend_gupshup_template_generic(
                            phone, template_id, params, final_image_url,
                            client_id=client_id, event_key=event_key,
                            template_name=cfg_db.get('template_name'),
                            log_tag="SHIPROCKET",
                        )
                        sent = resp is not None
                        if sent:
                            print(f"[NOTIFICATION] ✅ Gupshup template sent successfully to {phone} for order {order_id}")
                            logger.info(f"[CLIENT: {client_id}] ✅ Gupshup template sent to {phone} for order {order_id}")
                        else:
                            print(f"[NOTIFICATION] ❌ Failed to send template to {phone}")
                    else:
                        print(f"[TEMPLATE_LOOKUP] ❌ No template found in DB for event_key: {event_key}")
                        logger.warning(f"[CLIENT: {client_id}] ❌ No template found in DB for event_key: {event_key}")
            except Exception as e:
                logger.error(f"[CLIENT: {client_id}] ❌ Error processing WhatsApp notification: {e}", exc_info=True)
            result['notification_sent'] = sent
            if sent:
                await self.db_service.amark_notification_sent(order_id, event_key, client_id)
             
            return result
            
        except Exception as e:
            logger.error(f"Error processing event {event_key}: {e}")
            return {
                'event_name': event_key,
                'error': str(e),
                'notification_sent': False
            }
    
    async def _extract_user_id(self, webhook_data: Dict[str, Any]) -> Optional[str]:
        """
        Extract user ID from webhook data.
        This method should be implemented based on your user identification system.
        
        Args:
            webhook_data: The webhook data
            
        Returns:
            User ID if found, None otherwise
        """
        # TODO: Implement based on your user identification system
        # This could be:
        # - Phone number from order
        # - Customer ID from order
        # - Email from order
        # - Any other unique identifier
        
        # For now, return None
        return None
    
    async def process_bulk_events(self, webhook_events: List[Dict[str, Any]], client_id: str = None) -> Dict[str, Any]:
        """
        Process multiple webhook events in bulk with multi-client support.
        
        Args:
            webhook_events: List of webhook events
            client_id: The client ID if available
            
        Returns:
            Bulk processing results
        """
        from fashion_bot.config_manager import aresolve_client_id
        if client_id is None:
            client_id = await aresolve_client_id()

        results = {
            'total': len(webhook_events),
            'successful': 0,
            'failed': 0,
            'client_id': client_id,
            'details': []
        }
        
        logger.info(f"[CLIENT: {client_id}] Processing {len(webhook_events)} bulk webhook events")
        
        for event in webhook_events:
            try:
                result = await self.process_webhook_event(event, client_id=client_id)
                results['details'].append(result)
                
                if result.get('success', False):
                    results['successful'] += 1
                else:
                    results['failed'] += 1
                    
            except Exception as e:
                logger.error(f"[CLIENT: {client_id}] Error processing bulk event: {e}")
                results['failed'] += 1
                results['details'].append({
                    'success': False,
                    'error': str(e),
                    'order_id': event.get('order_id', 'Unknown'),
                    'client_id': client_id
                })
        
        return results
    
    async def get_event_statistics(self, order_id: str = None, 
                                 event_name: str = None, 
                                 days: int = 30) -> Dict[str, Any]:
        """
        Get event processing statistics.
        
        Args:
            order_id: Optional order ID to filter
            event_name: Optional event name to filter
            days: Number of days to look back
            
        Returns:
            Statistics data
        """
        try:
            # This would implement statistics gathering from the database
            # For now, return a placeholder
            return {
                'total_events': 0,
                'notifications_sent': 0,
                'success_rate': 0.0,
                'period_days': days,
                'order_id': order_id,
                'event_name': event_name
            }
        except Exception as e:
            logger.error(f"Error getting event statistics: {e}")
            return {}
    
    async def cleanup_old_events(self, days_to_keep: int = 90) -> bool:
        """
        Clean up old events from the database.
        
        Args:
            days_to_keep: Number of days to keep events
            
        Returns:
            True if cleanup successful, False otherwise
        """
        try:
            return await self.db_service.acleanup_old_events(days_to_keep)
        except Exception as e:
            logger.error(f"Error cleaning up old events: {e}")
            return False
