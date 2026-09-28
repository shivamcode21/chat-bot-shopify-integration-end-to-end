"""
Canonical logistics-status resolver.

Why this exists
---------------
The `order_status_handler` agent prompt selects a customer-facing response
template by switching on the `logistics_status` field returned by the order
tools. That field flows into the LLM from two distinct code paths:

  * `tool_factory.get_order_details_tool` — enriches the Shopify order with
    a `logistics_data.status` value pulled from the logistics partner
    (Shiprocket / Delhivery / ...).
  * `tool_factory._create_get_recent_orders_tool` — returns a list of orders
    from Shopify only; historically did NOT include `logistics_status`.

Two production bugs were caused by the absence of canonical normalization:

  Bug A — stale partner status overrides reality
    Shiprocket sometimes returns a pre-dispatch status (e.g. "PICKUP
    EXCEPTION", "PICKUP RESCHEDULED") for an order that has already been
    picked up and is in transit. Shopify's carrier-driven
    `fulfillment.shipment_status` correctly reports `in_transit`, but the
    raw partner string wins and the agent fires Rule 3 ("packed and ready,
    dispatch tomorrow") instead of Rule 4 ("already dispatched").

  Bug B — recent-orders path has no logistics_status at all
    When the agent calls `get_recent_orders` (e.g. customer asks "track my
    order" without an order ID), the tool output has `shipment_status` but
    no `logistics_status`. None of the prompt's Rules 1-6 match, so the
    agent improvises and frequently falls back to the same "packed and
    ready" wording.

Both bugs share a fix: compute a canonical `logistics_status` from all
available signals, and write it into both tool outputs. The carrier-reported
Shopify `shipment_status` is the most reliable signal — if the carrier has
acknowledged the parcel moving, that beats whatever stale value the partner
API may return.

Why this is NOT what PR #654 proposed
-------------------------------------
PR #654 added a helper at `fashion_bot/fashion_bot/fixes/logistics_status_validation.py`.
That helper had three independent problems:

  1. Dead code — the new file was never imported anywhere, so merging it
     changed zero runtime behavior.
  2. Case mismatch — the helper compared the partner status against a
     Pascal-case set ({"Pickup Rescheduled", ...}), but the Shiprocket
     adapter at `shiprocket/tools/logistics_adapter.py:192` uppercases
     before returning (`(detail_data.get("status") or "").upper()`), so the
     set membership check would always miss in production.
  3. Wrong signal — the helper used `tracking_url present` and
     `fulfillment_status == "fulfilled"` as the override trigger. In
     Shopify, both of those are set at label-print time, not at pickup
     time, so the override would happily lie in the opposite direction
     ("In Transit") for orders that are genuinely stuck at the warehouse.

This resolver uses Shopify's carrier-driven `fulfillment.shipment_status`
instead — the only signal that actually reports parcel movement.
"""

from __future__ import annotations

from typing import Optional


# ── Canonical category strings ─────────────────────────────────────────────
# These MUST match the strings the order_status_handler prompt switches on.
# Changing these requires a paired prompt update.

NEW = "New"
READY_TO_SHIP = "Ready to Ship"
PICKUP_SCHEDULED = "Pickup Scheduled"
OUT_FOR_PICKUP = "Out for Pickup"
PICKUP_ERROR = "Pickup Error"
PICKUP_EXCEPTION = "Pickup Exception"
PICKUP_RESCHEDULED = "Pickup Rescheduled"
DELAYED = "Delayed"
PICKED_UP = "Picked Up"
IN_TRANSIT = "In Transit"
REACHED_DESTINATION_HUB = "Reached Destination Hub"
OUT_FOR_DELIVERY = "Out for Delivery"
MISROUTED = "Misrouted"
DELIVERED = "Delivered"


