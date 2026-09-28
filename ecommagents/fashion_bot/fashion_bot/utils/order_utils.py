import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any

from fashion_bot.shopify.order_tags import (
    OrderTag,
    PAYMENT_REFERENCE_TAG_PREFIX,
    PAYMENT_TXN_TAG_PREFIX,
)

logger = logging.getLogger("order_utils")

IST = timezone(timedelta(hours=5, minutes=30))


def classify_payment_type(order_data: dict) -> dict:
    """
    Classify order payment type and calculate amounts paid/outstanding.

    Args:
        order_data: Raw Shopify order data with financial_status, total_price,
                    and optionally total_outstanding.

    Returns:
        {
            "payment_type": "cod" | "prepaid" | "partial_prepaid",
            "total_price": Decimal,
            "amount_paid": Decimal,
            "amount_outstanding": Decimal
        }
    """
    financial_status = (order_data.get("financial_status") or "pending").lower()
    total_price = Decimal(str(order_data.get("total_price", "0")))

    if financial_status == "paid":
        return {
            "payment_type": "prepaid",
            "total_price": total_price,
            "amount_paid": total_price,
            "amount_outstanding": Decimal("0"),
        }

    if financial_status == "partially_paid":
        outstanding = Decimal(str(order_data.get("total_outstanding", "0")))
        return {
            "payment_type": "partial_prepaid",
            "total_price": total_price,
            "amount_paid": total_price - outstanding,
            "amount_outstanding": outstanding,
        }

    return {
        "payment_type": "cod",
        "total_price": total_price,
        "amount_paid": Decimal("0"),
        "amount_outstanding": total_price,
    }


# Known payment-gateway reference keys seen in Shopify order note_attributes,
# across the gateways used by our tenants. Matched case-insensitively; the
# original key name is preserved in the tag so the gateway + id type stay
# legible (e.g. "orig_PayU_txn_id:…", "orig_razorpay_order_id:…"). Stored
# lower-cased for matching.
_GATEWAY_PAYMENT_NOTE_KEYS = frozenset({
    "payu_txn_id",             # PayU — transaction id
    "razorpay_payment_id",     # Razorpay — payment id
    "razorpay_order_id",       # Razorpay — order id
    "cf_payment_id",           # Cashfree — payment id
    "cf_order_id",             # Cashfree — order id
    "transactionid",           # PhonePe — transaction id
    "phonepe_transaction_id",  # PhonePe — transaction id
    "merchanttransactionid",   # PhonePe — merchant transaction id
    "txnid",                   # Paytm — transaction id
    "orderid",                 # Paytm — order id
    "easebuzz_payment_id",     # Easebuzz — payment id
    "tracking_id",             # CCAvenue — tracking id
    "payment_id",              # Instamojo — payment id
})


def extract_gateway_payment_reference_tags(note_attributes: list) -> List[str]:
    """Tags for every known gateway payment-reference in ``note_attributes``.

    ``note_attributes`` are ``{"name": ..., "value": ...}`` dicts (Shopify's
    "Additional details"). For each entry whose key is a known gateway
    reference (``_GATEWAY_PAYMENT_NOTE_KEYS``), emit ``orig_<key>:<value>`` with
    the original key name preserved (e.g. ``orig_PayU_txn_id:29092610009``).
    Empty or comma-containing values are skipped (comma is the Shopify tag
    delimiter); duplicate tags are de-duplicated.

    Pure and stateless — inputs to outputs, no side effects.
    """
    tags: List[str] = []
    for attr in (note_attributes or []):
        if not isinstance(attr, dict):
            continue
        name = str(attr.get("name") or "").strip()
        if name.lower() not in _GATEWAY_PAYMENT_NOTE_KEYS:
            continue
        value = str(attr.get("value") or "").strip()
        if not value or "," in value:
            continue
        tag = f"{PAYMENT_REFERENCE_TAG_PREFIX}{name}:{value}"
        if tag not in tags:
            tags.append(tag)
    return tags


