"""Shopify order lookup helpers for Return Prime workflows."""

from __future__ import annotations

from typing import Any


async def fetch_order_by_name(client_id: str, order_name: str) -> dict[str, Any]:
    from fashion_bot.shopify.tools.order_adapter import ShopifyOrderAdapter

    adapter = await ShopifyOrderAdapter.create(client_id=client_id)
    order = await adapter._aresolve_order_record(order_name, state={"client_id": client_id})
    if not order:
        return {}

    customer = order.get("customer") or {}
    shipping = order.get("shipping_address") or {}
    return {
        "customer_email": customer.get("email") or order.get("email"),
        "customer_phone": customer.get("phone") or shipping.get("phone") or order.get("phone"),
    }