# ── Partner-status → canonical map ─────────────────────────────────────────
# Keyed by the UPPERCASE form because the Shiprocket adapter normalizes
# with .upper() before returning. Keep both pre-dispatch and post-dispatch
# variants the partner can emit.
_RAW_PARTNER_MAP = {
    "NEW": NEW,
    "READY TO SHIP": READY_TO_SHIP,
    "PICKUP SCHEDULED": PICKUP_SCHEDULED,
    "OUT FOR PICKUP": OUT_FOR_PICKUP,
    "PICKUP ERROR": PICKUP_ERROR,
    "PICKUP EXCEPTION": PICKUP_EXCEPTION,
    "PICKUP RESCHEDULED": PICKUP_RESCHEDULED,
    "DELAYED": DELAYED,
    "PICKED UP": PICKED_UP,
    "IN TRANSIT": IN_TRANSIT,
    "INTRANSIT": IN_TRANSIT,
    "IN-TRANSIT": IN_TRANSIT,
    "REACHED DESTINATION HUB": REACHED_DESTINATION_HUB,
    "OUT FOR DELIVERY": OUT_FOR_DELIVERY,
    "OUT FOR DELIVERY (OFD)": OUT_FOR_DELIVERY,
    "OFD": OUT_FOR_DELIVERY,
    "MISROUTED": MISROUTED,
    "DELIVERED": DELIVERED,
}

# Pre-dispatch raw statuses that should be OVERRIDDEN to "In Transit"
# when the carrier-driven Shopify shipment_status confirms movement.
_STALE_PRE_DISPATCH = frozenset({
    PICKUP_ERROR,
    PICKUP_EXCEPTION,
    PICKUP_RESCHEDULED,
    DELAYED,
    NEW,
    READY_TO_SHIP,
    PICKUP_SCHEDULED,
    OUT_FOR_PICKUP,
})


# ── Shopify shipment_status → canonical map ────────────────────────────────
# This field is populated by the carrier via webhook. When present it is the
# strongest signal we have for actual parcel state. Keyed by snake_case
# lower form which is how Shopify emits it.
_SHIPMENT_STATUS_MAP = {
    "delivered": DELIVERED,
    "out_for_delivery": OUT_FOR_DELIVERY,
    "attempted_delivery": OUT_FOR_DELIVERY,
    "in_transit": IN_TRANSIT,
    "ready_for_pickup": REACHED_DESTINATION_HUB,
}


def resolve_logistics_status(
    raw_partner_status: Optional[str],
    shipment_status: Optional[str] = None,
    cancelled_at: Optional[str] = None,
) -> Optional[str]:
    """
    Return the canonical `logistics_status` to surface to the agent.

    Resolution order (first match wins):

    1. Cancelled orders → return None (caller must use cancellation
       wording, not the LOGISTICS STATUS-BASED RESPONSE RULES).
    2. Shopify `fulfillment.shipment_status` if known to us. This is the
       strongest signal because it is populated by the carrier via webhook
       and reflects actual parcel state.
    3. Partner-reported `raw_partner_status` mapped through
       `_RAW_PARTNER_MAP`. Inputs are uppercased before lookup so that
       both "PICKUP EXCEPTION" (Shiprocket's actual emission) and
       "Pickup Exception" map correctly.
    4. None if no signal recognised — caller should not invoke the
       status-based response rules and should fall back to neutral
       wording.

    Staleness guard
    ---------------
    If the partner says one of `_STALE_PRE_DISPATCH` but the carrier-driven
    `shipment_status` confirms movement (`in_transit` / `out_for_delivery`
    / `delivered`), step 2 above already wins, so the stale partner string
    never reaches the LLM.

    Parameters
    ----------
    raw_partner_status:
        Status string from the logistics partner (Shiprocket etc.). May be
        upper, lower, or mixed case. May be None.
    shipment_status:
        Shopify `fulfillment.shipment_status` (e.g. "in_transit",
        "delivered"). May be None when the order has no fulfillment or
        the carrier has not reported yet.
    cancelled_at:
        Shopify `cancelled_at` timestamp. Any truthy value means the order
        is cancelled.

    Returns
    -------
    Optional[str]
        One of the canonical strings exported from this module, or None.
    """
    if cancelled_at:
        return None

    # Strongest signal: carrier-reported shipment_status from Shopify
    norm_ship = (shipment_status or "").strip().lower()
    if norm_ship in _SHIPMENT_STATUS_MAP:
        return _SHIPMENT_STATUS_MAP[norm_ship]

    # Fall back to the partner-reported value, case-normalised.
    norm_raw = (raw_partner_status or "").strip().upper()
    if norm_raw in _RAW_PARTNER_MAP:
        return _RAW_PARTNER_MAP[norm_raw]

    return None
