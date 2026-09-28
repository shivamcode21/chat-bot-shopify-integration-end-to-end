"""Async Shopify Admin GraphQL helpers for native returns.

Financial refund execution is intentionally excluded from this module. These
helpers are for return visibility/creation only and are consumed through the
generic return partner layer.
"""

from __future__ import annotations

import re
from typing import Any

from fashion_bot.config_manager import aget_shopify_config
from fashion_bot.utils.http_client import get_shared_async_http_client
from fashion_bot.utils.shopify_throttle import shopify_graphql_post


def _gid(resource: str, value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("gid://"):
        return text
    numeric = re.search(r"(\d+)$", text)
    if not numeric:
        return text
    return f"gid://shopify/{resource}/{numeric.group(1)}"


async def _ashopify_return_graphql(
    *,
    client_id: str,
    query: str,
    variables: dict[str, Any],
) -> dict:
    config = await aget_shopify_config(client_id=client_id) or {}
    shop_url = config.get("shop_url")
    token = config.get("access_token")
    api_version = config.get("api_version", "2025-07")
    if not shop_url or not token:
        return {"success": False, "error": "Missing Shopify configuration"}

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
            {"query": query, "variables": variables},
            rate_limit_key=shop_url,
        )
        return {"success": True, "data": payload.get("data") or {}, "raw": payload}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def aget_order_returns(
    *,
    client_id: str,
    order_gid_or_id: str,
) -> dict:
    query = """
    query GetOrderReturns($id: ID!) {
      order(id: $id) {
        id
        name
        returns(first: 25) {
          nodes {
            id
            name
            status
            createdAt
            updatedAt
            returnLineItems(first: 25) {
              nodes {
                id
                quantity
                customerNote
                returnReason
                fulfillmentLineItem {
                  id
                  lineItem {
                    id
                    name
                    quantity
                    variant { id title }
                    product { id title productType tags }
                  }
                }
              }
            }
            exchangeLineItems(first: 25) {
              nodes {
                id
                quantity
                variantId
                lineItems {
                  id
                  name
                  quantity
                  variant { id title }
                  product { id title productType tags }
                }
              }
            }
          }
        }
      }
    }
    """
    return await _ashopify_return_graphql(
        client_id=client_id,
        query=query,
        variables={"id": _gid("Order", order_gid_or_id)},
    )


async def aget_order_fulfillment_line_items(
    *,
    client_id: str,
    order_gid_or_id: str,
) -> dict:
    query = """
    query GetFulfillmentLineItems($id: ID!) {
      order(id: $id) {
        id
        name
        fulfillments(first: 25) {
          id
          status
          fulfillmentLineItems(first: 50) {
            nodes {
              id
              quantity
              lineItem {
                id
                name
                quantity
                variant { id title }
                product { id title productType tags }
              }
            }
          }
        }
      }
    }
    """
    return await _ashopify_return_graphql(
        client_id=client_id,
        query=query,
        variables={"id": _gid("Order", order_gid_or_id)},
    )


async def acreate_shopify_return(
    *,
    client_id: str,
    order_gid_or_id: str,
    return_line_items: list[dict[str, Any]],
    exchange_line_items: list[dict[str, Any]] | None = None,
    notify_customer: bool = False,
) -> dict:
    mutation = """
    mutation CreateReturn($returnInput: ReturnInput!) {
      returnCreate(returnInput: $returnInput) {
        return {
          id
          name
          status
          exchangeLineItems(first: 25) {
            nodes {
              id
              quantity
              variantId
            }
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    return_input: dict[str, Any] = {
        "orderId": _gid("Order", order_gid_or_id),
        "returnLineItems": return_line_items,
    }
    if exchange_line_items:
        return_input["exchangeLineItems"] = exchange_line_items
    if notify_customer:
        return_input["notifyCustomer"] = notify_customer
    variables = {
        "returnInput": return_input,
    }
    result = await _ashopify_return_graphql(
        client_id=client_id,
        query=mutation,
        variables=variables,
    )
    if not result.get("success"):
        return result
    payload = (result.get("data") or {}).get("returnCreate") or {}
    user_errors = payload.get("userErrors") or []
    if user_errors:
        return {"success": False, "error": "Shopify returnCreate user errors", "details": user_errors}
    return {"success": True, "return": payload.get("return"), "raw": result.get("raw")}
