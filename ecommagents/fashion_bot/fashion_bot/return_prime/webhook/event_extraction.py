"""Derive the RETURN_INITIATED / EXCHANGE_INITIATED / REFUND_INITIATED event
key (and WhatsApp template params) from a raw Return Prime webhook payload.

Return Prime's webhook body nests everything under ``request`` (see the
sample payload in the design discussion): ``request.order``,
``request.customer``, ``request.line_items[0].refund``, etc. Detection rule
(confirmed with the client):

* ``request.status == "requested"`` is the request's creation moment —
  ``EXCHANGE_INITIATED`` when it's an exchange (``request_type == "exchange"``
  or ``smart_exchange`` is true), otherwise ``RETURN_INITIATED``.
* Once status has moved past "requested", a line item's ``refund.status``
  leaving/skipping "pending" (money actually starting to move) fires
  ``REFUND_INITIATED`` exactly once — the notification claim table
  (``return_prime_event_notifications``, unique on client/request/event_key)
  guarantees a single send even though this condition can stay true across
  several subsequent webhook deliveries for the same request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

RETURN_INITIATED = "RETURN_INITIATED"
EXCHANGE_INITIATED = "EXCHANGE_INITIATED"
REFUND_INITIATED = "REFUND_INITIATED"


def _coerce_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


@dataclass
class ReturnPrimeLineItem:
    title: Optional[str]
    sku: Optional[str]
    quantity: Optional[Any]
    reason: Optional[str]
    notes: Optional[str]


@dataclass
class ReturnPrimeEvent:
    event_key: str
    request_id: Optional[str]
    request_number: Optional[str]
    order_name: Optional[str]
    phone: Optional[str]
    email: Optional[str]
    shopify_order_id: Optional[str] = None
    product_id: Optional[Any] = None
    variant_id: Optional[Any] = None
    customer_full_name: Optional[str] = None
    order_created_at: Optional[str] = None
    requested_on: Optional[str] = None
    line_items: List[ReturnPrimeLineItem] = field(default_factory=list)
    context: Dict[str, str] = field(default_factory=dict)


def _first_refundable_amount(first_item: dict) -> Optional[Any]:
    refund = _coerce_dict(first_item.get("refund"))
    shop_money = _coerce_dict(_coerce_dict(refund.get("refunded_amount")).get("shop_money"))
    amount = shop_money.get("amount")
    if amount not in (None, ""):
        return amount
    return _coerce_dict(first_item.get("shop_price")).get("actual_amount")


def extract_return_prime_event(payload: Any) -> Optional[ReturnPrimeEvent]:
    body = _coerce_dict(payload)
    request = _coerce_dict(body.get("request")) or _coerce_dict(body.get("data")) or body

    order = _coerce_dict(request.get("order"))
    customer = _coerce_dict(request.get("customer"))
    line_items = request.get("line_items") if isinstance(request.get("line_items"), list) else []
    first_item = _coerce_dict(line_items[0]) if line_items else {}
    refund = _coerce_dict(first_item.get("refund"))

    status = str(request.get("status") or "").strip().lower()
    request_type = str(request.get("request_type") or "").strip().lower()
    smart_exchange = bool(request.get("smart_exchange"))
    refund_status = str(refund.get("status") or "").strip().lower()

    event_key: Optional[str] = None
    if status == "requested":
        if smart_exchange or request_type == "exchange":
            event_key = EXCHANGE_INITIATED
        elif request_type == "return":
            event_key = RETURN_INITIATED
    elif refund_status and refund_status != "pending":
        event_key = REFUND_INITIATED

    if not event_key:
        return None

    request_id = request.get("id")
    if request_id is not None:
        request_id = str(request_id)

    order_name = order.get("name")
    if not order_name and order.get("id") is not None:
        order_name = str(order.get("id"))

    customer_name = str(customer.get("name") or "").strip() or "Customer"
    customer_first_name = customer_name.split()[0] if customer_name else "Customer"

    if event_key == EXCHANGE_INITIATED:
        request_type_word = "Exchange"
    elif event_key == REFUND_INITIATED:
        request_type_word = "Refund"
    else:
        request_type_word = "Return"

    refund_amount = _first_refundable_amount(first_item) if event_key == REFUND_INITIATED else None

    context = {
        "customer_name": customer_first_name,
        "order_id": order_name or "",
        "request_type": request_type_word,
        "refund_amount": str(refund_amount) if refund_amount not in (None, "") else "",
    }

    # The item being returned/exchanged — used to fetch the *current* Shopify
    # product image at send time (product photos can change after the return
    # was filed), rather than trusting whatever was embedded in the webhook.
    original_product = _coerce_dict(first_item.get("original_product"))

    shopify_order_id = order.get("id")
    if shopify_order_id is not None:
        shopify_order_id = str(shopify_order_id)

    line_item_details = []
    for raw_item in line_items:
        item = _coerce_dict(raw_item)
        product = _coerce_dict(item.get("original_product"))
        line_item_details.append(
            ReturnPrimeLineItem(
                title=product.get("title"),
                sku=product.get("sku"),
                quantity=item.get("quantity"),
                reason=item.get("reason"),
                notes=item.get("notes"),
            )
        )

    return ReturnPrimeEvent(
        event_key=event_key,
        request_id=request_id,
        request_number=request.get("request_number"),
        order_name=order_name,
        phone=customer.get("phone") or None,
        email=customer.get("email") or None,
        shopify_order_id=shopify_order_id,
        product_id=original_product.get("product_id"),
        variant_id=original_product.get("variant_id"),
        customer_full_name=customer_name,
        order_created_at=order.get("created_at"),
        requested_on=request.get("created_at"),
        line_items=line_item_details,
        context=context,
    )
