"""
Delivery and order status utility functions.
These are vendor-agnostic business logic functions.
"""
import logging
import json
import time
from typing import Dict, Any, Optional, List, Tuple
from fashion_bot.database_manager import get_async_postgres_connection

logger = logging.getLogger(__name__)

_DELIVERY_PARTNERS_CACHE: Dict[str, Dict[str, Any]] = {}
_DELIVERY_PARTNERS_CACHE_TTL = 600  # 10 minutes

def determine_delivery_status_and_message(order_dto: Dict[str, Any]) -> Dict[str, str]:
    """
    Determine delivery timeline status and message based on order data.
    This is vendor-agnostic business logic.
   
    Returns:
        dict with keys: timeline_status, message, formatted_etd
    """
    awb = order_dto.get("awb")
    delivery_date = order_dto.get("delivery_date")
    status = order_dto.get("partner_status", "").upper()
   
    # Determine status and message based on AWB and delivery date
    if not awb:
        return {
            "timeline_status": "Not yet shipped",
            "message": "Your order hasn't shipped yet. We'll notify you once it moves!",
            "formatted_etd": "N/A"
        }
    else:
        formatted_etd = delivery_date if delivery_date else "soon! 🚚"
        return {
            "timeline_status": "Shipped",
            "message": f"Expected delivery: {formatted_etd}",
            "formatted_etd": formatted_etd
        }


