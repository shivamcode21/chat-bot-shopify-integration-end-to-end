from fastapi import APIRouter, Request
from langsmith import traceable
import logging
import base64
import re
import time
from typing import Dict, Any, Optional

from fashion_bot.trace_context import generate_trace_id, set_trace_id
from fashion_bot.langsmith_config import setup_langsmith_for_service, get_langsmith_config
from fashion_bot.utils.utils import log_with_trace_id
from fashion_bot.shiprocket.webhook.event_processor import ShipRocketEventProcessor
from fashion_bot.config_manager import aresolve_client_id
from fashion_bot.shopify.webhook.abandoned_checkout_webhook import transform_checkout_url_to_param
from fashion_bot.shopify.webhook.gupshup_template_sender import asend_shopify_gupshup_template_generic
from fashion_bot.shopify.webhook.templates_db import aget_client_template
from fashion_bot.utils.redis_client import get_shared_async_redis_client
from fashion_bot.utils.template_param_resolver import build_template_params_from_context
from fashion_bot.utils.whatsapp_api_version import (
    WHATSAPP_API_VERSION_ENTERPRISE,
    aget_whatsapp_api_version,
)
# Queue producer — dramatiq-free import; offloads to a worker only when the
# 'shiprocket' lane is enabled, else awaits inline (unchanged behaviour).
from fashion_bot.workers.enqueue import submit_or_inline
from fashion_bot.workers.config import JOB_SHIPROCKET_CART_EVENT, JOB_SHIPROCKET_EVENT

router = APIRouter()
abandoncart_router = APIRouter()
LANGSMITH_ENABLED = setup_langsmith_for_service("shiprocket")
LANGSMITH_CONFIG = get_langsmith_config("gupshup")

# Initialize event processor
event_processor = ShipRocketEventProcessor()

ABANDONCART_DEDUP_TTL_SECONDS = 12 * 60 * 60
_abandoncart_local_dedup: Dict[str, float] = {}

def _decode_client_id(encoded_client_id: str) -> str:
    """
    Decode base64 encoded client_id from URL.
    
    Args:
        encoded_client_id: Base64 encoded client_id
        
    Returns:
        Decoded client_id string
    """
    try:
        decoded_bytes = base64.b64decode(encoded_client_id)
        return decoded_bytes.decode('utf-8')
    except Exception as e:
        logging.error(f"Error decoding client_id '{encoded_client_id}': {e}")
        raise ValueError(f"Invalid base64 encoded client_id: {e}")


