"""
Logistics Status Validation Fix
================================
File: fashion_bot/fashion_bot/fixes/logistics_status_validation.py

Root Cause
----------
When tool_factory.py fetches order details, it calls:
    result["logistics_status"] = logistics_data.get("status")

The `logistics_data.get("status")` value comes from the Shiprocket/logistics
partner API. In some cases this value is stale (e.g., "Pickup Rescheduled",
"Delayed") even though the order already has a tracking URL and is "In Transit"
according to Shopify's fulfillment status.

The order_status_handler agent uses `logistics_status` to select the response
message template. When the stale status is "Pickup Rescheduled" or "Delayed",
the agent uses this template:
    "Your order is packed and ready to be shipped. The expected delivery
    time is 3-5 days. We will dispatch the order tomorrow."

But the order is already IN TRANSIT with a tracking URL — so the customer
receives factually incorrect information.

Example (message_id: f9b7a7ae-a7ea-4f44-84f2-a5adb486ffca):
  Order #gv15094 | Status: In Transit | Tracking URL: present
  Agent said: "Your order is packed and ready to be shipped..."  ← WRONG

Fix
---
In tool_factory.py, replace:

    result["logistics_status"] = logistics_data.get("status")

With:

    result["logistics_status"] = resolve_logistics_status(
        raw_status=logistics_data.get("status"),
        tracking_url=(
            result.get("tracking", {}).get("tracking_url")
            or result.get("tracking_url")
        ),
        fulfillment_status=result.get("fulfillment_status", ""),
    )

The helper function (see below) ensures that if a tracking URL is present,
a stale pre-dispatch logistics_status is overridden to "In Transit".
"""

# ── Pre-dispatch statuses that may lag behind actual shipment ──────────────
_PRE_DISPATCH_STATUSES = frozenset({
    "Pickup Rescheduled",
    "Pickup Error",
    "Pickup Exception",
    "Delayed",
    "Pickup Scheduled",
    "Out for Pickup",
    "Ready to Ship",
    "New",
})

# ── Fulfillment statuses that indicate the order is already moving ─────────
_IN_TRANSIT_FULFILLMENT_KEYWORDS = frozenset({
    "in transit",
    "intransit",
    "shipped",
    "fulfilled",
    "out_for_delivery",
    "out for delivery",
})


def resolve_logistics_status(
    raw_status: str | None,
    tracking_url: str | None,
    fulfillment_status: str | None = None,
) -> str | None:
    """
    Return the canonical logistics_status to expose to the agent.

    If the raw status from the logistics API is a pre-dispatch value
    but evidence suggests the order has already been picked up
    (tracking URL present, or fulfillment_status indicates in-transit),
    return "In Transit" instead of the stale raw_status.

    Parameters
    ----------
    raw_status:
        The status string returned by logistics_data.get("status").
    tracking_url:
        The tracking URL from result["tracking"]["tracking_url"],
        or None if not yet assigned.
    fulfillment_status:
        The Shopify fulfillment_status field (e.g., "in_transit",
        "fulfilled"), or None.

    Returns
    -------
    str | None
        The resolved (possibly corrected) logistics status.
    """
    if raw_status is None:
        return raw_status

    # Only override pre-dispatch statuses — leave everything else alone
    if raw_status not in _PRE_DISPATCH_STATUSES:
        return raw_status

    # If there's a tracking URL, the parcel has already left the warehouse
    if tracking_url:
        return "In Transit"

    # If Shopify fulfillment_status says the item is moving, trust it
    if fulfillment_status:
        normalized = fulfillment_status.lower().replace("_", " ")
        if any(kw in normalized for kw in _IN_TRANSIT_FULFILLMENT_KEYWORDS):
            return "In Transit"

    return raw_status


# ── Where to apply this fix in tool_factory.py ────────────────────────────
#
# Search for the block that currently reads:
#
#     result["logistics_status"] = logistics_data.get("status")
#     result["logistics_order_id"] = logistics_data.get("logistics_order_id")
#
# Replace the first line with:
#
#     from fashion_bot.fixes.logistics_status_validation import resolve_logistics_status
#
#     result["logistics_status"] = resolve_logistics_status(
#         raw_status=logistics_data.get("status"),
#         tracking_url=(
#             result.get("tracking", {}).get("tracking_url")
#             or result.get("tracking_url")
#         ),
#         fulfillment_status=result.get("fulfillment_status"),
#     )
#
# No other changes are required.
