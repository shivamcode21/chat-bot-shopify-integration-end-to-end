"""Config-driven return/exchange validation rules.

Rules are stored per tenant in ``client_configs.return_exchange_rules`` and are
evaluated before handing a customer to a return partner flow.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from fashion_bot.config_manager import aget_json_config, aget_shopify_config
from fashion_bot.utils.delivery_timeline import aget_confirmed_delivery_datetime
from fashion_bot.utils.http_client import get_shared_async_http_client
from fashion_bot.utils.shopify_rate_limiter import get_shopify_rate_limiter
from fashion_bot.utils.utils import log_with_trace_id

logger = logging.getLogger(__name__)

RETURN_EXCHANGE_RULES_CONFIG_KEY = "return_exchange_rules"


def _normalize_request_type(request_type: str | None) -> str:
    normalized = str(request_type or "return").strip().lower()
    return "exchange" if normalized == "exchange" else "return"


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _split_tags(value: Any) -> set[str]:
    if not value:
        return set()
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, list):
        parts = value
    else:
        parts = [value]
    return {str(part).strip().lower() for part in parts if str(part).strip()}


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _as_list(value: Any) -> list:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    return [value]


def _merge_type_rules(rules_config: dict, request_type: str) -> dict:
    common = rules_config.get("common") if isinstance(rules_config.get("common"), dict) else {}
    type_rules = rules_config.get(request_type) if isinstance(rules_config.get(request_type), dict) else {}
    return {**common, **type_rules}


def _line_item_product_id(item: dict) -> str | None:
    product_id = item.get("product_id") or item.get("productId")
    if not product_id:
        product = item.get("product") if isinstance(item.get("product"), dict) else {}
        product_id = product.get("id")
    if not product_id:
        return None
    match = re.search(r"(\d+)$", str(product_id))
    return match.group(1) if match else str(product_id)


def _line_item_tags(item: dict) -> set[str]:
    product = item.get("product") if isinstance(item.get("product"), dict) else {}
    return _split_tags(item.get("tags") or product.get("tags"))


def _line_item_product_type(item: dict) -> str:
    product = item.get("product") if isinstance(item.get("product"), dict) else {}
    return _normalize_text(
        item.get("product_type")
        or item.get("productType")
        or item.get("category")
        or product.get("product_type")
        or product.get("productType")
        or product.get("category")
    )


def _line_item_identifier_values(item: dict) -> set[str]:
    product = item.get("product") if isinstance(item.get("product"), dict) else {}
    variant = item.get("variant") if isinstance(item.get("variant"), dict) else {}
    values = {
        item.get("id"),
        item.get("line_item_id"),
        item.get("product_id"),
        item.get("variant_id"),
        item.get("sku"),
        item.get("title"),
        product.get("id"),
        variant.get("id"),
    }
    return {_normalize_text(value) for value in values if _normalize_text(value)}


def _selected_line_items(order: dict, selected_line_items: list[dict] | None) -> list[dict]:
    line_items = [item for item in (order.get("line_items") or []) if isinstance(item, dict)]
    if not selected_line_items:
        return line_items
    selected_values: set[str] = set()
    for selection in selected_line_items:
        if not isinstance(selection, dict):
            continue
        selected_values.update(
            _normalize_text(value)
            for value in (
                selection.get("line_item_id"),
                selection.get("product_id"),
                selection.get("variant_id"),
                selection.get("sku"),
                selection.get("title"),
            )
            if _normalize_text(value)
        )
    if not selected_values:
        return line_items
    return [
        item
        for item in line_items
        if _line_item_identifier_values(item).intersection(selected_values)
    ]


def _has_loyalty_exemption(order: dict, type_rules: dict) -> bool:
    loyalty = type_rules.get("loyalty_exemption")
    if not isinstance(loyalty, dict) or not _as_bool(loyalty.get("enabled"), default=False):
        return False
    tags = _split_tags(order.get("tags"))
    customer = order.get("customer") if isinstance(order.get("customer"), dict) else {}
    tags.update(_split_tags(customer.get("tags")))
    required = _split_tags(loyalty.get("customer_tags") or loyalty.get("tags"))
    return bool(required and tags.intersection(required))


def _is_already_returned_item(order: dict) -> bool:
    status_values: list[str] = []
    for key in (
        "request_type",
        "request_status",
        "status",
        "return_status",
        "reverse_status",
        "shipment_status",
        "partner_status",
    ):
        value = order.get(key)
        if value not in (None, ""):
            status_values.append(str(value).strip().lower())

    for checkpoint in ("received", "inspected", "refunded", "archived"):
        value = order.get(checkpoint)
        if isinstance(value, dict) and value.get("status"):
            status_values.append(checkpoint)
        elif value is True:
            status_values.append(checkpoint)

    if any(
        value in {"return", "returned", "received", "inspected", "refunded", "archived"}
        or "returned" in value
        or "refund" in value
        for value in status_values
    ):
        return True

    tags = _split_tags(order.get("tags"))
    for item in order.get("line_items") or []:
        if isinstance(item, dict):
            tags.update(_line_item_tags(item))
    return bool(tags.intersection({"returned", "return-complete", "return_complete", "refunded"}))


def _is_delivered_order(order: dict, order_service: Any | None = None) -> bool:
    if order_service and hasattr(order_service, "filter_delivered_orders"):
        try:
            return bool(order_service.filter_delivered_orders([order]))
        except Exception:
            pass

    delivered_statuses = {"delivered", "fulfilled"}
    for key in ("shipment_status", "partner_status", "fulfillment_status", "status"):
        if str(order.get(key) or "").strip().lower() in delivered_statuses:
            return True

    fulfillments = order.get("fulfillments") or []
    if isinstance(fulfillments, list):
        for fulfillment in fulfillments:
            if not isinstance(fulfillment, dict):
                continue
            status = str(
                fulfillment.get("shipment_status")
                or fulfillment.get("delivery_status")
                or fulfillment.get("status")
                or ""
            ).strip().lower()
            if status in delivered_statuses:
                return True
    return False


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(text[:10], "%Y-%m-%d")
            except ValueError:
                return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def _adelivery_datetime(
    order: dict,
    *,
    client_id: str | None = None,
    order_number: str | None = None,
    state: dict | None = None,
) -> datetime | None:
    """Real delivery timestamp for a Shopify order.

    Prefers the courier's own tracking timeline (``shipment_status_history``,
    populated by the Shiprocket/Delhivery webhooks) — the only source that
    records *when* delivery actually happened, rather than when some order
    or fulfillment field was last written. Shopify's own "updated_at"/
    "closed_at" fields are last-modified timestamps, not delivery
    timestamps: they can drift forward on any later, unrelated write (a
    tracking resync, a note, a tag) long after the real delivery event, even
    on a fulfillment that is genuinely marked delivered — which is exactly
    how a long-delivered order can look freshly delivered against a
    return/exchange window. They're kept as a fallback only for
    orders/clients with no courier tracking history recorded (e.g. no
    logistics integration), and even then only when there is a confirmed
    "delivered" status alongside the timestamp.
    """
    if client_id and order_number:
        confirmed = await aget_confirmed_delivery_datetime(client_id, order_number, state=state)
        if confirmed:
            return confirmed

    candidates: list[Any] = []
    for fulfillment in order.get("fulfillments") or []:
        if not isinstance(fulfillment, dict):
            continue
        candidates.append(fulfillment.get("delivered_at"))
        candidates.append(fulfillment.get("delivery_date"))
        status = str(
            fulfillment.get("shipment_status")
            or fulfillment.get("delivery_status")
            or fulfillment.get("status")
            or ""
        ).strip().lower()
        if status == "delivered":
            candidates.append(fulfillment.get("updated_at"))

    candidates.append(order.get("delivered_at"))
    if _is_delivered_order(order):
        candidates.append(order.get("closed_at"))
        candidates.append(order.get("updated_at"))

    for candidate in candidates:
        parsed = _parse_datetime(candidate)
        if parsed:
            return parsed
    return None


async def _aget_product_tags(client_id: str, product_ids: set[str]) -> dict[str, set[str]]:
    if not product_ids:
        return {}
    shopify_config = await aget_shopify_config(client_id=client_id)
    shop_url = shopify_config.get("shop_url")
    access_token = shopify_config.get("access_token")
    api_version = shopify_config.get("api_version", "2024-04")
    if not shop_url or not access_token:
        return {}

    http = await get_shared_async_http_client()
    limiter = get_shopify_rate_limiter()
    headers = {"X-Shopify-Access-Token": access_token, "Accept": "application/json"}
    tags_by_product: dict[str, set[str]] = {}

    for product_id in product_ids:
        await limiter.acquire(shop_url)
        response = await http.get(
            f"https://{shop_url}/admin/api/{api_version}/products/{product_id}.json",
            params={"fields": "id,tags"},
            headers=headers,
            timeout=15,
        )
        if response.status_code != 200:
            logger.warning(
                "[RETURN_RULES] product tag lookup failed client_id=%s product_id=%s status=%s",
                client_id,
                product_id,
                response.status_code,
            )
            continue
        product = response.json().get("product") or {}
        tags_by_product[product_id] = _split_tags(product.get("tags"))
    return tags_by_product


async def _aget_order(client_id: str, order_number: str, state: dict | None) -> tuple[dict, Any | None]:
    from fashion_bot.core.factory import ServiceFactory

    order_service = await ServiceFactory.aget_order_service(
        client_id=client_id,
        state=state,
        vendor="shopify",
    )
    order = await order_service.aget_order_details(order_number, state=state)
    return (order or {}, order_service)


async def avalidate_return_exchange_request(
    *,
    client_id: str,
    order_number: str,
    request_type: str | None,
    state: dict | None = None,
    order: dict | None = None,
    selected_line_items: list[dict] | None = None,
    return_reason: str | None = None,
    proof_provided: bool | None = None,
    tag_intact_confirmed: bool | None = None,
    desired_resolution: str | None = None,
) -> dict:
    """Validate an order against tenant return/exchange rules.

    A missing rules config preserves current behavior and returns ``valid=True``.
    """

    rules_config = await aget_json_config(RETURN_EXCHANGE_RULES_CONFIG_KEY, client_id=client_id) or {}
    if not rules_config:
        return {
            "success": True,
            "valid": True,
            "validation_applied": False,
            "message": "No return/exchange rules configured.",
        }

    normalized_type = _normalize_request_type(request_type)
    type_rules = _merge_type_rules(rules_config, normalized_type) if isinstance(rules_config, dict) else {}

    window_days = _as_int(type_rules.get("window_days"))
    if window_days == 0:
        return {
            "success": True,
            "valid": False,
            "validation_applied": True,
            "request_type": normalized_type,
            "message": f"{normalized_type.title()} requests are disabled for this client.",
            "failed_rules": ["window_disabled"],
        }

    fetched_order_service = None
    order_data = order or {}
    if not order_data:
        try:
            order_data, fetched_order_service = await _aget_order(client_id, order_number, state)
        except Exception as exc:
            log_with_trace_id(
                state,
                f"[RETURN_RULES] order lookup failed order={order_number}: {exc}",
                "error",
            )
            return {
                "success": True,
                "valid": False,
                "validation_applied": True,
                "request_type": normalized_type,
                "message": "I couldn't verify this order for return/exchange right now. Please contact support.",
                "failed_rules": ["order_lookup_failed"],
                "error": str(exc),
            }

    if not order_data:
        return {
            "success": True,
            "valid": False,
            "validation_applied": True,
            "request_type": normalized_type,
            "message": f"I couldn't find order {order_number} to validate this {normalized_type}.",
            "failed_rules": ["order_not_found"],
        }

    failed_rules: list[str] = []
    needs_input: list[str] = []
    details: dict[str, Any] = {"order_name": order_data.get("name") or order_number}
    selected_items = _selected_line_items(order_data, selected_line_items)
    details["selected_item_count"] = len(selected_items)
    if selected_line_items and not selected_items:
        failed_rules.append("selected_item_not_found")

    loyalty_exempt = _has_loyalty_exemption(order_data, type_rules)
    details["loyalty_exempt"] = loyalty_exempt

    if normalized_type == "exchange" and _is_already_returned_item(order_data):
        failed_rules.append("already_returned_item")

    require_delivered = _as_bool(type_rules.get("require_delivered"), default=False)
    delivered = _is_delivered_order(order_data, fetched_order_service)
    details["delivered"] = delivered
    if require_delivered and not delivered:
        failed_rules.append("order_not_delivered")

    if window_days is not None and window_days > 0 and not loyalty_exempt:
        delivered_at = await _adelivery_datetime(
            order_data, client_id=client_id, order_number=order_number, state=state,
        )
        details["window_days"] = window_days
        details["delivered_at"] = delivered_at.isoformat() if delivered_at else None
        if not delivered_at:
            failed_rules.append("delivery_date_missing")
        else:
            days_since_delivery = (datetime.now(timezone.utc) - delivered_at).days
            details["days_since_delivery"] = days_since_delivery
            if days_since_delivery > window_days:
                failed_rules.append("outside_window")

    blocked_tags = _split_tags(type_rules.get("blocked_product_tags"))
    if blocked_tags:
        line_items = selected_items
        observed_tags: set[str] = set()
        product_ids: set[str] = set()
        for item in line_items:
            if not isinstance(item, dict):
                continue
            observed_tags.update(_line_item_tags(item))
            product_id = _line_item_product_id(item)
            if product_id:
                product_ids.add(product_id)
        missing_product_ids = {
            product_id
            for product_id in product_ids
            if not any(_line_item_product_id(item) == product_id and _line_item_tags(item) for item in line_items if isinstance(item, dict))
        }
        fetched_tags = await _aget_product_tags(client_id, missing_product_ids)
        for tags in fetched_tags.values():
            observed_tags.update(tags)
        matched_tags = sorted(observed_tags.intersection(blocked_tags))
        details["blocked_product_tags"] = sorted(blocked_tags)
        details["matched_blocked_product_tags"] = matched_tags
        if matched_tags:
            failed_rules.append("blocked_product_tag")

    blocked_categories = {
        _normalize_text(value)
        for value in _as_list(type_rules.get("blocked_product_categories") or type_rules.get("blocked_product_types"))
        if _normalize_text(value)
    }
    if blocked_categories:
        matched_categories = sorted(
            {
                product_type
                for item in selected_items
                if (product_type := _line_item_product_type(item)) in blocked_categories
            }
        )
        details["blocked_product_categories"] = sorted(blocked_categories)
        details["matched_blocked_product_categories"] = matched_categories
        if matched_categories and not loyalty_exempt:
            failed_rules.append("blocked_product_category")

    reason_text = _normalize_text(return_reason)
    details["return_reason"] = return_reason
    allowed_reasons = {_normalize_text(value) for value in _as_list(type_rules.get("allowed_reasons")) if _normalize_text(value)}
    if allowed_reasons and reason_text and reason_text not in allowed_reasons:
        failed_rules.append("reason_not_allowed")

    defect_only = _as_bool(type_rules.get("defect_only"), default=False)
    if defect_only:
        defect_keywords = {
            _normalize_text(value)
            for value in _as_list(type_rules.get("defect_reason_keywords") or ["defect", "damaged", "wrong", "missing", "broken"])
        }
        if not reason_text or not any(keyword in reason_text for keyword in defect_keywords):
            failed_rules.append("defect_reason_required")

    proof_required = _as_bool(type_rules.get("proof_required"), default=False)
    proof_reasons = {
        _normalize_text(value)
        for value in _as_list(type_rules.get("proof_required_reasons"))
        if _normalize_text(value)
    }
    reason_requires_proof = proof_required or (
        bool(proof_reasons) and any(reason in reason_text for reason in proof_reasons)
    )
    if reason_requires_proof and proof_provided is not True:
        needs_input.append("proof_required")

    if _as_bool(type_rules.get("tag_intact_required"), default=False) and tag_intact_confirmed is not True:
        needs_input.append("tag_intact_confirmation_required")

    if normalized_type == "exchange":
        exchange_timing = type_rules.get("exchange_creation_policy") or type_rules.get("creation_policy")
        if exchange_timing:
            details["exchange_creation_policy"] = exchange_timing
        stock_required = _as_bool(type_rules.get("require_exchange_stock"), default=False)
        if stock_required:
            desired = _normalize_text(desired_resolution)
            if desired and "exchange" not in desired:
                failed_rules.append("exchange_resolution_required")

    refund_rules = type_rules.get("refund") if isinstance(type_rules.get("refund"), dict) else {}
    details["refund"] = {
        "destination": refund_rules.get("default_destination") or type_rules.get("refund_destination"),
        "return_fee_flat": refund_rules.get("return_fee_flat") or type_rules.get("return_fee_flat"),
        "visibility_mode": refund_rules.get("visibility_mode"),
    }

    if needs_input:
        return {
            "success": True,
            "valid": False,
            "needs_customer_input": True,
            "validation_applied": True,
            "request_type": normalized_type,
            "message": _input_message(normalized_type, needs_input),
            "failed_rules": failed_rules,
            "needs_input": needs_input,
            "details": details,
        }

    if failed_rules:
        message = _failure_message(normalized_type, failed_rules, details)
        return {
            "success": True,
            "valid": False,
            "validation_applied": True,
            "request_type": normalized_type,
            "message": message,
            "failed_rules": failed_rules,
            "details": details,
        }

    return {
        "success": True,
        "valid": True,
        "validation_applied": True,
        "request_type": normalized_type,
        "message": f"Order is eligible for {normalized_type}.",
        "details": details,
    }


def _failure_message(request_type: str, failed_rules: list[str], details: dict[str, Any]) -> str:
    label = "exchange" if request_type == "exchange" else "return"
    if "already_returned_item" in failed_rules:
        return "This item has already been returned, so it cannot be exchanged."
    if "selected_item_not_found" in failed_rules:
        return f"I couldn't match the selected item to this order, so I can't start the {label} yet."
    if "order_not_delivered" in failed_rules:
        return f"This order is not eligible for {label} yet because it is not marked delivered."
    if "outside_window" in failed_rules:
        return (
            f"This order is outside the {details.get('window_days')}-day {label} window, "
            f"so it is not eligible for {label}."
        )
    if "delivery_date_missing" in failed_rules:
        return f"I couldn't verify the delivery date for this order, so I can't start the {label} right now."
    if "blocked_product_tag" in failed_rules:
        tags = ", ".join(details.get("matched_blocked_product_tags") or [])
        return f"This item is not eligible for {label} because it matches a restricted product tag: {tags}."
    if "blocked_product_category" in failed_rules:
        categories = ", ".join(details.get("matched_blocked_product_categories") or [])
        return f"This item is not eligible for {label} because {categories} products are restricted by policy."
    if "reason_not_allowed" in failed_rules:
        return f"This reason is not eligible for {label} based on the configured policy."
    if "defect_reason_required" in failed_rules:
        return f"This item is eligible for {label} only for defect, damage, wrong-item, or similar issue reasons."
    if "exchange_resolution_required" in failed_rules:
        return "Please confirm the exchange item/variant before I proceed with exchange eligibility."
    return f"This order is not eligible for {label} based on the configured return/exchange rules."


def _input_message(request_type: str, needs_input: list[str]) -> str:
    label = "exchange" if request_type == "exchange" else "return"
    if "proof_required" in needs_input and "tag_intact_confirmation_required" in needs_input:
        return f"Please share proof images and confirm the product tag is intact before I can start this {label}."
    if "proof_required" in needs_input:
        return f"Please share the required proof images before I can start this {label}."
    if "tag_intact_confirmation_required" in needs_input:
        return f"Please confirm the product tag is intact before I can start this {label}."
    return f"I need a little more information before I can start this {label}."
