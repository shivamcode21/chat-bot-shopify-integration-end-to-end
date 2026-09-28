"""Authoritative delivery timestamp lookup from courier tracking history.

Shopify/Shiprocket order and fulfillment fields (``updated_at``,
``closed_at``) are last-modified timestamps, not delivery timestamps — they
drift forward on any later write to the order or fulfillment record, long
after the real delivery event. ``shipment_status_history`` is the raw
courier tracking timeline (populated by the Shiprocket/Delhivery webhook
processors) and is the only place a genuine "when was this actually
delivered" timestamp lives, so it should be preferred over any Shopify
field wherever a delivery date drives a return/exchange window check.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from fashion_bot.database_manager import get_async_postgres_connection
from fashion_bot.utils.utils import log_with_trace_id


def _order_id_variants(order_number: str) -> list[str]:
    """Literal order_id strings to try, matching how Shopify order names
    (e.g. "#gv16496") land in shipment_status_history verbatim from the
    courier webhook payload — case and "#" prefix as originally sent."""
    stripped = str(order_number or "").strip().lstrip("#").strip()
    if not stripped:
        return []
    variants = {stripped, f"#{stripped}", stripped.lower(), f"#{stripped.lower()}",
                stripped.upper(), f"#{stripped.upper()}"}
    return sorted(variants)


async def aget_confirmed_delivery_datetime(
    client_id: str,
    order_number: str,
    state: Optional[dict] = None,
) -> Optional[datetime]:
    """Earliest confirmed "delivered" timestamp for an order, sourced from
    courier tracking history (``shipment_status_history``).

    Returns ``None`` if the client/order has no such event recorded (e.g. no
    logistics partner integration, or the webhook history predates
    tracking), so callers should fall back to their own heuristics rather
    than treating ``None`` as "not delivered".
    """
    variants = _order_id_variants(order_number)
    if not client_id or not variants:
        return None

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                # order_id = ANY(%s) with an exact-match candidate list uses
                # the (client_id, order_id) index — do NOT wrap order_id in
                # a function (e.g. regexp_replace/upper) here, that forces a
                # full scan of this multi-GB table and times out.
                await cur.execute(
                    """
                    SELECT MIN(timestamp) AS delivered_at
                    FROM shipment_status_history
                    WHERE client_id = %s
                      AND order_id = ANY(%s)
                      AND upper(status) = 'DELIVERED'
                    """,
                    (str(client_id), variants),
                )
                row = await cur.fetchone()
    except Exception as exc:
        log_with_trace_id(
            state,
            f"[DELIVERY_TIMELINE] lookup failed client={client_id} order={order_number}: {exc}",
            "warning",
        )
        return None

    if not row:
        return None
    value = row.get("delivered_at") if isinstance(row, dict) else row[0]
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
