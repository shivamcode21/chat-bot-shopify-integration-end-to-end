"""
Delhivery-side helpers for the Shopify cancel-and-recreate flow.

We never *create* shipments on Delhivery directly. Order creation is Shopify's
job — Shopify's Delhivery integration auto-syncs new/edited orders to
Delhivery, where a fresh AWB is generated. Our only responsibility on
Delhivery's side is to **cancel** the stale AWB so the customer doesn't
receive the pre-edit package.

The function is named ``aupdate_delhivery_order_size`` to mirror
``shopify/modules/order_editing_graphql.py::_aupdate_shiprocket_order_size``,
but the implementation is a thin cancel — the recreate step is left to
Shopify→Delhivery sync.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def aupdate_delhivery_order_size(
    order_id: str,
    shopify_order_data: dict,  # kept for signature parity with the Shiprocket helper
    old_size: str,
    new_size: str,
    state: Optional[dict] = None,
) -> Dict[str, Any]:
    """
    Cancel the existing Delhivery AWB after a Shopify-side size change.

    Steps:
      1. Look up the AWB + current status via DelhiveryLogisticsAdapter.
      2. If status is in the mutable set (Manifested/Pending/Open/Scheduled),
         cancel the AWB on Delhivery.
      3. Trust Shopify→Delhivery auto-sync to create the new shipment with
         the updated SKU. We do not create on Delhivery directly.

    Returns a dict shaped like the Shiprocket helper so the caller in
    ``order_editing_graphql.py`` can render its response uniformly.
    """
    from fashion_bot.delhivery.tools.logistics_adapter import (
        DelhiveryLogisticsAdapter,
    )

    cid = (state or {}).get("client_id")
    logistics = DelhiveryLogisticsAdapter(client_id=cid)

    current = await logistics.aget_order_data(order_id, state=state)
    if not current.get("found"):
        return {
            "success": False,
            "skipped": True,
            "error": "Order not found in Delhivery",
            "size_change": f"{old_size} -> {new_size}",
        }

    current_status = (current.get("status") or "").upper()
    mutable = {"MANIFESTED", "PENDING", "OPEN", "SCHEDULED"}
    if current_status not in mutable:
        return {
            "success": False,
            "skipped": True,
            "error": (
                f"Delhivery cannot cancel AWB in status '{current_status}' "
                "for size-change re-sync. Manual handling required."
            ),
            "size_change": f"{old_size} -> {new_size}",
        }

    cancel_result = await logistics.acancel_shipment(order_id, state=state)
    if not cancel_result.get("success"):
        return {
            "success": False,
            "error": f"Delhivery cancel failed: {cancel_result.get('error')}",
            "size_change": f"{old_size} -> {new_size}",
        }

    # We cancelled the stale AWB but we do NOT recreate it ourselves —
    # Shopify→Delhivery sync is supposed to mint a fresh AWB once the edit
    # propagates. If that sync silently drops (mis-config, paused integration,
    # SLA breach), the order ends up "cancelled on Delhivery, never recreated"
    # with no human signal. Flag the caller so the orchestrator can schedule a
    # verification check or surface to ops via the standard escalation path.
    return {
        "success": True,
        "message": (
            "Delhivery AWB cancelled. New shipment will be auto-created by "
            "Shopify→Delhivery sync once the edit propagates."
        ),
        "old_awb": cancel_result.get("awb"),
        "new_awb": None,  # populated by Shopify auto-sync, not by us
        "size_change": f"{old_size} -> {new_size}",
        "requires_verification": True,
        "verification_reason": (
            "AWB cancelled on Delhivery; Shopify→Delhivery sync must create a "
            "new AWB. Verify the new AWB exists before considering this complete."
        ),
    }
