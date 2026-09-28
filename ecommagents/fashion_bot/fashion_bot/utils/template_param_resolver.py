"""Resolve configured WhatsApp template param labels into ordered values."""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union


_PARAM_KEY_ALIASES = {
    "customername": "customer_name",
    "customer_name": "customer_name",
    "customerfirstname": "customer_name",
    "customer_firstname": "customer_name",
    "firstname": "customer_name",
    "first_name": "customer_name",
    "name": "customer_name",
    "orderid": "order_id",
    "order_id": "order_id",
    "ordernumber": "order_id",
    "order_number": "order_id",
    "trackinglink": "tracking_link",
    "tracking_link": "tracking_link",
    "trackingurl": "tracking_link",
    "tracking_url": "tracking_link",
    "ordertrackinglink": "tracking_link",
    "ordervalue": "order_value",
    "order_value": "order_value",
    "totalprice": "order_value",
    "total_price": "order_value",
    "price": "order_value",
    "amount": "order_value",
    "productdetails": "product_details",
    "product_details": "product_details",
    "deliveryaddress": "delivery_address",
    "delivery_address": "delivery_address",
    "trackingnumber": "tracking_number",
    "tracking_number": "tracking_number",
    "awb": "tracking_number",
    "couriername": "courier_name",
    "courier_name": "courier_name",
    "carrier": "courier_name",
    "abandonedcheckouturl": "abandoned_checkout_url",
    "abandoned_checkout_url": "abandoned_checkout_url",
    "checkouturl": "abandoned_checkout_url",
    "checkout_url": "abandoned_checkout_url",
    "producturl": "product_url",
    "product_url": "product_url",
    "itemname": "item_name",
    "item_name": "item_name",
    "productname": "item_name",
    "product_name": "item_name",
    "title": "item_name",
    "carttoken": "cart_token",
    "cart_token": "cart_token",
    "checkouttoken": "cart_token",
    "checkout_token": "cart_token",
    "cartid": "cart_id",
    "cart_id": "cart_id",
    "checkoutid": "cart_id",
    "checkout_id": "cart_id",
    "discount": "discount",
    "discount_code": "discount",
    "discount_amount": "discount",
    "discountcode": "discount",
    "discountamount": "discount",
    "requesttype": "request_type",
    "request_type": "request_type",
    "returnorexchange": "request_type",
    "return_or_exchange": "request_type",
    "refundamount": "refund_amount",
    "refund_amount": "refund_amount",
    "refundedamount": "refund_amount",
}

_CANONICAL_DYNAMIC_KEYS = frozenset(_PARAM_KEY_ALIASES.values())

ParamValue = Union[Any, Callable[[], Any]]


def normalize_template_param_key(key: Any) -> str:
    """Normalize finite template variable labels to stable lookup keys."""
    raw = re.sub(r"[^a-z0-9]+", "_", str(key or "").strip().lower()).strip("_")
    compact = raw.replace("_", "")
    return _PARAM_KEY_ALIASES.get(raw) or _PARAM_KEY_ALIASES.get(compact) or raw


def normalize_template_param_order(param_order: Optional[Sequence[Any]]) -> List[Any]:
    """Normalize DB/API param order into individual labels.

    ``gupshup_templates.param_order`` is commonly stored as comma-separated
    text, e.g. ``Customer Name,Order ID`` or ``Customer Name, Order ID``.
    """
    if not param_order:
        return []
    if isinstance(param_order, str):
        return [part.strip() for part in param_order.split(",") if part.strip()]
    return list(param_order)


def resolve_template_params(
    values: Optional[Union[Sequence[Any], Mapping[str, Any]]],
    param_order: Optional[Sequence[Any]] = None,
) -> List[str]:
    """Return template values in the exact order used for var1..varN."""
    if not values:
        return []
    if isinstance(values, (str, bytes)):
        return [str(values)]
    if not isinstance(values, Mapping):
        return [str(value) for value in values]

    normalized_values = {
        normalize_template_param_key(key): value for key, value in values.items()
    }
    ordered_keys = normalize_template_param_order(param_order) or list(values.keys())
    resolved: List[str] = []
    missing: List[str] = []
    for key in ordered_keys:
        normalized_key = normalize_template_param_key(key)
        value = normalized_values.get(normalized_key)
        if value is None:
            if normalized_key in _CANONICAL_DYNAMIC_KEYS:
                resolved.append("")
                continue
            missing.append(str(key))
            continue
        resolved.append(str(value))
    if missing:
        raise ValueError(f"missing template params: {', '.join(missing)}")
    return resolved


def build_template_params_from_context(
    param_order: Optional[Sequence[Any]],
    context: Mapping[str, ParamValue],
) -> List[str]:
    """Resolve a template row's param_order against webhook/order context.

    Known dynamic placeholders missing from ``context`` resolve to an empty
    string (legacy parity). Unrecognized labels remain static strings so DB
    ``param_order`` can include literal text (e.g. ``10% OFF``).
    """
    params: List[str] = []
    for label in normalize_template_param_order(param_order):
        key = normalize_template_param_key(label)
        value = context.get(key)
        if callable(value):
            value = value()
        if value is not None:
            params.append(str(value))
        elif key in _CANONICAL_DYNAMIC_KEYS:
            params.append("")
        else:
            params.append(str(label))
    return params
