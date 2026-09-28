"""
Async-backed Shopify order editing helpers for size changes.
"""

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

from fashion_bot.utils.http_client import get_shared_async_http_client
from fashion_bot.utils.utils import log_with_trace_id
from fashion_bot.utils.product_utils import resolve_variant_match
from fashion_bot.shopify.order_tags import OrderTag


async def _ashopify_graphql_request(
    payload: Dict[str, Any],
    access_token: str,
    shop_url: str,
    api_version: str = "2024-10",
) -> Dict[str, Any]:
    url = f"https://{shop_url}/admin/api/{api_version}/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token,
    }
    client = await get_shared_async_http_client()

    try:
        _t0 = time.monotonic()
        response = await client.post(url, headers=headers, json=payload, timeout=15)
        logger.info(f"[SHOPIFY] POST {url} elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={response.status_code}")
        if response.status_code != 200:
            return {
                "success": False,
                "error": f"GraphQL request failed with status {response.status_code}",
                "details": response.text,
            }

        data = response.json()
        if "errors" in data:
            return {"success": False, "error": "GraphQL errors", "details": data["errors"]}

        return {"success": True, "data": data}
    except httpx.TimeoutException:
        return {"success": False, "error": "GraphQL request timed out"}
    except httpx.HTTPError as exc:
        return {"success": False, "error": f"GraphQL HTTP error: {str(exc)}"}
    except Exception as exc:
        return {"success": False, "error": f"Exception: {str(exc)}"}


async def _aget_calculated_line_items(
    calculated_order_id: str,
    access_token: str,
    shop_url: str,
    state: Optional[dict] = None,
    api_version: str = "2024-10",
) -> Dict[str, Any]:
    query = """
    query getCalculatedLineItems($id: ID!) {
      calculatedOrder(id: $id) {
        id
        lineItems(first: 50) {
          edges {
            node {
              id
              title
              quantity
              variant {
                id
                title
              }
            }
          }
        }
      }
    }
    """
    payload = {"query": query, "variables": {"id": calculated_order_id}}
    graphql_result = await _ashopify_graphql_request(payload, access_token, shop_url, api_version)
    if not graphql_result.get("success"):
        return graphql_result

    calculated_order = graphql_result["data"].get("data", {}).get("calculatedOrder", {})
    return {"success": True, "line_items": calculated_order.get("lineItems", {}).get("edges", [])}