def _clean_url(value: Any) -> str:
    """Return a plain URL from direct or markdown-link webhook values."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    markdown_match = re.fullmatch(r"\[[^\]]*\]\(([^)]+)\)", value)
    if markdown_match:
        return markdown_match.group(1).strip()
    return value


def _first_item(webhook_data: Dict[str, Any]) -> Dict[str, Any]:
    items = webhook_data.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return {}


def _nested_mapping(webhook_data: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = webhook_data.get(key)
    return value if isinstance(value, dict) else {}


def _extract_abandoncart_phone(webhook_data: Dict[str, Any]) -> Optional[str]:
    for value in (
        webhook_data.get("phone_number"),
        _nested_mapping(webhook_data, "shipping_address").get("phone"),
        _nested_mapping(webhook_data, "billing_address").get("phone"),
    ):
        if value:
            phone = str(value).strip()
            return phone[1:] if phone.startswith("+") else phone
    return None


def _extract_abandoncart_name(webhook_data: Dict[str, Any]) -> str:
    for value in (
        webhook_data.get("first_name"),
        _nested_mapping(webhook_data, "shipping_address").get("first_name"),
        _nested_mapping(webhook_data, "billing_address").get("first_name"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()

    for value in (
        _nested_mapping(webhook_data, "shipping_address").get("name"),
        _nested_mapping(webhook_data, "billing_address").get("name"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip().split()[0]

    return "Customer"


def _extract_abandoncart_checkout_url(webhook_data: Dict[str, Any]) -> str:
    for key in ("checkout_url", "abandoned_checkout_url", "recovery_url", "web_url", "url"):
        url = _clean_url(webhook_data.get(key))
        if url:
            return url
    return ""


def _extract_abandoncart_product_url(webhook_data: Dict[str, Any]) -> str:
    first_item = _first_item(webhook_data)
    for key in ("url", "product_url"):
        url = _clean_url(first_item.get(key))
        if url:
            return url

    product_id = first_item.get("product_id") or webhook_data.get("product_id")
    variant_id = first_item.get("variant_id") or webhook_data.get("variant_id")
    if not product_id:
        product_ids = webhook_data.get("product_id_list")
        product_id = product_ids[0] if isinstance(product_ids, list) and product_ids else None
    if not variant_id:
        variant_ids = webhook_data.get("variant_id_list")
        variant_id = variant_ids[0] if isinstance(variant_ids, list) and variant_ids else None

    if not product_id:
        return ""

    if variant_id:
        return f"https://groovee.in/products/{product_id}?variant={variant_id}"
    return f"https://groovee.in/products/{product_id}"


def _extract_abandoncart_image_url(webhook_data: Dict[str, Any]) -> str:
    for value in (webhook_data.get("img_url"), _first_item(webhook_data).get("img_url")):
        url = _clean_url(value)
        if url:
            return url
    return ""


def _extract_abandoncart_item_name(webhook_data: Dict[str, Any]) -> str:
    first_item = _first_item(webhook_data)
    for value in (
        first_item.get("title"),
        first_item.get("name"),
        webhook_data.get("item_name_list", [None])[0]
        if isinstance(webhook_data.get("item_name_list"), list)
        else None,
        webhook_data.get("item_title_list", [None])[0]
        if isinstance(webhook_data.get("item_title_list"), list)
        else None,
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _abandoncart_dedup_key(client_id: str, phone: str, cart_id: Any) -> str:
    return f"shiprocket_abandoncart_dedup:{client_id}:{phone}:{cart_id}"


def _prune_abandoncart_local_dedup(now: float) -> None:
    expired_keys = [
        key for key, expires_at in _abandoncart_local_dedup.items()
        if expires_at <= now
    ]
    for key in expired_keys:
        _abandoncart_local_dedup.pop(key, None)


async def _already_seen_abandoncart_event(
    *,
    client_id: str,
    phone: str,
    cart_id: Any,
) -> bool:
    """
    Deduplicate Fastrr/Shiprocket abandon-cart events for 12 hours.

    Process-local cache handles repeated hits on this pod. Redis SET NX handles
    cross-pod duplicates and also stores the same 12-hour TTL. Redis failures
    fail open after local dedupe so webhook delivery is not blocked.
    """
    now = time.monotonic()
    _prune_abandoncart_local_dedup(now)

    key = _abandoncart_dedup_key(client_id, phone, cart_id)
    if _abandoncart_local_dedup.get(key, 0) > now:
        return True

    redis_client = await get_shared_async_redis_client()
    if redis_client is None:
        _abandoncart_local_dedup[key] = now + ABANDONCART_DEDUP_TTL_SECONDS
        return False

    try:
        created = await redis_client.set(
            key,
            "1",
            nx=True,
            ex=ABANDONCART_DEDUP_TTL_SECONDS,
        )
        _abandoncart_local_dedup[key] = now + ABANDONCART_DEDUP_TTL_SECONDS
        return not bool(created)
    except Exception as ex:
        logging.warning(
            f"[ABANDONCART-WEBHOOK] Redis dedupe unavailable for key={key}: {ex}"
        )
        _abandoncart_local_dedup[key] = now + ABANDONCART_DEDUP_TTL_SECONDS
        return False


@abandoncart_router.post("/event/webhook/{encoded_client_id}")
async def abandoncart_webhook(request: Request, encoded_client_id: str):
    """
    Abandon cart webhook endpoint with multi-client support.

    URL Format: /abandoncart/event/webhook/{base64_encoded_client_id}
    """
    trace_id = generate_trace_id()
    set_trace_id(trace_id)

    try:
        client_id = _decode_client_id(encoded_client_id)
        logging.info(
            f"[ABANDONCART-WEBHOOK] Decoded client_id: {client_id} "
            f"(from: {encoded_client_id})"
        )
    except ValueError as e:
        logging.error(f"[ABANDONCART-WEBHOOK] Invalid client_id encoding: {e}")
        return {
            "status": "error",
            "error": str(e),
            "trace_id": trace_id
        }

    from fashion_bot.utils.client_id_utils import is_client_blocklisted
    if is_client_blocklisted(client_id):
        logging.info(
            f"[ABANDONCART-WEBHOOK] 🚫 Blocked webhook for blocklisted client_id: {client_id}"
        )
        return {"status": "blocked", "message": "Client is blocklisted", "trace_id": trace_id}

    try:
        webhook_data = await request.json()
        log_with_trace_id(
            trace_id,
            f"[CLIENT: {client_id}] Received abandon cart webhook: {webhook_data}",
            "info",
        )

        cart_id = webhook_data.get("cart_id") or webhook_data.get("id") or "unknown"
        cart_token = webhook_data.get("cart_token") or ""
        phone = _extract_abandoncart_phone(webhook_data)
        if not phone:
            logging.warning(
                f"[ABANDONCART-WEBHOOK] No phone number found for cart_id={cart_id}"
            )
            return {
                "status": "skipped",
                "reason": "guest_checkout_phone_not_available",
                "cart_id": cart_id,
                "client_id": client_id,
                "trace_id": trace_id,
            }

        if await _already_seen_abandoncart_event(
            client_id=client_id,
            phone=phone,
            cart_id=cart_id,
        ):
            logging.info(
                f"[ABANDONCART-WEBHOOK] Duplicate skipped for "
                f"client_id={client_id}, phone={phone}, cart_id={cart_id}"
            )
            return {
                "status": "skipped",
                "reason": "duplicate_abandoncart_event",
                "cart_id": cart_id,
                "phone": phone,
                "client_id": client_id,
                "trace_id": trace_id,
            }

        customer_name = _extract_abandoncart_name(webhook_data)
        checkout_url = _extract_abandoncart_checkout_url(webhook_data)
        checkout_param = transform_checkout_url_to_param(checkout_url) if checkout_url else ""
        product_url = _extract_abandoncart_product_url(webhook_data)
        image_url_webhook = _extract_abandoncart_image_url(webhook_data)
        item_name = _extract_abandoncart_item_name(webhook_data)

        event_key = "ABANDON_CHECKOUT"
        channel = "shiprocket"
        template_config = await aget_client_template(client_id, channel, event_key)
        if not template_config:
            logging.warning(
                f"[ABANDONCART-WEBHOOK] No template found for "
                f"event_key={event_key}, channel={channel}, client_id={client_id}"
            )
            return {
                "status": "skipped",
                "reason": f"No template configured for {event_key}",
                "cart_id": cart_id,
                "phone": phone,
                "client_id": client_id,
                "trace_id": trace_id,
            }

        template_id = template_config.get("template_id")
        image_url_db = template_config.get("image_url")
        param_order = template_config.get("param_order", [])
        template_name = template_config.get("template_name", "abandoned_checkout_reminder")

        if (
            await aget_whatsapp_api_version(client_id, trace_id)
            == WHATSAPP_API_VERSION_ENTERPRISE
        ):
            params = build_template_params_from_context(
                param_order,
                {
                    "customer_name": customer_name,
                    "abandoned_checkout_url": checkout_param or checkout_url or product_url or "",
                    "product_url": checkout_param or checkout_url or product_url or "",
                    "item_name": item_name,
                    "cart_token": cart_token,
                    "cart_id": str(cart_id),
                    "order_value": str(webhook_data.get("total_price", "")),
                },
            )
        else:
            params = []
            for param_key in param_order:
                if param_key in ("name", "first_name"):
                    params.append(customer_name)
                elif param_key in ("abandoned_checkout_url", "product_url"):
                    params.append(checkout_param or checkout_url or product_url or "")
                elif param_key in ("item_name", "product_name", "title"):
                    params.append(item_name)
                elif param_key in ("cart_token", "checkout_token"):
                    params.append(cart_token)
                elif param_key in ("cart_id", "checkout_id"):
                    params.append(str(cart_id))
                elif param_key in ("total_price", "amount"):
                    params.append(str(webhook_data.get("total_price", "")))
                else:
                    params.append("")

        image_url = image_url_webhook or image_url_db or  ""
        logging.info(
            f"[ABANDONCART-WEBHOOK] Sending template to {phone}: "
            f"template_id={template_id}, params={params}, image_url={image_url or 'N/A'}"
        )

        result = await submit_or_inline(
            JOB_SHIPROCKET_CART_EVENT,
            {
                "destination_phone": phone,
                "template_id": template_id,
                "params": params,
                "image_url": image_url,
                "client_id": client_id,
                "event_key": event_key,
                "template_name": template_name,
                "trace_id": trace_id,
            },
            lambda: asend_shopify_gupshup_template_generic(
                destination_phone=phone,
                template_id=template_id,
                params=params,
                image_url=image_url,
                client_id=client_id,
                event_key=event_key,
                template_name=template_name,
            ),
        )

        if result:
            return {
                "status": "success",
                "message": "Abandon cart template sent",
                "cart_id": cart_id,
                "phone": phone,
                "template_id": template_id,
                "params": params,
                "client_id": client_id,
                "trace_id": trace_id,
            }

        return {
            "status": "error",
            "message": "Failed to send template",
            "cart_id": cart_id,
            "phone": phone,
            "client_id": client_id,
            "trace_id": trace_id,
        }
    except Exception as e:
        log_with_trace_id(
            trace_id,
            f"[CLIENT: {client_id}] Abandon cart webhook error: {e}",
            "error",
        )
        return {
            "status": "error",
            "error": str(e),
            "client_id": client_id,
            "trace_id": trace_id
        }

@router.post("/event/webhook/{encoded_client_id}")
# NOTE: @traceable removed - only conversation traces should go to LangSmith, not webhook handlers
async def webhook(request: Request, encoded_client_id: str):
    """
    ShipRocket webhook endpoint with multi-client support.
    
    URL Format: /event/webhook/{base64_encoded_client_id}
    
    The client_id should be base64 encoded in the webhook URL.
    Example: If client_id is "groovee", encode it to "Z3Jvb3ZlZQ==" and use:
             /event/webhook/Z3Jvb3ZlZQ==
    """
    trace_id = generate_trace_id()
    set_trace_id(trace_id)
    
    # Decode client_id from URL
    try:
        client_id = _decode_client_id(encoded_client_id)
        logging.info(f"[SHIPROCKET-WEBHOOK] Decoded client_id: {client_id} (from: {encoded_client_id})")
    except ValueError as e:
        logging.error(f"[SHIPROCKET-WEBHOOK] Invalid client_id encoding: {e}")
        return {
            "status": "error",
            "error": str(e),
            "trace_id": trace_id
        }

    from fashion_bot.utils.client_id_utils import is_client_blocklisted
    if is_client_blocklisted(client_id):
        logging.info(f"[SHIPROCKET-WEBHOOK] 🚫 Blocked webhook for blocklisted client_id: {client_id}")
        return {"status": "blocked", "message": "Client is blocklisted", "trace_id": trace_id}

    # Log LangSmith trace ID for easy correlation
    if LANGSMITH_ENABLED:
        from langsmith import get_current_run_tree
        current_run = get_current_run_tree()
        langsmith_trace_id = current_run.trace_id if current_run else None
        log_with_trace_id(trace_id, f"Webhook started - LangSmith trace_id: {langsmith_trace_id}, client_id: {client_id}", "info")

    try:
        # Parse webhook data
        webhook_data = await request.json()
        print(f"\n[WEBHOOK_RECEIVED] 📦 Shiprocket webhook received for client: {client_id}")
        print(f"[WEBHOOK_RECEIVED] Order ID: {webhook_data.get('order_id', 'N/A')}, AWB: {webhook_data.get('awb', 'N/A')}, Status: {webhook_data.get('shipment_status', 'N/A')}")
        log_with_trace_id(trace_id, f"[CLIENT: {client_id}] Received ShipRocket webhook: {webhook_data}", "info")

        # Validate webhook data
        if not _validate_webhook_data(webhook_data):
            print(f"[WEBHOOK_RECEIVED] ❌ Invalid webhook data for client: {client_id}")
            log_with_trace_id(trace_id, f"[CLIENT: {client_id}] Invalid webhook data received", "warning")
            return {
                "status": "error", 
                "message": "Invalid webhook data",
                "trace_id": trace_id,
                "client_id": client_id
            }

        # Process the webhook event with client_id (offloaded to a worker when
        # the 'shiprocket' lane is enabled; inline otherwise).
        print(f"[WEBHOOK_RECEIVED] ⚙️  Processing webhook for order: {webhook_data.get('order_id', 'N/A')}")
        processing_result = await submit_or_inline(
            JOB_SHIPROCKET_EVENT,
            {"webhook_data": webhook_data, "client_id": client_id, "trace_id": trace_id},
            lambda: event_processor.process_webhook_event(webhook_data, client_id=client_id),
        )
        
        # Log processing result
        if processing_result.get('success'):
            print(f"[WEBHOOK_RECEIVED] ✅ Event processed successfully: {processing_result.get('event_name', 'Unknown')} - Order: {processing_result.get('order_id')}")
            print(f"[WEBHOOK_RECEIVED] Notification sent: {processing_result.get('notification_sent', False)}")
            log_with_trace_id(trace_id, 
                f"[CLIENT: {client_id}] Event processed successfully: {processing_result.get('event_name', 'Unknown')} - Order: {processing_result.get('order_id')}", 
                "info")
        else:
            print(f"[WEBHOOK_RECEIVED] ❌ Event processing failed: {processing_result.get('error', 'Unknown error')} - Order: {processing_result.get('order_id')}")
            log_with_trace_id(trace_id, 
                f"[CLIENT: {client_id}] Event processing failed: {processing_result.get('error', 'Unknown error')} - Order: {processing_result.get('order_id')}", 
                "error")

        # Return response
        return {
            "status": "ok" if processing_result.get('success') else "error",
            "message": processing_result.get('message', 'Event processed'),
            "order_id": processing_result.get('order_id'),
            "event_name": processing_result.get('event_name'),
            "notification_sent": processing_result.get('notification_sent', False),
            "client_id": client_id,
            "trace_id": trace_id
        }

    except Exception as e:
        log_with_trace_id(trace_id, f"[CLIENT: {client_id}] ShipRocket webhook error: {e}", "error")
        return {
            "status": "error",
            "error": str(e),
            "client_id": client_id,
            "trace_id": trace_id
        }

# Backward compatibility endpoint (uses default client_id)
@router.post("/event/webhook")
# NOTE: @traceable removed - only conversation traces should go to LangSmith, not webhook handlers
async def webhook_legacy(request: Request):
    """
    Legacy ShipRocket webhook endpoint (backward compatibility).
    Uses default client_id from configuration.
    
    For new integrations, use /event/webhook/{base64_encoded_client_id}
    """
    # Use default client_id for backward compatibility
    resolved_client_id = await aresolve_client_id()
    logging.info(f"[SHIPROCKET-WEBHOOK-LEGACY] Using resolved client_id: {resolved_client_id}")

    # Encode the default client_id and forward to the main handler
    encoded_default = base64.b64encode(resolved_client_id.encode('utf-8')).decode('utf-8')
    return await webhook(request, encoded_default)

@router.post("/event/webhook/bulk/{encoded_client_id}")
# NOTE: @traceable removed - only conversation traces should go to LangSmith, not webhook handlers
async def bulk_webhook(request: Request, encoded_client_id: str):
    """Bulk ShipRocket webhook endpoint for processing multiple events with multi-client support"""
    trace_id = generate_trace_id()
    set_trace_id(trace_id)
    
    # Decode client_id from URL
    try:
        client_id = _decode_client_id(encoded_client_id)
        logging.info(f"[SHIPROCKET-BULK-WEBHOOK] Decoded client_id: {client_id}")
    except ValueError as e:
        logging.error(f"[SHIPROCKET-BULK-WEBHOOK] Invalid client_id encoding: {e}")
        return {
            "status": "error",
            "error": str(e),
            "trace_id": trace_id
        }

    from fashion_bot.utils.client_id_utils import is_client_blocklisted
    if is_client_blocklisted(client_id):
        logging.info(f"[SHIPROCKET-BULK-WEBHOOK] 🚫 Blocked webhook for blocklisted client_id: {client_id}")
        return {"status": "blocked", "message": "Client is blocklisted", "trace_id": trace_id}

    try:
        # Parse bulk webhook data
        bulk_data = await request.json()
        webhook_events = bulk_data.get('events', [])
        
        log_with_trace_id(trace_id, f"[CLIENT: {client_id}] Received bulk webhook with {len(webhook_events)} events", "info")

        if not webhook_events:
            return {
                "status": "error",
                "message": "No events provided",
                "client_id": client_id,
                "trace_id": trace_id
            }

        # Process bulk events with client_id
        bulk_result = await event_processor.process_bulk_events(webhook_events, client_id=client_id)
        
        log_with_trace_id(trace_id, 
            f"[CLIENT: {client_id}] Bulk processing completed: {bulk_result['successful']} successful, {bulk_result['failed']} failed", 
            "info")

        return {
            "status": "ok",
            "message": "Bulk processing completed",
            "total_events": bulk_result['total'],
            "successful": bulk_result['successful'],
            "failed": bulk_result['failed'],
            "client_id": client_id,
            "trace_id": trace_id
        }

    except Exception as e:
        log_with_trace_id(trace_id, f"[CLIENT: {client_id}] Bulk webhook error: {e}", "error")
        return {
            "status": "error",
            "error": str(e),
            "client_id": client_id,
            "trace_id": trace_id
        }

# Backward compatibility for bulk webhook
@router.post("/event/webhook/bulk")
# NOTE: @traceable removed - only conversation traces should go to LangSmith, not webhook handlers
async def bulk_webhook_legacy(request: Request):
    """Legacy bulk webhook endpoint (backward compatibility)"""
    resolved_client_id = await aresolve_client_id()
    logging.info(f"[SHIPROCKET-BULK-WEBHOOK-LEGACY] Using resolved client_id: {resolved_client_id}")
    encoded_default = base64.b64encode(resolved_client_id.encode('utf-8')).decode('utf-8')
    return await bulk_webhook(request, encoded_default)

@router.get("/events/statistics")
async def get_event_statistics(order_id: str = None, event_name: str = None, days: int = 30):
    """Get event processing statistics"""
    try:
        stats = await event_processor.get_event_statistics(order_id, event_name, days)
        return {
            "status": "ok",
            "statistics": stats
        }
    except Exception as e:
        logging.error(f"Error getting event statistics: {e}")
        return {
            "status": "error",
            "error": str(e)
        }

@router.post("/events/cleanup")
async def cleanup_old_events(days_to_keep: int = 90):
    """Clean up old events from database"""
    try:
        success = await event_processor.cleanup_old_events(days_to_keep)
        return {
            "status": "ok" if success else "error",
            "message": "Cleanup completed" if success else "Cleanup failed",
            "days_kept": days_to_keep
        }
    except Exception as e:
        logging.error(f"Error cleaning up old events: {e}")
        return {
            "status": "error",
            "error": str(e)
        }

def _validate_webhook_data(webhook_data: Dict[str, Any]) -> bool:
    """
    Validate webhook data structure.
    
    Args:
        webhook_data: The webhook data to validate
        
    Returns:
        True if valid, False otherwise
    """
    required_fields = ['order_id', 'awb', 'shipment_status']
    
    # Check if all required fields are present
    for field in required_fields:
        if field not in webhook_data or not webhook_data[field]:
            logging.warning(f"Missing required field: {field}")
            return False
    
    # Validate data types
    if not isinstance(webhook_data['order_id'], str):
        logging.warning("order_id must be a string")
        return False
    
    if not isinstance(webhook_data['awb'], str):
        logging.warning("awb must be a string")
        return False
    
    if not isinstance(webhook_data['shipment_status'], str):
        logging.warning("shipment_status must be a string")
        return False
    
    return True