def build_payment_reference_tags(
    transactions: list,
    note_attributes: Optional[list] = None,
) -> List[str]:
    """Build tags pinning a clone back to the original order's captured payment.

    Returns:
      - ``orig_txn_id:<id>`` — the Shopify ``OrderTransaction.id`` of the parent
        sale/capture transaction (same predicate the refund flow uses).
      - one ``orig_<key>:<value>`` per known gateway payment reference found in
        the order's ``note_attributes`` (e.g. ``orig_PayU_txn_id:29092610009``).

    Each tag is omitted when its source is missing (COD orders have no
    successful transaction; many orders carry no gateway note_attribute), so
    callers can append the result unconditionally.

    Pure and stateless — inputs to outputs, no side effects.
    """
    tags: List[str] = []

    parent = next(
        (
            t for t in (transactions or [])
            if t.get("kind") in ("capture", "sale") and t.get("status") == "success"
        ),
        None,
    )
    if parent:
        txn_id = parent.get("id")
        if txn_id not in (None, ""):
            tags.append(f"{PAYMENT_TXN_TAG_PREFIX}{txn_id}")

    tags.extend(extract_gateway_payment_reference_tags(note_attributes))

    return tags


def create_order_info_dto(order_id: str, channel_order_id: str, status: str, partner_status: str, 
                         shipment_status: str, customer: str, delivery_date: Optional[str], 
                         out_for_delivery_date: Optional[str], items: List[str], courier: str, tracking_url: str, awb: Optional[str], 
                         products: List[str], created_at: str, updated_at: str, source: Optional[str] = None,
                         financial_status: Optional[str] = None, fulfillment_status: Optional[str] = None, 
                         total_price: Optional[str] = None, currency: Optional[str] = None,
                         line_items: Optional[List[Dict[str, Any]]] = None,
                         cancelled_at: Optional[str] = None,
                         customer_phone: Optional[str] = None,
                         billing_phone: Optional[str] = None,
                         delivered_date: Optional[str] = None,
                         tracking_company: Optional[str] = None) -> Dict[str, Any]:
    """Create order info using the DTO structure."""
    try:
        from fashion_bot.schema import OrderInfoDTO
        order_info: OrderInfoDTO = {
            "order_id": order_id,
            "channel_order_id": channel_order_id,
            "status": status,
            "partner_status": partner_status,
            "shipment_status": shipment_status,
            "customer": customer,
            "delivery_date": delivery_date,
            "delivered_date": delivered_date or delivery_date,  # Alias for delivery_date
            "out_for_delivery_date": out_for_delivery_date,
            "items": items,
            "courier": courier,
            "tracking_company": tracking_company or courier,
            "tracking_url": tracking_url,
            "awb": awb,
            "products": products,
            "created_at": created_at,
            "updated_at": updated_at,
            "financial_status": financial_status,
            "fulfillment_status": fulfillment_status,
            "total_price": total_price,
            "currency": currency,
            "line_items": line_items or [],
            "cancelled_at": cancelled_at,
            "customer_phone": customer_phone,
            "billing_phone": billing_phone or customer_phone
        }
        if source:
            order_info["source"] = source
        return order_info
    except ImportError:
        return {
            "order_id": order_id,
            "channel_order_id": channel_order_id,
            "status": status,
            "partner_status": partner_status,
            "shipment_status": shipment_status,
            "customer": customer,
            "delivery_date": delivery_date,
            "delivered_date": delivered_date or delivery_date,
            "out_for_delivery_date": out_for_delivery_date,
            "items": items,
            "courier": courier,
            "tracking_company": tracking_company or courier,
            "tracking_url": tracking_url,
            "awb": awb,
            "products": products,
            "created_at": created_at,
            "updated_at": updated_at,
            "source": source,
            "financial_status": financial_status,
            "fulfillment_status": fulfillment_status,
            "total_price": total_price,
            "currency": currency,
            "line_items": line_items or [],
            "cancelled_at": cancelled_at,
            "customer_phone": customer_phone,
            "billing_phone": billing_phone or customer_phone
        }

def get_pricing_formatter(vendor: str):
    """
    Factory function to get the pricing formatter for a specific vendor.
    
    Args:
        vendor: Vendor name (e.g., "shopify", "woocommerce")
    
    Returns:
        Formatter function that takes order_dto and returns pricing view dict
    """
    vendor_lower = vendor.lower()
    
    try:
        if vendor_lower == "shopify":
            from fashion_bot.shopify.formatters.pricing_formatter import format_pricing_view
            return format_pricing_view
        elif vendor_lower == "woocommerce":
            from fashion_bot.woocommerce.formatters.pricing_formatter import format_pricing_view
            return format_pricing_view
        else:
            # Fallback to default formatter
            return _default_pricing_formatter
    except ImportError:
        # If vendor formatter doesn't exist, use default
        return _default_pricing_formatter