async def aorder_edit_begin(
    shopify_order_id: str,
    access_token: str,
    shop_url: str,
    state: Optional[dict] = None,
    api_version: str = "2024-10",
) -> Dict[str, Any]:
    mutation = """
    mutation orderEditBegin($id: ID!) {
      orderEditBegin(id: $id) {
        calculatedOrder {
          id
          lineItems(first: 50) {
            edges {
              node {
                id
                title
                quantity
                variant {
                  id
                  title
                }
              }
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
    payload = {"query": mutation, "variables": {"id": shopify_order_id}}

    log_with_trace_id(state, f"Starting async order edit session for order: {shopify_order_id}")
    graphql_result = await _ashopify_graphql_request(payload, access_token, shop_url, api_version)
    if not graphql_result.get("success"):
        return graphql_result

    result = graphql_result["data"].get("data", {}).get("orderEditBegin", {})
    user_errors = result.get("userErrors", [])
    if user_errors:
        return {"success": False, "error": "User errors in orderEditBegin", "details": user_errors}

    calculated_order = result.get("calculatedOrder", {})
    calculated_order_id = calculated_order.get("id")
    if not calculated_order_id:
        return {"success": False, "error": "No calculatedOrder ID returned", "details": result}

    return {
        "success": True,
        "calculated_order_id": calculated_order_id,
        "calculated_order": calculated_order,
    }


async def aorder_edit_set_quantity(
    calculated_order_id: str,
    line_item_id: str,
    quantity: int,
    access_token: str,
    shop_url: str,
    state: Optional[dict] = None,
    api_version: str = "2024-10",
) -> Dict[str, Any]:
    mutation = """
    mutation orderEditSetQuantity($id: ID!, $lineItemId: ID!, $quantity: Int!) {
      orderEditSetQuantity(id: $id, lineItemId: $lineItemId, quantity: $quantity) {
        calculatedOrder { id }
        calculatedLineItem { id quantity }
        userErrors { field message }
      }
    }
    """
    payload = {
        "query": mutation,
        "variables": {"id": calculated_order_id, "lineItemId": line_item_id, "quantity": quantity},
    }

    graphql_result = await _ashopify_graphql_request(payload, access_token, shop_url, api_version)
    if not graphql_result.get("success"):
        return graphql_result

    result = graphql_result["data"].get("data", {}).get("orderEditSetQuantity", {})
    user_errors = result.get("userErrors", [])
    if user_errors:
        return {"success": False, "error": "User errors in orderEditSetQuantity", "details": user_errors}
    return {"success": True, "calculated_line_item": result.get("calculatedLineItem")}


async def aorder_edit_add_variant(
    calculated_order_id: str,
    variant_id: str,
    quantity: int,
    access_token: str,
    shop_url: str,
    state: Optional[dict] = None,
    api_version: str = "2024-10",
) -> Dict[str, Any]:
    mutation = """
    mutation orderEditAddVariant($id: ID!, $variantId: ID!, $quantity: Int!) {
      orderEditAddVariant(id: $id, variantId: $variantId, quantity: $quantity) {
        calculatedOrder { id }
        calculatedLineItem {
          id
          variant { id title }
          quantity
        }
        userErrors { field message }
      }
    }
    """
    payload = {
        "query": mutation,
        "variables": {"id": calculated_order_id, "variantId": variant_id, "quantity": quantity},
    }

    graphql_result = await _ashopify_graphql_request(payload, access_token, shop_url, api_version)
    if not graphql_result.get("success"):
        return graphql_result

    result = graphql_result["data"].get("data", {}).get("orderEditAddVariant", {})
    user_errors = result.get("userErrors", [])
    if user_errors:
        return {"success": False, "error": "User errors in orderEditAddVariant", "details": user_errors}
    return {"success": True, "calculated_line_item": result.get("calculatedLineItem")}


async def aorder_edit_commit(
    calculated_order_id: str,
    access_token: str,
    shop_url: str,
    notify_customer: bool = True,
    state: Optional[dict] = None,
    api_version: str = "2024-10",
) -> Dict[str, Any]:
    mutation = """
    mutation orderEditCommit($id: ID!, $notifyCustomer: Boolean) {
      orderEditCommit(id: $id, notifyCustomer: $notifyCustomer) {
        order {
          id
          name
          lineItems(first: 10) {
            edges {
              node {
                id
                title
                quantity
                variant { id title }
              }
            }
          }
        }
        userErrors { field message }
      }
    }
    """
    payload = {
        "query": mutation,
        "variables": {"id": calculated_order_id, "notifyCustomer": notify_customer},
    }

    graphql_result = await _ashopify_graphql_request(payload, access_token, shop_url, api_version)
    if not graphql_result.get("success"):
        return graphql_result

    result = graphql_result["data"].get("data", {}).get("orderEditCommit", {})
    user_errors = result.get("userErrors", [])
    if user_errors:
        return {"success": False, "error": "User errors in orderEditCommit", "details": user_errors}
    return {"success": True, "order": result.get("order")}


async def _aadd_size_update_note_to_shopify(
    order_id: str,
    old_size: str,
    new_size: str,
    shopify_success: bool,
    shiprocket_success: bool,
    state: Optional[dict] = None,
) -> None:
    try:
        from fashion_bot.core.factory import ServiceFactory

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if shopify_success and shiprocket_success:
            status = "Successfully updated in both Shopify and Shiprocket"
        elif shopify_success and not shiprocket_success:
            status = "Updated in Shopify, Shiprocket update failed"
        elif not shopify_success and shiprocket_success:
            status = "Shopify update failed, updated in Shiprocket"
        else:
            status = "Update failed in both systems"

        note = f"[Bloomerce] Size update: {old_size} → {new_size}. {status}."
        order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
        await order_service.aadd_order_note(order_id, note, state=state)
    except Exception as exc:
        log_with_trace_id(state, f"Exception adding note to Shopify: {str(exc)}", "warning")


def _select_recreate_line_items(
    line_items: List[Dict[str, Any]],
    old_size: str,
    new_variant_id: Any,
    line_item_variant_id: str = "",
) -> Dict[str, Any]:
    """Build the cloned line-items list for a size change.

    Replaces exactly one line item — the one identified by
    ``line_item_variant_id`` (matched against each line's ``variant_id``) — with
    ``new_variant_id``, preserving every other item verbatim.

    Prefer the exact variant id the agent selected: on a multi-item order
    several lines can share the same size (e.g. two different products both in
    "M"), so matching on the size string alone picks the first match — which
    may be the wrong product, silently dropping the intended item and
    duplicating another (the G37792 → G37793 incident). Size-string matching
    is kept only as a fallback for single-item / legacy callers that don't
    pass a variant id.

    Returns ``{"success": True, "new_line_items": [...]}`` on success, or
    ``{"success": False, "error": ...}`` when the target can't be located.
    """
    old_lower = old_size.lower()
    target_vid = str(line_item_variant_id).strip()
    new_line_items: List[Dict[str, Any]] = []
    replaced = False
    for item in line_items:
        item_vid = str(item.get("variant_id", "")).strip()
        if target_vid:
            is_target = item_vid == target_vid and not replaced
        else:
            vt = (item.get("variant_title") or "").lower()
            item_name = (item.get("name") or "").lower()
            is_target = (vt == old_lower or old_lower in vt or old_lower in item_name) and not replaced
        if is_target:
            new_line_items.append({"variant_id": new_variant_id, "quantity": item.get("quantity", 1)})
            replaced = True
        else:
            new_line_items.append({"variant_id": item["variant_id"], "quantity": item.get("quantity", 1)})

    if not replaced:
        if target_vid:
            return {
                "success": False,
                "error": "variant_id_not_in_order",
                "valid_variant_ids": [
                    str(li.get("variant_id")) for li in line_items if li.get("variant_id") is not None
                ],
            }
        return {"success": False, "error": "size_not_matched"}
    return {"success": True, "new_line_items": new_line_items}


async def _acancel_and_recreate_order_with_new_size(
    order_id: str,
    order_data: dict,
    old_size: str,
    new_size: str,
    product_handle: str,
    state: Optional[dict] = None,
    old_variant_price: float = 0,
    new_variant_price: float = 0,
    line_item_variant_id: str = "",
) -> Dict[str, Any]:
    """Cancel and recreate an order with a new size (variant change).

    Uses the clone approach: fetches the full original order, resolves the new
    variant, builds a new line-items list that preserves every other item, and
    creates the replacement order via ``OrderCreationOrchestrator.aclone_order``.

    ``line_item_variant_id`` identifies the exact line item to swap. When
    provided it is matched against each line item's ``variant_id`` — this is
    the only reliable selector on a multi-item order where several lines can
    share the same size (e.g. two different products both in "M"). Matching on
    the size string alone swaps the first size match, which may be a different
    product, silently dropping the intended item and duplicating another.
    Size-string matching is kept only as a fallback for single-item / legacy
    callers that don't pass a variant id.
    """
    from decimal import Decimal

    from fashion_bot.config_manager import aget_shopify_config
    from fashion_bot.core.factory import ServiceFactory
    from fashion_bot.core.orchestrator import OrderCreationOrchestrator
    from fashion_bot.shopify.modules.product_handlers import ashopify_get_product_by_handle_graphql
    from fashion_bot.utils.order_utils import (
        astamp_bloomerce_edited,
        build_payment_reference_tags,
        classify_payment_type,
    )

    try:
        client_id = state.get("client_id") if state else None
        shopify_config = await aget_shopify_config(client_id=client_id)
        if not shopify_config or not shopify_config.get("access_token"):
            return {"success": False, "error": "Shopify configuration not found"}

        access_token = shopify_config["access_token"]
        shop_url = shopify_config["shop_url"]
        api_version = shopify_config.get("api_version", "2024-04")
        line_items = order_data.get("line_items", [])
        order_name = order_data.get("name", order_id)

        if not product_handle:
            return {"success": False, "error": "Could not determine product handle for recreation"}

        # ── Resolve new variant ID from product handle + new_size ──
        product_result = await ashopify_get_product_by_handle_graphql(
            handle=product_handle,
            access_token=access_token,
            shop_url=shop_url,
            api_version=api_version,
            formatted_response=False,
        )
        if not product_result.get("success") or not product_result.get("product"):
            return {"success": False, "error": f"Could not fetch product '{product_handle}' for variant resolution"}

        product_data = product_result["product"]
        from fashion_bot.shopify.tools.order_adapter import ShopifyOrderAdapter
        variant_match = ShopifyOrderAdapter._find_variant_by_size(product_data, new_size, state=state)
        if not variant_match.get("success"):
            return variant_match

        new_variant_id = int(str(variant_match["variant_id"]).replace("gid://shopify/ProductVariant/", ""))

        # ── Build cloned line items: replace the targeted item, keep all others ──
        selection = _select_recreate_line_items(
            line_items=line_items,
            old_size=old_size,
            new_variant_id=new_variant_id,
            line_item_variant_id=line_item_variant_id,
        )
        if not selection.get("success"):
            if selection.get("error") == "variant_id_not_in_order":
                return {
                    "success": False,
                    "error": "variant_id_not_in_order",
                    "message": (
                        f"variant_id '{line_item_variant_id}' is not a line item in order {order_id}."
                    ),
                    "valid_variant_ids": selection.get("valid_variant_ids", []),
                }
            return {"success": False, "error": f"Could not find line item with variant '{old_size}' to replace"}
        new_line_items = selection["new_line_items"]

        payment_info = classify_payment_type(order_data)
        payment_type = payment_info["payment_type"]
        amount_paid = payment_info["amount_paid"]

        order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")

        # ── Price-difference guard (ALL payment types, incl. COD) ──
        # A size change must never silently alter what the customer pays. If
        # the new variant's price differs from the old one, escalate WITHOUT
        # cancelling or recreating: for prepaid this avoids an over/under
        # charge, and for COD it avoids recreating an order that would collect
        # a different amount on delivery than the customer agreed to.
        #
        # Self-resolve prices when the caller doesn't pass them (e.g. the
        # cancel_and_recreate strategy shortcut). This ensures the guard is
        # never bypassed due to missing price arguments.
        _resolved_old = old_variant_price
        _resolved_new = new_variant_price
        if not _resolved_old:
            target_vid = str(line_item_variant_id).strip() if line_item_variant_id else ""
            for li in line_items:
                if target_vid and str(li.get("variant_id", "")).strip() == target_vid:
                    _resolved_old = float(li.get("price", 0))
                    break
                elif not target_vid and old_size.lower() in (li.get("variant_title") or "").lower():
                    _resolved_old = float(li.get("price", 0))
                    break
        if not _resolved_new:
            new_vid_str = str(new_variant_id)
            for v in product_data.get("variants", []):
                vid = str(v.get("id", "")).replace("gid://shopify/ProductVariant/", "")
                if vid == new_vid_str:
                    _resolved_new = float(v.get("price", 0))
                    break

        old_price = Decimal(str(_resolved_old)) if _resolved_old else Decimal("0")
        new_price = Decimal(str(_resolved_new)) if _resolved_new else Decimal("0")
        differential = new_price - old_price

        logger.info(
            f"[SIZE_UPDATE] {payment_type} order {order_id} — "
            f"old_price=₹{old_price}, new_price=₹{new_price}, differential=₹{differential}"
        )

        if differential != 0:
            direction = "owes" if differential > 0 else "is owed"
            abs_diff = abs(differential)
            logger.info(
                f"[SIZE_UPDATE] Non-zero differential (₹{abs_diff}) — customer {direction} money. "
                f"Returning escalation info (no order changes)."
            )
            return {
                "success": True,
                "requires_escalation": True,
                "order_not_modified": True,
                "old_order_id": order_name,
                "old_order_cancelled": False,
                "new_order_created": False,
                "old_size": old_size,
                "new_size": new_size,
                "old_variant": old_size,
                "new_variant": new_size,
                "old_variant_price": float(old_price),
                "new_variant_price": float(new_price),
                "differential_amount": float(differential),
                "original_amount_paid": float(amount_paid),
                "payment_type": payment_type,
                "method": "cancel_and_recreate",
                "message": (
                    f"Variant change for order {order_name} ({old_size} → {new_size}) requires escalation "
                    f"due to a price difference of ₹{abs_diff}. The existing order has NOT been cancelled "
                    f"and no new order has been created."
                ),
            }

        # Pin the cloned order back to the original's captured payment so
        # finance/ops can reconcile: orig_txn_id (Shopify transaction id) plus
        # the gateway reference(s) from the original order's note_attributes
        # (e.g. orig_PayU_txn_id). Same tags the cancel-and-recreate update flow
        # adds. Fail-open — yields no extra tags for COD or on a fetch failure;
        # the note_attribute tags need no extra API call. Computed once here and
        # appended to additional_tags in both clone scenarios below.
        payment_ref_tags: List[str] = []
        if float(amount_paid) > 0:
            txns: List[Dict[str, Any]] = []
            if hasattr(order_service, "aget_order_transactions"):
                txns = await order_service.aget_order_transactions(
                    order_data.get("id"), state=state,
                )
            payment_ref_tags = build_payment_reference_tags(
                txns, note_attributes=order_data.get("note_attributes"),
            )

        # ── SCENARIO 1: COD (same price) → cancel old, clone with new variant ──
        if payment_type == "cod":
            logger.info(f"[SIZE_UPDATE] COD order {order_id} — cancel and clone with new size")

            await astamp_bloomerce_edited(
                order_service, order_id, "size", state=state,
                order_data=order_data, extra_tags=[OrderTag.BLOOMERCE_UPDATED],
            )
            cancel_result = await order_service.acancel_order(
                order_id, "other", state=state,
                custom_note=f"Order cancelled for size change ({old_size} → {new_size}). COD order. Tags: SIZE_CHANGE_CANCELLED",
            )
            if not cancel_result.get("success"):
                return {
                    "success": False,
                    "error": f"Failed to cancel original order: {cancel_result.get('error')}",
                    "cancel_result": cancel_result,
                }

            create_result = await OrderCreationOrchestrator.aclone_order(
                original_order_data=order_data,
                new_line_items=new_line_items,
                state=state,
                note=f"Size change from order {order_name}. Changed {old_size} → {new_size}. Customer requested via Bloomerce.",
                additional_tags=[OrderTag.SIZE_CHANGE_CLONED, OrderTag.BLOOMERCE_UPDATED],
            )
            if create_result.get("success"):
                new_order_id = create_result.get("order_name") or create_result.get("order_id")
                return {
                    "success": True,
                    "message": f"Variant updated successfully! Old order {order_id} cancelled, new order {new_order_id} created with variant {new_size}",
                    "old_order_id": order_id,
                    "new_order_id": new_order_id,
                    "old_size": old_size,
                    "new_size": new_size,
                    "old_variant": old_size,
                    "new_variant": new_size,
                    "payment_type": "cod",
                    "method": "cancel_and_recreate",
                    "create_result": create_result,
                }

            return {
                "success": False,
                "error": f"Original order cancelled but failed to create new order: {create_result.get('error')}",
                "old_order_cancelled": True,
                "create_error": create_result.get("error"),
            }

        # ── SCENARIO 3: Prepaid / Partial-Prepaid (same price — guarded above)
        #    → cancel (skip refund), clone with original financial_status ──
        logger.info(f"[SIZE_UPDATE] Same price (₹{new_price}) — direct swap for {payment_type} order {order_id}")

        await astamp_bloomerce_edited(
            order_service, order_id, "size", state=state,
            order_data=order_data, extra_tags=[OrderTag.BLOOMERCE_UPDATED],
        )
        cancel_result = await order_service.acancel_order(
            order_id, "other", state=state,
            custom_note=(
                f"Order cancelled for size change ({old_size} → {new_size}). "
                f"Same price — credit applied to new order. Tags: SIZE_CHANGE_CANCELLED"
            ),
            skip_refund=True,
        )
        if not cancel_result.get("success"):
            return {
                "success": False,
                "error": f"Failed to cancel original order: {cancel_result.get('error')}",
                "cancel_result": cancel_result,
            }

        original_financial_status = order_data.get("financial_status", "pending")
        original_gateways = order_data.get("payment_gateway_names", [])

        clone_transactions = None
        if float(amount_paid) > 0:
            gateway = original_gateways[0] if original_gateways else "manual"
            clone_transactions = [{
                "kind": "sale",
                "status": "success",
                "amount": str(amount_paid),
                "gateway": gateway,
            }]

        create_result = await OrderCreationOrchestrator.aclone_order(
            original_order_data=order_data,
            new_line_items=new_line_items,
            state=state,
            note=(
                f"Size change from order {order_name}. Changed {old_size} → {new_size}. "
                f"Same price swap — previous payment of ₹{amount_paid} applied. Customer requested via Bloomerce."
            ),
            additional_tags=[OrderTag.SIZE_CHANGE_CLONED, OrderTag.BLOOMERCE_UPDATED] + payment_ref_tags,
            financial_status_override=original_financial_status,
            payment_gateway_names_override=original_gateways if original_gateways else None,
            transactions=clone_transactions,
        )
        if create_result.get("success"):
            new_order_id = create_result.get("order_name") or create_result.get("order_id")
            return {
                "success": True,
                "message": (
                    f"Variant updated successfully! Old order {order_id} cancelled, new order {new_order_id} "
                    f"created with variant {new_size}. Your previous payment of ₹{amount_paid} has been applied."
                ),
                "old_order_id": order_id,
                "new_order_id": new_order_id,
                "old_size": old_size,
                "new_size": new_size,
                "old_variant": old_size,
                "new_variant": new_size,
                "payment_type": payment_type,
                "original_amount_paid": float(amount_paid),
                "method": "cancel_and_recreate",
                "create_result": create_result,
            }

        return {
            "success": False,
            "error": f"Original order cancelled but failed to create new order: {create_result.get('error')}",
            "old_order_cancelled": True,
            "customer_credit": float(amount_paid),
            "requires_manual_intervention": True,
            "create_error": create_result.get("error"),
        }
    except Exception as exc:
        return {"success": False, "error": f"Exception during cancel-and-recreate: {str(exc)}"}


async def _acancel_shiprocket_order_only(
    order_id: str,
    state: Optional[dict] = None,
) -> Dict[str, Any]:
    try:
        from fashion_bot.core.factory import ServiceFactory

        logistics_service = await ServiceFactory.aget_logistics_service(state=state)
        result = await logistics_service.acancel_shipment(order_id, state=state)
        if result.get("success"):
            return result
        error_message = (result.get("error") or "").lower()
        if "not found" in error_message:
            return {"success": False, "skipped": True, "error": result.get("error")}
        return result
    except Exception as exc:
        return {"success": False, "error": f"Exception: {str(exc)}"}


async def _aattempt_shiprocket_only_update(
    order_id: str,
    order_data: dict,
    old_size: str,
    new_size: str,
    product_handle: str,
    shopify_error: str,
    shopify_details: Any,
    state: Optional[dict] = None,
    old_variant_price: float = 0,
    new_variant_price: float = 0,
    quantity: int = 0,
    line_item_variant_id: str = "",
) -> Dict[str, Any]:
    if quantity > 0:
        line_items = order_data.get("line_items", [])
        old_lower = old_size.lower()
        for item in line_items:
            if item.get("variant_title", "").lower() == old_lower or old_lower in item.get("name", "").lower():
                if quantity < item.get("quantity", 1):
                    return {
                        "success": True,
                        "requires_escalation": True,
                        "order_not_modified": True,
                        "message": (
                            f"Partial quantity variant change ({quantity} of {item.get('quantity', 1)}) "
                            f"cannot be performed via cancel-and-recreate. Please escalate to a human agent."
                        ),
                        "old_variant": old_size,
                        "new_variant": new_size,
                        "old_size": old_size,
                        "new_size": new_size,
                        "quantity_requested": quantity,
                    }
                break
    shiprocket_cancel_result = await _acancel_shiprocket_order_only(order_id=order_id, state=state)
    recreate_result = await _acancel_and_recreate_order_with_new_size(
        order_id=order_id,
        order_data=order_data,
        old_size=old_size,
        new_size=new_size,
        product_handle=product_handle,
        state=state,
        old_variant_price=old_variant_price,
        new_variant_price=new_variant_price,
        line_item_variant_id=line_item_variant_id,
    )

    if recreate_result.get("requires_escalation"):
        return recreate_result

    if not recreate_result.get("success"):
        await _aadd_size_update_note_to_shopify(
            order_id=order_id,
            old_size=old_size,
            new_size=new_size,
            shopify_success=False,
            shiprocket_success=shiprocket_cancel_result.get("success", False),
            state=state,
        )
        return {
            "success": False,
            "error": f"Failed to recreate order: {recreate_result.get('error')}",
            "shopify": {"success": False, "error": shopify_error, "details": shopify_details},
            "shiprocket": {"success": shiprocket_cancel_result.get("success", False), "cancelled": True},
        }

    new_order_id = recreate_result.get("new_order_id")
    return {
        "success": True,
        "message": f"Variant updated successfully! Old order {order_id} cancelled, new order {new_order_id} created with variant {new_size}",
        "old_size": old_size,
        "new_size": new_size,
        "old_variant": old_size,
        "new_variant": new_size,
        "old_order_id": order_id,
        "new_order_id": new_order_id,
        "method": "cancel_and_recreate",
        "shopify": {
            "success": True,
            "method": "cancel_and_recreate",
            "old_order_cancelled": True,
            "new_order_id": new_order_id,
        },
    }


async def _aupdate_shiprocket_order_size(
    order_id: str,
    shopify_order_data: dict,
    old_size: str,
    new_size: str,
    state: Optional[dict] = None,
) -> Dict[str, Any]:
    from fashion_bot.config_manager import aget_config, aget_shiprocket_config

    try:
        client_id = state.get("client_id") if state else None
        shiprocket_config = await aget_shiprocket_config(client_id=client_id)
        email = shiprocket_config.get("email")
        password = shiprocket_config.get("password")
        if not email or not password:
            return {"success": False, "skipped": True, "error": "Shiprocket credentials not configured"}

        client = await get_shared_async_http_client()
        _t0 = time.monotonic()
        auth_response = await client.post(
            "https://apiv2.shiprocket.in/v1/external/auth/login",
            json={"email": email, "password": password},
            timeout=30,
        )
        logger.info(f"[SHOPIFY] POST https://apiv2.shiprocket.in/v1/external/auth/login elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={auth_response.status_code}")
        if auth_response.status_code != 200:
            return {"success": False, "error": "Failed to authenticate with Shiprocket"}

        token = auth_response.json().get("token")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        order_prefix = await aget_config("order_prefix", client_id=client_id, default=None)
        if order_prefix is None:
            order_prefix = ""

        order_id_clean = order_id.replace("#", "")
        if order_prefix:
            order_id_clean = order_id_clean.replace(order_prefix.lower(), "").replace(order_prefix.upper(), "")
            channel_order_id = f"{order_prefix.upper()}{order_id_clean}"
        else:
            channel_order_id = order_id_clean

        _t0 = time.monotonic()
        search_response = await client.get(
            "https://apiv2.shiprocket.in/v1/external/orders",
            params={"channel_order_id": channel_order_id},
            headers=headers,
            timeout=30,
        )
        logger.info(f"[SHOPIFY] GET https://apiv2.shiprocket.in/v1/external/orders elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={search_response.status_code}")
        if search_response.status_code != 200:
            return {"success": False, "skipped": True, "error": "Order not found in Shiprocket"}

        orders = search_response.json().get("data", [])
        shiprocket_order = next(
            (
                order
                for order in orders
                if order.get("channel_order_id", "").lower() == channel_order_id.lower()
            ),
            None,
        )
        if not shiprocket_order:
            return {"success": False, "skipped": True, "error": "Exact order match not found in Shiprocket"}

        shiprocket_order_id = shiprocket_order.get("id")
        if shiprocket_order.get("status_code") not in [1, 6]:
            return {
                "success": False,
                "skipped": True,
                "error": f"Order status ({shiprocket_order.get('status_code')}) doesn't allow modification",
            }

        _t0 = time.monotonic()
        cancel_response = await client.post(
            "https://apiv2.shiprocket.in/v1/external/orders/cancel",
            json={"ids": [shiprocket_order_id]},
            headers=headers,
            timeout=30,
        )
        logger.info(f"[SHOPIFY] POST https://apiv2.shiprocket.in/v1/external/orders/cancel elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={cancel_response.status_code}")
        if cancel_response.status_code not in [200, 201]:
            return {"success": False, "error": "Failed to cancel existing Shiprocket order"}

        line_items = shopify_order_data.get("line_items", [])
        new_order_items = []
        old_sku = None
        new_sku = None
        for item in line_items:
            item_sku = item.get("sku", "")
            item_name = item.get("name", "")
            is_old_size_item = (
                f" - {old_size}" in item_name
                or item_sku.endswith(f"-{old_size}")
                or item_sku.endswith(f"- {old_size}")
                or item_sku.endswith(f" - {old_size}")
            )
            if is_old_size_item:
                new_item_name = item_name.replace(f" - {old_size}", f" - {new_size}")
                new_item_sku = item_sku
                if item_sku.endswith(f"-{old_size}"):
                    new_item_sku = item_sku.replace(f"-{old_size}", f"-{new_size}")
                elif item_sku.endswith(f"- {old_size}"):
                    new_item_sku = item_sku.replace(f"- {old_size}", f"- {new_size}")
                elif item_sku.endswith(f" - {old_size}"):
                    new_item_sku = item_sku.replace(f" - {old_size}", f" - {new_size}")
                old_sku = item_sku
                new_sku = new_item_sku
                new_order_items.append(
                    {
                        "name": new_item_name,
                        "sku": new_item_sku,
                        "units": item.get("quantity", 1),
                        "selling_price": item.get("price"),
                        "discount": "0",
                        "tax": "",
                        "hsn": "",
                    }
                )
            else:
                new_order_items.append(
                    {
                        "name": item_name,
                        "sku": item_sku,
                        "units": item.get("quantity", 1),
                        "selling_price": item.get("price"),
                        "discount": "0",
                        "tax": "",
                        "hsn": "",
                    }
                )

        customer = shopify_order_data.get("customer") or {}
        shipping_address = shopify_order_data.get("shipping_address") or {}
        billing_address = shopify_order_data.get("billing_address") or {}

        def clean_phone(phone: str) -> str:
            if not phone:
                return ""
            phone = str(phone)
            if phone.startswith("+91"):
                phone = phone[3:]
            elif phone.startswith("91") and len(phone) > 10:
                phone = phone[2:]
            return "".join(filter(str.isdigit, phone))

        create_payload = {
            "order_id": channel_order_id,
            "order_date": shopify_order_data.get("created_at"),
            "pickup_location": "Primary",
            "channel_id": "",
            "comment": f"Order recreated with size change: {old_size} -> {new_size} (automated by Bloomerce)",
            "billing_customer_name": billing_address.get("first_name") or customer.get("first_name") or "",
            "billing_last_name": billing_address.get("last_name") or customer.get("last_name") or "",
            "billing_address": billing_address.get("address1") or "",
            "billing_address_2": billing_address.get("address2") or "",
            "billing_city": billing_address.get("city") or "",
            "billing_pincode": billing_address.get("zip") or "",
            "billing_state": billing_address.get("province") or "",
            "billing_country": billing_address.get("country") or "India",
            "billing_email": customer.get("email") or "",
            "billing_phone": clean_phone(billing_address.get("phone") or customer.get("phone") or ""),
            "shipping_is_billing": shipping_address == billing_address,
            "shipping_customer_name": shipping_address.get("first_name") or customer.get("first_name") or "",
            "shipping_last_name": shipping_address.get("last_name") or customer.get("last_name") or "",
            "shipping_address": shipping_address.get("address1") or "",
            "shipping_address_2": shipping_address.get("address2") or "",
            "shipping_city": shipping_address.get("city") or "",
            "shipping_pincode": shipping_address.get("zip") or "",
            "shipping_country": shipping_address.get("country") or "India",
            "shipping_state": shipping_address.get("province") or "",
            "shipping_email": customer.get("email") or "",
            "shipping_phone": clean_phone(shipping_address.get("phone") or customer.get("phone") or ""),
            "order_items": new_order_items,
            "payment_method": shiprocket_order.get("payment_method") or "COD",
            "shipping_charges": 0,
            "giftwrap_charges": 0,
            "transaction_charges": 0,
            "total_discount": 0,
            "sub_total": shopify_order_data.get("subtotal_price"),
            "length": 10,
            "breadth": 10,
            "height": 10,
            "weight": 0.5,
        }

        _t0 = time.monotonic()
        create_response = await client.post(
            "https://apiv2.shiprocket.in/v1/external/orders/create/adhoc",
            json=create_payload,
            headers=headers,
            timeout=30,
        )
        logger.info(f"[SHOPIFY] POST https://apiv2.shiprocket.in/v1/external/orders/create/adhoc elapsed_ms={int((time.monotonic() - _t0) * 1000)} status={create_response.status_code}")
        if create_response.status_code not in [200, 201]:
            return {
                "success": False,
                "error": f"Shiprocket API error {create_response.status_code}: {create_response.text[:500]}",
            }

        create_result = create_response.json()
        if create_result.get("status_code") == 1:
            return {
                "success": True,
                "message": "Shiprocket order updated via cancel-and-recreate",
                "old_order_id": shiprocket_order_id,
                "new_order_id": create_result.get("order_id"),
                "shipment_id": create_result.get("shipment_id"),
                "size_change": f"{old_size} -> {new_size}",
                "sku_change": f"{old_sku} -> {new_sku}",
            }
        if create_result.get("status_code") == 5:
            return {
                "success": False,
                "error": (
                    f"Shiprocket cannot recreate order with same channel_order_id "
                    f"'{channel_order_id}'."
                ),
            }
        return {
            "success": False,
            "error": f"Shiprocket creation failed: {create_result.get('message')}",
        }
    except Exception as exc:
        return {"success": False, "error": f"Exception: {str(exc)}"}


async def aupdate_order_size_graphql(
    order_id: str,
    old_size: str,
    new_size: str,
    access_token: str,
    shop_url: str,
    line_item_variant_id: str = "",
    quantity: int = 0,
    state: Optional[dict] = None,
    api_version: str = "2024-10",
) -> Dict[str, Any]:
    try:
        from fashion_bot.core.factory import ServiceFactory
        from fashion_bot.shopify.modules.product_handlers import (
            ashopify_get_product_by_id_graphql,
        )

        order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
        order_data = await order_service.aget_order_details(order_id, state=state)
        if not order_data:
            return {"success": False, "error": f"Order {order_id} not found"}

        # ── Strategy short-circuit ───────────────────────────────────────
        # When any connected partner is configured for cancel_and_recreate,
        # skip the in-place orderEdit GraphQL flow AND the per-partner
        # cancel-recreate handlers. Use the already-existing
        # _acancel_and_recreate_order_with_new_size helper directly — it
        # does Shopify cancel + clone with payment preservation, then relies
        # on Shopify webhooks to auto-sync the new order to every connected
        # partner.
        try:
            from fashion_bot.core.orchestrator import OrderUpdateOrchestrator, UpdateStrategyResolution
            resolution = await OrderUpdateOrchestrator._aresolve_update_strategy(state)
        except Exception:
            resolution = UpdateStrategyResolution(strategy="legacy")  # defensive fallback
        if resolution.strategy == OrderUpdateOrchestrator.STRATEGY_CANCEL_AND_RECREATE:
            log_with_trace_id(
                state,
                f"[SIZE_UPDATE] strategy=cancel_and_recreate for {order_id} — "
                f"routing to _acancel_and_recreate_order_with_new_size; "
                f"skipping orderEdit + partner handler",
            )
            # Resolve product handle for variant lookup. Anchor on the exact
            # line item the agent selected (line_item_variant_id) so the new
            # size is resolved against the right product — on a multi-item
            # order the first line item is often a different product.
            line_items = order_data.get("line_items", [])
            target_vid = str(line_item_variant_id).strip()
            target_li = None
            if target_vid:
                target_li = next(
                    (li for li in line_items if str(li.get("variant_id", "")).strip() == target_vid),
                    None,
                )
            handle_candidates = [target_li] if target_li else line_items
            product_handle = next(
                (
                    li.get("product_handle") or (li.get("properties") or {}).get("handle")
                    for li in handle_candidates
                    if li and li.get("product_handle")
                ),
                "",
            )
            if not product_handle:
                # Fallback — fetch product to get handle. Use the targeted
                # line item's product_id when known, else the first item's.
                fallback_pid = (target_li or (line_items[0] if line_items else {})).get("product_id")
                if fallback_pid:
                    try:
                        prod_res = await ashopify_get_product_by_id_graphql(
                            str(fallback_pid),
                            access_token=access_token,
                            shop_url=shop_url,
                            api_version=api_version,
                        )
                        product_handle = (prod_res.get("product") or {}).get("handle", "")
                    except Exception:
                        product_handle = ""
            return await _acancel_and_recreate_order_with_new_size(
                order_id=order_id,
                order_data=order_data,
                old_size=old_size,
                new_size=new_size,
                product_handle=product_handle,
                state=state,
                line_item_variant_id=line_item_variant_id,
            )

        shopify_order_id = order_data.get("id")
        if not shopify_order_id:
            return {"success": False, "error": "No Shopify order ID found in order data"}

        line_items = order_data.get("line_items", [])

        target_line_item = None
        if line_item_variant_id:
            vid = str(line_item_variant_id).strip()
            target_line_item = next(
                (item for item in line_items if str(item.get("variant_id", "")) == vid),
                None,
            )
            if not target_line_item:
                return {
                    "success": False,
                    "error": "variant_id_not_in_order",
                    "message": (
                        f"variant_id '{line_item_variant_id}' is not a line item in "
                        f"order {order_id}. Call get_order_details for this order and "
                        f"use one of its actual variant_id values from line_items — do "
                        f"not use a variant_id from product search or the catalog."
                    ),
                    "available_items": [
                        {"name": item.get("name", ""), "variant_id": item.get("variant_id")}
                        for item in line_items
                    ],
                    "valid_variant_ids": [
                        str(item.get("variant_id"))
                        for item in line_items
                        if item.get("variant_id") is not None
                    ],
                }
        else:
            old_lower = old_size.lower()
            target_line_item = next(
                (
                    item for item in line_items
                    if item.get("variant_title", "").lower() == old_lower
                ),
                None,
            )
            if not target_line_item:
                target_line_item = next(
                    (
                        item for item in line_items
                        if old_lower in item.get("variant_title", "").lower()
                        or old_lower in item.get("name", "").lower()
                    ),
                    None,
                )
            if not target_line_item:
                return {
                    "success": False,
                    "error": f"Could not find item with variant '{old_size}' in the order",
                    "suggestion": "Please verify the current variant value.",
                    "available_variants": [
                        item.get("variant_title", "") or item.get("name", "")
                        for item in line_items
                    ],
                }

        matched_variant_id = target_line_item.get("variant_id")
        line_item_product_id = target_line_item.get("product_id")

        if not line_item_product_id:
            return {
                "success": False,
                "error": "Line item has no product_id — cannot resolve product variants",
            }

        product_result = await ashopify_get_product_by_id_graphql(
            line_item_product_id,
            access_token,
            shop_url,
            api_version,
            formatted_response=False,
        )
        if not product_result or not product_result.get("success"):
            return {
                "success": False,
                "error": f"Could not fetch product details: {(product_result or {}).get('error', 'missing product data')}",
            }

        product = product_result.get("product", {})
        resolved_product_handle = product.get("handle", "")
        variants = product.get("variants", [])
        # Resolve the requested value to a variant. The shared matcher handles
        # multi-option products whose title is a compound "Colour / Size" (a bare
        # 'S' never equals that full title) by anchoring on the current line
        # item's variant and changing only the option the customer named — while
        # still working for single-option (size-only / colour-only) products.
        matched_new_variant = resolve_variant_match(
            variants,
            new_size,
            current_variant_id=str(matched_variant_id or ""),
            current_value=old_size,
        )
        new_variant_id = None
        new_variant_price = 0.0
        if matched_new_variant:
            variant_gid = matched_new_variant.get("id", "")
            new_variant_id = (
                variant_gid
                if "ProductVariant/" in variant_gid
                else f"gid://shopify/ProductVariant/{matched_new_variant.get('id')}"
            )
            new_variant_price = float(matched_new_variant.get("price", "0"))

        old_variant_price = float(target_line_item.get("price", "0"))

        if not new_variant_id:
            return {
                "success": False,
                "error": f"Could not find variant '{new_size}' for this product",
                "available_variants": [variant.get("title") for variant in variants],
            }

        is_available = matched_new_variant.get("is_available", True)
        inventory_qty = matched_new_variant.get("inventoryQuantity", 0)
        inventory_policy = matched_new_variant.get("inventoryPolicy", "DENY")
        if not is_available:
            in_stock_variants = [
                v.get("title") for v in variants
                if v.get("is_available", False)
                and v.get("title", "").lower() != old_size.lower()
            ]
            log_with_trace_id(
                state,
                f"⚠️ Variant '{new_size}' is out of stock for order {order_id} "
                f"(inventory={inventory_qty}, policy={inventory_policy})",
                "warning",
            )
            return {
                "success": False,
                "error": (
                    f"Size '{new_size}' is currently out of stock "
                    f"(inventory: {inventory_qty})"
                ),
                "in_stock_variants": in_stock_variants,
                "suggestion": (
                    f"The requested size '{new_size}' is not available. "
                    + (
                        f"Available sizes: {', '.join(in_stock_variants)}."
                        if in_stock_variants
                        else "No other sizes are currently in stock for this product."
                    )
                ),
            }

        original_qty = target_line_item.get("quantity", 1)
        change_qty = quantity if quantity > 0 else original_qty
        if change_qty > original_qty:
            return {
                "success": False,
                "error": (
                    f"Requested quantity ({change_qty}) exceeds line item quantity ({original_qty})"
                ),
            }
        remaining_qty = original_qty - change_qty

        order_discount_codes = order_data.get("discount_codes") or []
        has_discounts = any(dc.get("code") for dc in order_discount_codes)
        if has_discounts:
            log_with_trace_id(
                state,
                f"⚠️ Order {order_id} has discount codes "
                f"{[dc.get('code') for dc in order_discount_codes]}. "
                f"Skipping GraphQL edit (discounts are lost on new line items). "
                f"Using cancel-and-recreate.",
                "warning",
            )
            return await _aattempt_shiprocket_only_update(
                order_id=order_id,
                order_data=order_data,
                old_size=old_size,
                new_size=new_size,
                product_handle=resolved_product_handle,
                shopify_error="Skipped GraphQL edit due to discount codes on order",
                shopify_details={"discount_codes": order_discount_codes},
                old_variant_price=old_variant_price,
                new_variant_price=new_variant_price,
                quantity=change_qty,
                state=state,
                line_item_variant_id=str(matched_variant_id),
            )

        # Price-differential guard for in-place GraphQL edit path.
        # Must mirror the check in _acancel_and_recreate_order_with_new_size —
        # a size/variant swap must never silently alter the order total.
        from decimal import Decimal as _Decimal
        _old_price = _Decimal(str(old_variant_price)) if old_variant_price else _Decimal("0")
        _new_price = _Decimal(str(new_variant_price)) if new_variant_price else _Decimal("0")
        _size_differential = (_new_price - _old_price) * change_qty

        if _size_differential != 0:
            from fashion_bot.utils.order_utils import classify_payment_type
            _pay_info = classify_payment_type(order_data)
            _direction = "owes" if _size_differential > 0 else "is owed"
            _abs_diff = abs(_size_differential)
            log_with_trace_id(
                state,
                f"⚠️ Price differential ₹{_abs_diff} in in-place size edit path — customer {_direction} money. "
                f"old_variant_price=₹{_old_price}, new_variant_price=₹{_new_price}. "
                f"Returning escalation info (no order changes made).",
            )
            order_name = order_data.get("name", order_id)
            return {
                "success": True,
                "requires_escalation": True,
                "order_not_modified": True,
                "old_order_id": order_name,
                "old_order_cancelled": False,
                "new_order_created": False,
                "old_size": old_size,
                "new_size": new_size,
                "old_variant": old_size,
                "new_variant": new_size,
                "old_variant_price": float(_old_price),
                "new_variant_price": float(_new_price),
                "differential_amount": float(_size_differential),
                "original_amount_paid": float(_pay_info.get("amount_paid", 0)),
                "payment_type": _pay_info.get("payment_type", "unknown"),
                "message": (
                    f"Variant change for order {order_name} ({old_size} → {new_size}) requires escalation "
                    f"due to a price difference of ₹{_abs_diff}. The existing order has NOT been modified."
                ),
            }

        shopify_order_gid = f"gid://shopify/Order/{shopify_order_id}"
        begin_result = await aorder_edit_begin(shopify_order_gid, access_token, shop_url, state, api_version)
        if not begin_result.get("success"):
            return await _aattempt_shiprocket_only_update(
                order_id=order_id,
                order_data=order_data,
                old_size=old_size,
                new_size=new_size,
                product_handle=resolved_product_handle,
                shopify_error="Failed to begin order edit session",
                shopify_details=begin_result,
                old_variant_price=old_variant_price,
                new_variant_price=new_variant_price,
                quantity=change_qty,
                state=state,
                line_item_variant_id=str(matched_variant_id),
            )

        calculated_order_id = begin_result.get("calculated_order_id")
        calculated_order = begin_result.get("calculated_order", {})
        calculated_line_items = calculated_order.get("lineItems", {}).get("edges", [])
        old_variant_id_numeric = str(matched_variant_id)
        target_calculated_line_item = None
        _fallback_item = None
        for edge in calculated_line_items:
            item = edge.get("node", {})
            variant_gid = (item.get("variant") or {}).get("id", "")
            if "ProductVariant/" in variant_gid and variant_gid.split("ProductVariant/")[-1] == old_variant_id_numeric:
                if item.get("quantity", 0) > 0:
                    target_calculated_line_item = item
                    break
                elif _fallback_item is None:
                    _fallback_item = item
        if not target_calculated_line_item:
            target_calculated_line_item = _fallback_item

        if target_calculated_line_item:
            await aorder_edit_set_quantity(
                calculated_order_id,
                target_calculated_line_item.get("id"),
                remaining_qty,
                access_token,
                shop_url,
                state,
                api_version,
            )

        add_result = await aorder_edit_add_variant(
            calculated_order_id,
            new_variant_id,
            change_qty,
            access_token,
            shop_url,
            state,
            api_version,
        )
        if not add_result.get("success"):
            return {"success": False, "error": "Failed to add new size variant", "details": add_result}

        commit_result = await aorder_edit_commit(
            calculated_order_id,
            access_token,
            shop_url,
            notify_customer=True,
            state=state,
            api_version=api_version,
        )
        shopify_success = commit_result.get("success")

        if shopify_success:
            from fashion_bot.utils.order_utils import astamp_bloomerce_edited
            await astamp_bloomerce_edited(
                order_service, order_id, "size", state=state, order_data=order_data,
            )

        # Dispatch to the right partner's cancel-and-recreate flow via the
        # central registry. Adding a new partner does not require editing
        # this branch — registrations expose ``cancel_recreate_handler`` and
        # the dispatch below picks the matching one. Falls back to
        # Shiprocket for legacy behaviour when tracking_company is missing.
        from fashion_bot.core.logistics_registry import get_partner
        from fashion_bot.utils.delivery_partner_utils import (
            aresolve_effective_partner_for_order,
        )

        # URL-first, matching every other per-order carrier resolution. An
        # aggregator never appears as tracking_company -- Shopify records the
        # underlying courier -- so resolving on that alone picks the wrong
        # partner, or none, for those tenants.
        tracking_company = (order_data or {}).get("tracking_company") or ""
        tracking_url = (order_data or {}).get("tracking_url") or ""
        if not tracking_url or not tracking_company:
            for _f in (order_data or {}).get("fulfillments") or []:
                if not isinstance(_f, dict):
                    continue
                tracking_url = tracking_url or (_f.get("tracking_url") or "")
                tracking_company = tracking_company or (_f.get("tracking_company") or "")
                if tracking_url:
                    break

        canonical, is_integrated = await aresolve_effective_partner_for_order(
            {"tracking_url": tracking_url, "tracking_company": tracking_company},
            state=state,
        )

        target_partner = canonical if (is_integrated and canonical) else "shiprocket"
        partner_reg = get_partner(target_partner) or get_partner("shiprocket")
        handler = partner_reg.cancel_recreate_handler if partner_reg else None

        if handler is not None:
            partner_result = await handler(
                order_id=order_id,
                shopify_order_data=order_data,
                old_size=old_size,
                new_size=new_size,
                state=state,
            )
        elif target_partner == "shiprocket":
            # No partner could be resolved for this order; keep the legacy
            # default rather than changing behaviour for existing tenants.
            partner_result = await _aupdate_shiprocket_order_size(
                order_id=order_id,
                shopify_order_data=order_data,
                old_size=old_size,
                new_size=new_size,
                state=state,
            )
        else:
            # A partner was resolved and is connected, but exposes no size
            # cancel-and-recreate handler. Running a different partner's API
            # against their shipment would cancel the wrong AWB -- or fail on
            # credentials they do not have. The Shopify edit is already
            # committed above, so escalate for a manual courier sync.
            log_with_trace_id(
                state,
                f"No cancel_recreate_handler for {target_partner}; size change on "
                f"{order_id} committed in Shopify, escalating for manual sync",
                "warning",
            )
            partner_result = {
                "success": False,
                "requires_verification": True,
                "verification_reason": (
                    f"{target_partner.title()} exposes no size cancel-and-recreate "
                    f"flow; confirm the courier record matches the updated size."
                ),
                "message": f"Size updated in Shopify; {target_partner} not synced automatically.",
            }
        # Keep legacy variable name for downstream block compatibility.
        shiprocket_result = partner_result

        # Some partners (e.g. Delhivery) only cancel the stale AWB and rely
        # on Shopify→partner auto-sync to recreate. They surface this by
        # returning ``requires_verification=True``. Escalate so an agent
        # confirms the new AWB actually got minted — otherwise the order
        # can end up "cancelled, never recreated" with no signal.
        if partner_result.get("requires_verification"):
            try:
                from fashion_bot.core.orchestrator import EscalationOrchestrator
                partner_label = (partner_reg.name if partner_reg else target_partner).title()
                await EscalationOrchestrator.aescalate_to_agent(
                    category=f"{partner_label} Auto-Sync Verification",
                    reason=(
                        f"Order {order_id}: {partner_label} AWB cancelled after size "
                        f"change ({old_size} → {new_size}); awaiting Shopify→"
                        f"{partner_label} sync to mint new AWB."
                    ),
                    details=partner_result.get(
                        "verification_reason",
                        f"Verify new AWB was created on {partner_label}.",
                    ),
                    state=state,
                    order_id=order_id,
                )
            except Exception as exc:
                logger.warning(
                    f"Failed to escalate {target_partner} verification for {order_id}: {exc}"
                )

        await _aadd_size_update_note_to_shopify(
            order_id=order_id,
            old_size=old_size,
            new_size=new_size,
            shopify_success=shopify_success,
            shiprocket_success=shiprocket_result.get("success", False),
            state=state,
        )

        qty_detail = f" (qty {change_qty} of {original_qty})" if remaining_qty > 0 else ""
        return {
            "success": shopify_success or shiprocket_result.get("success"),
            "order_id": order_id,
            "message": f"Order {order_id} updated: Variant changed from {old_size} to {new_size}{qty_detail}",
            "order": commit_result.get("order") if shopify_success else None,
            "old_size": old_size,
            "new_size": new_size,
            "old_variant": old_size,
            "new_variant": new_size,
            "quantity_changed": change_qty,
            "quantity_remaining": remaining_qty,
            "shopify": {
                "success": shopify_success,
                "error": commit_result.get("error") if not shopify_success else None,
                "details": commit_result.get("details") if not shopify_success else None,
            },
            "shiprocket": shiprocket_result,
        }
    except Exception as exc:
        log_with_trace_id(state, f"Exception in async update_order_size_graphql: {str(exc)}", "error")
        return {"success": False, "error": f"Both Shopify and Shiprocket updates failed. Shopify: {str(exc)}"}


async def achange_order_product_graphql(
    order_id: str,
    target_variant_id: str,
    new_variant_gid: str,
    new_variant_price: float,
    quantity: int,
    access_token: str,
    shop_url: str,
    state: Optional[dict] = None,
    api_version: str = "2024-10",
) -> Dict[str, Any]:
    """Replace one line item with a different product's variant via GraphQL Order Edit.

    Uses the same orderEditBegin / setQuantity / addVariant / commit flow as
    aupdate_order_size_graphql but swaps across different products.

    Args:
        order_id: Shopify numeric order ID (not the #-prefixed name).
        target_variant_id: Numeric variant ID of the line item to remove.
        new_variant_gid: Full GID of the new product variant to add
            (e.g. ``gid://shopify/ProductVariant/12345``).
        new_variant_price: Price of the new variant (for return metadata).
        quantity: Number of units to swap.
        access_token: Shopify Admin API access token.
        shop_url: Shopify store domain.
        state: Conversation state for logging.
        api_version: Shopify API version.

    Returns:
        Dict with ``success``, ``order``, and metadata on the swap.
    """
    try:
        shopify_order_gid = f"gid://shopify/Order/{order_id}"
        begin_result = await aorder_edit_begin(
            shopify_order_gid, access_token, shop_url, state, api_version
        )
        if not begin_result.get("success"):
            return begin_result

        calculated_order_id = begin_result.get("calculated_order_id")
        calculated_order = begin_result.get("calculated_order", {})
        calculated_line_items = calculated_order.get("lineItems", {}).get("edges", [])

        old_variant_id_numeric = str(target_variant_id)
        target_calculated_line_item = None
        _fallback_item = None
        for edge in calculated_line_items:
            item = edge.get("node", {})
            variant_gid = (item.get("variant") or {}).get("id", "")
            if (
                "ProductVariant/" in variant_gid
                and variant_gid.split("ProductVariant/")[-1] == old_variant_id_numeric
            ):
                if item.get("quantity", 0) > 0:
                    target_calculated_line_item = item
                    break
                elif _fallback_item is None:
                    _fallback_item = item
        if not target_calculated_line_item:
            target_calculated_line_item = _fallback_item

        if not target_calculated_line_item:
            return {
                "success": False,
                "error": f"Could not locate variant {target_variant_id} in the calculated order edit session",
            }

        set_qty_result = await aorder_edit_set_quantity(
            calculated_order_id,
            target_calculated_line_item.get("id"),
            0,
            access_token,
            shop_url,
            state,
            api_version,
        )
        if not set_qty_result.get("success"):
            return {
                "success": False,
                "error": "Failed to remove old line item from order edit session",
                "details": set_qty_result,
            }

        add_result = await aorder_edit_add_variant(
            calculated_order_id,
            new_variant_gid,
            quantity,
            access_token,
            shop_url,
            state,
            api_version,
        )
        if not add_result.get("success"):
            return {
                "success": False,
                "error": "Failed to add new product variant to order edit session",
                "details": add_result,
            }

        commit_result = await aorder_edit_commit(
            calculated_order_id,
            access_token,
            shop_url,
            notify_customer=True,
            state=state,
            api_version=api_version,
        )

        return {
            "success": commit_result.get("success", False),
            "order": commit_result.get("order"),
            "method": "graphql_order_edit",
            "error": commit_result.get("error") if not commit_result.get("success") else None,
            "details": commit_result.get("details") if not commit_result.get("success") else None,
        }
    except Exception as exc:
        log_with_trace_id(state, f"Exception in achange_order_product_graphql: {str(exc)}", "error")
        return {"success": False, "error": f"GraphQL product change failed: {str(exc)}"}
