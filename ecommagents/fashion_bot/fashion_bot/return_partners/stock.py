"""Stock checks used by return/exchange workflows."""

from __future__ import annotations

import re
from typing import Any

from fashion_bot.config_manager import aget_shopify_config
from fashion_bot.services.product_ingestion.shopify_product_service import variant_is_in_stock
from fashion_bot.utils.http_client import get_shared_async_http_client
from fashion_bot.utils.shopify_throttle import shopify_graphql_post
from fashion_bot.utils.utils import log_with_trace_id


def _variant_gid(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("gid://"):
        return text
    numeric = re.search(r"(\d+)$", text)
    if not numeric:
        return text
    return f"gid://shopify/ProductVariant/{numeric.group(1)}"


def _quantity(value: Any) -> int:
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def _inventory_quantity(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def acheck_shopify_variant_stock(
    *,
    client_id: str,
    line_items: list[dict],
    state: dict | None = None,
) -> dict:
    """Return whether all requested exchange variants are available in Shopify."""

    variant_ids = [
        _variant_gid(item.get("variant_id"))
        for item in line_items
        if isinstance(item, dict) and item.get("variant_id")
    ]
    variant_ids = list(dict.fromkeys(variant_ids))
    if not variant_ids:
        return {
            "success": False,
            "stock_available": None,
            "message": "Exchange variant data is missing, so stock could not be checked.",
            "variants": [],
        }

    config = await aget_shopify_config(client_id=client_id) or {}
    shop_url = config.get("shop_url")
    token = config.get("access_token")
    api_version = config.get("api_version") or "2025-07"
    if not shop_url or not token:
        return {
            "success": False,
            "stock_available": None,
            "message": (
                "Shopify configuration is missing, so exchange stock could not be checked."
            ),
            "variants": [],
        }

    query = """
    query ExchangeVariantStock($ids: [ID!]!) {
      nodes(ids: $ids) {
        ... on ProductVariant {
          id
          title
          sku
          inventoryQuantity
          inventoryPolicy
          product { id title }
        }
      }
    }
    """
    http = await get_shared_async_http_client()
    url = f"https://{shop_url}/admin/api/{api_version}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
    }

    try:
        payload = await shopify_graphql_post(
            http,
            url,
            headers,
            {"query": query, "variables": {"ids": variant_ids}},
            rate_limit_key=shop_url,
        )
    except Exception as exc:
        log_with_trace_id(state, f"[RETURN_PARTNERS] exchange stock check failed: {exc}", "warning")
        return {
            "success": False,
            "stock_available": None,
            "message": "Exchange stock could not be verified right now.",
            "error": str(exc),
            "variants": [],
        }

    nodes = payload.get("data", {}).get("nodes") or []
    by_id = {
        str(node.get("id")): node
        for node in nodes
        if isinstance(node, dict) and node.get("id")
    }
    requested_quantities = {
        _variant_gid(item.get("variant_id")): _quantity(item.get("quantity"))
        for item in line_items
        if isinstance(item, dict) and item.get("variant_id")
    }

    variants: list[dict] = []
    all_available = True
    unknown = False
    for variant_id in variant_ids:
        node = by_id.get(variant_id)
        qty_needed = requested_quantities.get(variant_id, 1)
        if not node:
            unknown = True
            all_available = False
            variants.append(
                {
                    "variant_id": variant_id,
                    "requested_quantity": qty_needed,
                    "available": None,
                    "reason": "variant_not_found",
                }
            )
            continue

        inventory_quantity = _inventory_quantity(node.get("inventoryQuantity"))
        inventory_policy = node.get("inventoryPolicy")
        purchasable = variant_is_in_stock(inventory_quantity, inventory_policy)
        policy_allows_oversell = str(inventory_policy or "").upper() == "CONTINUE"
        sufficient = purchasable and (policy_allows_oversell or inventory_quantity >= qty_needed)
        all_available = all_available and sufficient
        product = node.get("product") if isinstance(node.get("product"), dict) else {}
        variants.append(
            {
                "variant_id": variant_id,
                "title": node.get("title"),
                "sku": node.get("sku"),
                "product_title": product.get("title"),
                "requested_quantity": qty_needed,
                "inventory_quantity": inventory_quantity,
                "inventory_policy": inventory_policy,
                "available": sufficient,
            }
        )

    if unknown:
        message = "Exchange stock could not be fully verified because one or more variants were not found."
    elif all_available:
        message = "Requested exchange item stock is currently available."
    else:
        message = "Requested exchange item stock is not currently available."

    return {
        "success": True,
        "stock_available": all_available if not unknown else None,
        "message": message,
        "variants": variants,
        "raw": payload,
    }