def _default_pricing_formatter(order_dto: Dict[str, Any]) -> Dict[str, Any]:
    """
    Default pricing formatter for vendors without custom formatters.
    """
    return {
        "order_id": order_dto.get("order_id"),
        "financial_status": order_dto.get("financial_status", "unknown"),
        "created_at": order_dto.get("created_at", "Unknown"),
        "cancelled_at": order_dto.get("cancelled_at"),
        "items": order_dto.get("line_items", []),
        "currency": order_dto.get("currency", "INR"),
        "total_price": order_dto.get("total_price", 0),
        "customer_name": order_dto.get("customer", "Guest")
    }


# ---------------------------------------------------------------------------
# Pincode ↔ city/state validation
# ---------------------------------------------------------------------------

async def alookup_pincode_info(pin_code: str) -> Optional[Dict[str, str]]:
    """Look up the expected city (district) and state for an Indian PIN code.

    Uses the public ``api.postalpincode.in`` service. Returns
    ``{"city": "…", "state": "…"}`` on success, ``None`` on failure or
    when the PIN code is unrecognised.  Fail-open: a network/timeout error
    returns ``None`` so callers can proceed without blocking.
    """
    import time
    from fashion_bot.utils.http_client import get_shared_async_http_client

    if not pin_code or not pin_code.strip().isdigit() or len(pin_code.strip()) != 6:
        return None
    try:
        url = f"https://api.postalpincode.in/pincode/{pin_code.strip()}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        client = await get_shared_async_http_client()
        _t0 = time.monotonic()
        resp = await client.get(url, headers=headers, timeout=10)
        logger.info(
            "📮 Pincode lookup %s elapsed_ms=%d status=%s",
            pin_code, int((time.monotonic() - _t0) * 1000), resp.status_code,
        )
        data = resp.json()
        if (
            isinstance(data, list)
            and data
            and data[0].get("Status") == "Success"
            and data[0].get("PostOffice")
        ):
            po = data[0]["PostOffice"][0]
            return {
                "city": po.get("District", ""),
                "state": po.get("State", ""),
            }
    except Exception as exc:
        logger.debug("Pincode lookup failed for %s: %s", pin_code, exc)
    return None


async def acheck_pincode_city_state_match(
    pin_code: str,
    city: str = "",
    state: str = "",
) -> Dict[str, Any]:
    """Validate that the given city/state are consistent with the PIN code.

    Returns a dict with:
      ``match``  : True when consistent (or when the API is unavailable)
      ``expected``: ``{"city": …, "state": …}`` from the API (if available)
      ``message`` : human-readable mismatch description (only when not matched)

    Fail-open: when the postal API is unreachable, returns ``match=True``
    so the order flow is not blocked.
    """
    pin_info = await alookup_pincode_info(pin_code)
    if not pin_info or not pin_info.get("state"):
        return {"match": True, "expected": None}

    city_ok = (
        not city
        or city.strip().lower() == pin_info["city"].strip().lower()
    )
    state_ok = (
        not state
        or state.strip().lower() == pin_info["state"].strip().lower()
    )

    if city_ok and state_ok:
        return {"match": True, "expected": pin_info}

    return {
        "match": False,
        "expected": pin_info,
        "message": (
            f"Could you please confirm that the city '{city or '(not provided)'}' "
            f"and state '{state or '(not provided)'}' are correct for your shipping address?"
        ),
    }


# ── BLOOMERCE_EDITED note + tag ──────────────────────────────────────────────

def _extract_customer_name(order_data: Optional[Dict]) -> str:
    if not order_data:
        return ""
    customer = order_data.get("customer") or {}
    shipping = order_data.get("shipping_address") or {}
    first = shipping.get("first_name") or customer.get("first_name") or ""
    last = shipping.get("last_name") or customer.get("last_name") or ""
    return f"{first} {last}".strip()


def _extract_order_status(order_data: Optional[Dict]) -> str:
    if not order_data:
        return ""
    if order_data.get("cancelled_at"):
        return "cancelled"
    return order_data.get("fulfillment_status") or "unfulfilled"