def categorize_items_by_fulfillment(order_dto: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """
    Categorize order items into fulfilled and pending based on order status.
    This is vendor-agnostic business logic.
   
    Returns:
        tuple: (fulfilled_items, pending_items)
    """
    items = order_dto.get("items", [])
    status = order_dto.get("status", "").upper()
   
    # If order is delivered, in transit, or shipped, items are fulfilled
    if status in ["DELIVERED", "IN_TRANSIT", "SHIPPED"]:
        fulfilled_items = items
        pending_items = []
    else:
        fulfilled_items = []
        pending_items = items
   
    return fulfilled_items, pending_items


async def aget_all_delivery_partners_for_client(client_id: str) -> List[Dict[str, Any]]:
    """
    Fetch all configured delivery partners for a client from delivery_partner_integrations table.

    Uses a single async connection for both queries.

    Args:
        client_id: The client UUID

    Returns:
        List of configured delivery partners in format:
        [{"name": "partner_name", "connected": true/false}, ...]
    """
    if not client_id:
        logger.error("❌ aget_all_delivery_partners_for_client: client_id is required")
        return []

    try:
        delivery_partners: List[Dict[str, Any]] = []
        shiprocket_config = None

        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                logger.info(f"🔍 Fetching configured delivery partners for client_id: {client_id}")
                await cur.execute("""
                    SELECT dp.id, dp.name, dpi.is_connected
                    FROM delivery_partner_integrations dpi
                    JOIN delivery_partners dp ON dpi.partner_id = dp.id
                    WHERE dpi.client_id = %s
                    AND dpi.status = 'active'
                    ORDER BY dpi.priority ASC, dpi.created_at DESC
                """, (client_id,))

                rows = await cur.fetchall()
                if rows:
                    for row in rows:
                        if isinstance(row, dict):
                            is_connected = row.get('is_connected', 0)
                            delivery_partners.append({
                                "name": row['name'],
                                "connected": bool(is_connected)
                            })
                        else:
                            is_connected = row[2] if len(row) > 2 else 0
                            delivery_partners.append({
                                "name": row[1],
                                "connected": bool(is_connected)
                            })
                    logger.info(f"📦 Loaded {len(delivery_partners)} configured delivery partners from database")
                else:
                    logger.warning(f"⚠️ No active delivery partner integrations found for client_id: {client_id}")

                await cur.execute("""
                    SELECT config_value
                    FROM client_configs
                    WHERE client_id = %s
                    AND config_key = 'shiprocket_details'
                    LIMIT 1
                """, (client_id,))

                result = await cur.fetchone()
                if result:
                    config_value = result.get('config_value') if isinstance(result, dict) else result[0]
                    if config_value:
                        if isinstance(config_value, str):
                            try:
                                shiprocket_config = json.loads(config_value)
                            except json.JSONDecodeError:
                                logger.warning(f"⚠️ Failed to parse shiprocket_details as JSON: {config_value}")
                        else:
                            shiprocket_config = config_value

        shiprocket_exists = any(p['name'].lower() == 'shiprocket' for p in delivery_partners)
        if shiprocket_config and not shiprocket_exists:
            delivery_partners.append({
                "name": "shiprocket",
                "connected": True
            })

        logger.info(f"✅ Returning {len(delivery_partners)} delivery partners for client_id: {client_id}")
        return delivery_partners

    except Exception as e:
        logger.error(f"❌ Error fetching delivery partners for client_id {client_id}: {str(e)}", exc_info=True)
        return []


async def aget_all_delivery_partners_cached(client_id: str) -> List[Dict[str, Any]]:
    """Return delivery partners for a client with in-memory TTL caching."""
    if not client_id:
        return []

    cached = _DELIVERY_PARTNERS_CACHE.get(client_id)
    if cached and (time.monotonic() - cached["ts"]) < _DELIVERY_PARTNERS_CACHE_TTL:
        return cached["data"]

    partners = await aget_all_delivery_partners_for_client(client_id)
    _DELIVERY_PARTNERS_CACHE[client_id] = {"data": partners, "ts": time.monotonic()}
    return partners


async def aget_non_integrated_partners(client_id: Optional[str]) -> List[str]:
    """
    Return names of active delivery partners that we do *not* have a logistics
    adapter for (i.e. anything not in
    ``utils.delivery_partner_utils.INTEGRATED_PARTNERS``).

    Used by the orchestrator to decide whether a cancel/update needs to be
    *escalated to a human agent* for manual sync, after we've already done
    what we can on the integrated partner(s).

    Replaces the old ``aget_non_shiprocket_partners`` helper (which became
    semantically wrong once Delhivery shipped).
    """
    if not client_id:
        return []
    try:
        from fashion_bot.utils.delivery_partner_utils import INTEGRATED_PARTNERS

        all_partners = await aget_all_delivery_partners_cached(client_id)
        return [
            p["name"] for p in all_partners
            if (p.get("name") or "").lower() not in INTEGRATED_PARTNERS and p.get("connected")
        ]
    except Exception as e:
        logger.error(f"❌ Error checking non-integrated partners for {client_id}: {e}", exc_info=True)
        return []


# Back-compat alias. New code should call ``aget_non_integrated_partners``.
aget_non_shiprocket_partners = aget_non_integrated_partners


async def anotify_agent_for_non_integrated_partners(
    client_id: Optional[str],
    order_id: str,
    action_type: str,
    action_details: str,
    non_shiprocket_partners: Optional[List[str]] = None,
    customer_phone: str = "",
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Send a WhatsApp notification to the client's agent that non-Shiprocket
    delivery partners need manual syncing after a cancel/update.

    Called by ``EscalationOrchestrator.aescalate_to_agent`` when
    ``category == "Courier Update Pending"``.

    Returns:
        {"notified": bool, "partners": list[str]}
    """
    if not client_id:
        logger.warning("⚠️ anotify_agent_for_non_integrated_partners: no client_id, skipping")
        return {"notified": False, "partners": []}

    try:
        non_integrated = non_shiprocket_partners or await aget_non_integrated_partners(client_id)

        if not non_integrated:
            return {"notified": False, "partners": []}

        from fashion_bot.agent_config import aget_agent_phone_number
        agent_phone = await aget_agent_phone_number(client_id=client_id)
        if not agent_phone:
            logger.warning(f"⚠️ No AGENT_PHONE_NUMBER for client {client_id}; cannot notify about non-integrated partners")
            return {"notified": False, "partners": non_integrated}

        from fashion_bot.utils.utils import get_trace_id, log_with_trace_id
        from fashion_bot.utils.phone_number_utils import strip_country_code
        import pytz
        from datetime import datetime

        trace_id = get_trace_id(state) if state else "N/A"
        raw_phone = customer_phone or (state or {}).get("phone_number", "Unknown")
        resolved_phone = strip_country_code(raw_phone) if raw_phone != "Unknown" else "Unknown"
        ist_now = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")
        partner_names = ", ".join(non_integrated)

        message = (
            f"[Courier Update Pending Required]\n"
            f"⏰ Time: {ist_now} IST\n"
            f"📦 Order: {order_id}\n"
            f"📝 Action: {action_type.capitalize()} on Shopify & integrated logistics partner(s)\n"
            f"📋 Details: {action_details}\n"
            f"🚚 Other (non-integrated) delivery partners: {partner_names}\n"
            f"👉 Please update these partners manually.\n"
            f"📞 Customer phone: {resolved_phone}\n"
            f"🔍 Trace: {trace_id}"
        )

        from fashion_bot.gupshup_webhook import send_message
        await send_message(agent_phone, message, trace_id=trace_id, client_id=client_id)
        logger.info(f"📤 Courier Update Pending notification sent to agent for order {order_id} (partners: {partner_names})")

        try:
            from fashion_bot.utils.escalation_helper import alog_escalation_from_state, prepare_escalation_metadata
            if state:
                metadata = prepare_escalation_metadata(
                    order_id=order_id,
                    phone_number=resolved_phone,
                    trace_id=trace_id,
                    escalation_type="delivery_partner_sync",
                    escalation_classification="system",
                    additional_data={"non_integrated_partners": non_integrated, "action_type": action_type},
                )
                await alog_escalation_from_state(
                    state=state,
                    category="Courier Update Pending",
                    reason=(
                        f"Order {order_id} {action_type} on Shopify & integrated logistics. "
                        f"Non-integrated partners need manual update: {partner_names}"
                    ),
                    action_required=f"Update {partner_names} manually for order {order_id}",
                    metadata=metadata,
                )
        except Exception as log_err:
            logger.warning(f"⚠️ Failed to log Courier Update Pending escalation (non-critical): {log_err}")

        return {"notified": True, "partners": non_integrated}

    except Exception as e:
        logger.error(f"❌ Error in anotify_agent_for_non_integrated_partners for order {order_id}: {e}", exc_info=True)
        return {"notified": False, "partners": []}