def build_bloomerce_edited_note(
    update_type: str,
    state: Optional[Dict] = None,
    order_data: Optional[Dict] = None,
) -> str:
    """Build a structured Shopify order note for BLOOMERCE_EDITED orders.

    Pure function — no side effects. The caller is responsible for writing
    the note via ``order_service.aadd_order_note``.
    """
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    conversation_id = (state or {}).get("conversation_id") or ""
    phone_number = (state or {}).get("phone_number") or ""
    session_id = (state or {}).get("session_id") or ""
    customer_name = _extract_customer_name(order_data)
    order_status = _extract_order_status(order_data)
    order_name = (order_data or {}).get("name") or ""

    lines = [f"[Bloomerce Edit] {now}"]
    if order_name:
        lines.append(f"Order: {order_name}")
    if customer_name:
        lines.append(f"Customer: {customer_name}")
    if order_status:
        lines.append(f"Status: {order_status}")
    lines.append(f"Updated: {update_type}")
    if conversation_id:
        lines.append(f"Conversation: {conversation_id}")
    if phone_number:
        lines.append(f"Phone: {phone_number}")
    if session_id:
        lines.append(f"Session: {session_id}")

    return "\n".join(lines)


async def astamp_bloomerce_edited(
    order_service: Any,
    order_id: str,
    update_type: str,
    state: Optional[Dict] = None,
    order_data: Optional[Dict] = None,
    extra_tags: Optional[List[str]] = None,
) -> None:
    """Add the BLOOMERCE_EDITED tag and a structured note to a Shopify order.

    Fail-open: exceptions are logged as warnings, never propagated. The
    caller's update flow must not break because of a tagging failure.

    Args:
        order_service: Shopify order adapter (must have ``aadd_order_tags``
            and ``aadd_order_note``).
        order_id: Shopify order identifier.
        update_type: Human-readable label for the column being changed
            (e.g. ``"address"``, ``"size"``, ``"product"``).
        state: LangGraph state dict (carries conversation_id, phone_number,
            session_id).
        order_data: Raw Shopify order dict (for customer name, status).
        extra_tags: Additional tags to stamp alongside BLOOMERCE_EDITED
            (e.g. ``[OrderTag.BLOOMERCE_UPDATED]`` for clone flows).
    """
    try:
        tags = list(extra_tags or [])
        if OrderTag.BLOOMERCE_EDITED not in tags:
            tags.append(OrderTag.BLOOMERCE_EDITED)
        await order_service.aadd_order_tags(
            order_id, tags, state=state, order_record=order_data,
        )
    except Exception:
        logger.warning("Failed to add BLOOMERCE_EDITED tag to %s", order_id)

    try:
        note = build_bloomerce_edited_note(update_type, state=state, order_data=order_data)
        await order_service.aadd_order_note(order_id, note, state=state)
    except Exception:
        logger.warning("Failed to add BLOOMERCE_EDITED note to %s", order_id)


async def aadd_escalation_order_note(
    order_service: Any,
    order_id: str,
    note: str,
    state: Optional[Dict] = None,
) -> bool:
    """Write an ESCALATION-path note onto the order, unless the client opted out.

    Every note that accompanies a hand-off to a human goes through here — the
    ``[Bloomerce] … Requires manual intervention`` notes on the non-integrated
    cancel/update branches, the ``[Bot - FAILED] …`` note before a tool-failure
    escalation, and the return-partner automation-failure notes. A client that
    does not want its escalations stamped onto the order sets
    ``escalation_policy.order_notes_enabled = false`` and every one of them
    becomes a no-op, with no other behaviour changed: the escalation is still
    raised, logged and notified.

    Notes that are NOT escalations keep writing unconditionally — a deliberate
    ``annotate_order`` call and the BLOOMERCE_EDITED stamp on a successful edit
    do not route through this helper.

    Fail-open on config (a config read error still writes the note, preserving
    the audit trail) and fail-soft on the write itself, so a note failure can
    never break the escalation that follows it.

    Returns:
        ``True`` if the note was written, ``False`` if it was suppressed by
        config or the write failed.
    """
    from fashion_bot.agent_config import aescalation_order_notes_enabled
    from fashion_bot.utils.utils import log_with_trace_id

    client_id = (state or {}).get("client_id")
    if not await aescalation_order_notes_enabled(client_id):
        log_with_trace_id(
            state,
            f"🔕 [ESCALATION] order note suppressed for {order_id} "
            f"(escalation_policy.order_notes_enabled=false)",
        )
        return False

    try:
        await order_service.aadd_order_note(order_id, note, state=state)
        return True
    except Exception as exc:
        log_with_trace_id(
            state, f"Failed to add escalation note to {order_id}: {exc}", "warning"
        )
        return False
