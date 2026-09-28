"""Return Prime-specific eligibility validation.

This reads the raw Return Prime rules copied into
``client_configs.return_prime_return_exchange_rules``. It intentionally applies
only before starting a new Return Prime return/exchange, not for status,
pickup, or refund reads.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fashion_bot.config_manager import aget_json_config
from fashion_bot.return_partners.rules import (
    _adelivery_datetime,
    _aget_order,
    _aget_product_tags,
    _is_delivered_order,
    _line_item_product_id,
    _line_item_tags,
    _normalize_request_type,
    _normalize_text,
    _parse_datetime,
    _selected_line_items,
    _split_tags,
)
from fashion_bot.utils.utils import log_with_trace_id

RETURN_PRIME_RULES_CONFIG_KEY = "return_prime_return_exchange_rules"


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


def _as_list(value: Any) -> list:
    if value in (None, ""):
        return []
    return value if isinstance(value, list) else [value]


def _discount_codes(order: dict) -> set[str]:
    codes: set[str] = set()
    for item in _as_list(order.get("discount_codes")):
        if isinstance(item, dict):
            code = item.get("code")
        else:
            code = item
        if _normalize_text(code):
            codes.add(_normalize_text(code))
    for item in _as_list(order.get("discount_applications")):
        if isinstance(item, dict):
            for key in ("code", "title", "discount_code"):
                if _normalize_text(item.get(key)):
                    codes.add(_normalize_text(item.get(key)))
        elif _normalize_text(item):
            codes.add(_normalize_text(item))
    return codes


def _blocked_created_window(value: Any) -> tuple[datetime | None, datetime | None]:
    if not value:
        return None, None
    if isinstance(value, dict):
        start = value.get("start") or value.get("from") or value.get("created_at_min")
        end = value.get("end") or value.get("to") or value.get("created_at_max")
        return _parse_datetime(start), _parse_datetime(end)
    if isinstance(value, list) and len(value) >= 2:
        return _parse_datetime(value[0]), _parse_datetime(value[1])
    return None, None


def _created_at(order: dict) -> datetime | None:
    return _parse_datetime(order.get("created_at") or order.get("createdAt") or order.get("order_date"))


def _line_matches_blocked_tags(line_items: list[dict], blocked_tags: set[str], match_mode: str) -> list[str]:
    if not blocked_tags:
        return []
    per_item_matches: list[set[str]] = []
    for item in line_items:
        tags = _line_item_tags(item)
        per_item_matches.append(tags.intersection(blocked_tags))
    if not per_item_matches:
        return []
    normalized_match_mode = str(match_mode or "any").strip().lower()
    if normalized_match_mode == "all":
        if all(matches for matches in per_item_matches):
            return sorted(set().union(*per_item_matches))
        return []
    return sorted(set().union(*(matches for matches in per_item_matches if matches)))


async def avalidate_return_prime_rules(
    *,
    client_id: str,
    order_number: str,
    request_type: str | None,
    state: dict | None = None,
    order: dict | None = None,
    selected_line_items: list[dict] | None = None,
) -> dict:
    rules_config = await aget_json_config(RETURN_PRIME_RULES_CONFIG_KEY, client_id=client_id) or {}
    if not rules_config:
        return {
            "success": True,
            "valid": True,
            "validation_applied": False,
            "source": RETURN_PRIME_RULES_CONFIG_KEY,
            "message": "No Return Prime return/exchange rules configured.",
        }

    normalized_type = _normalize_request_type(request_type)
    type_rules = rules_config.get(normalized_type) if isinstance(rules_config.get(normalized_type), dict) else {}
    if not type_rules:
        return {
            "success": True,
            "valid": True,
            "validation_applied": False,
            "source": RETURN_PRIME_RULES_CONFIG_KEY,
            "request_type": normalized_type,
            "message": f"No Return Prime {normalized_type} rules configured.",
        }

    fetched_order_service = None
    order_data = order or {}
    if not order_data:
        try:
            order_data, fetched_order_service = await _aget_order(client_id, order_number, state)
        except Exception as exc:
            log_with_trace_id(state, f"[RETURN_PRIME_RULES] order lookup failed order={order_number}: {exc}", "error")
            return {
                "success": True,
                "valid": False,
                "validation_applied": True,
                "source": RETURN_PRIME_RULES_CONFIG_KEY,
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
            "source": RETURN_PRIME_RULES_CONFIG_KEY,
            "request_type": normalized_type,
            "message": f"I couldn't find order {order_number} to validate this {normalized_type}.",
            "failed_rules": ["order_not_found"],
        }

    failed_rules: list[str] = []
    needs_input: list[str] = []
    details: dict[str, Any] = {
        "source": RETURN_PRIME_RULES_CONFIG_KEY,
        "order_name": order_data.get("name") or order_number,
        "request_type": normalized_type,
    }

    if _as_bool(type_rules.get("enabled"), default=True) is False:
        failed_rules.append(f"{normalized_type}_disabled")

    selected_items = _selected_line_items(order_data, selected_line_items)
    details["selected_item_count"] = len(selected_items)
    multiple_cfg = rules_config.get("multiple_item_returns") if isinstance(rules_config.get("multiple_item_returns"), dict) else {}
    if _as_bool(multiple_cfg.get("enabled"), default=True) is False and len(selected_items) > 1:
        if selected_line_items:
            failed_rules.append("multiple_item_returns_disabled")
        else:
            needs_input.append("item_selection_required")

    require_delivered = _as_bool(type_rules.get("require_delivered"), default=False)
    delivered = _is_delivered_order(order_data, fetched_order_service)
    details["delivered"] = delivered
    if require_delivered and not delivered:
        failed_rules.append("order_not_delivered")

    window_days = _as_int(type_rules.get("window_days"))
    if window_days is not None:
        details["window_days"] = window_days
        if window_days <= 0:
            failed_rules.append("window_disabled")
        else:
            delivered_at = await _adelivery_datetime(
                order_data, client_id=client_id, order_number=order_number, state=state,
            )
            details["delivered_at"] = delivered_at.isoformat() if delivered_at else None
            if not delivered_at:
                failed_rules.append("delivery_date_missing")
            else:
                days_since_delivery = (datetime.now(timezone.utc) - delivered_at).days
                details["days_since_delivery"] = days_since_delivery
                if days_since_delivery > window_days:
                    failed_rules.append("outside_window")

    blocked_codes = {_normalize_text(code) for code in _as_list(type_rules.get("blocked_discount_codes")) if _normalize_text(code)}
    if blocked_codes:
        matched_codes = sorted(_discount_codes(order_data).intersection(blocked_codes))
        details["blocked_discount_codes"] = sorted(blocked_codes)
        details["matched_blocked_discount_codes"] = matched_codes
        if matched_codes:
            failed_rules.append("blocked_discount_code")

    start, end = _blocked_created_window(type_rules.get("blocked_order_created_between"))
    if start or end:
        created_at = _created_at(order_data)
        details["blocked_order_created_between"] = {
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "order_created_at": created_at.isoformat() if created_at else None,
        }
        if created_at and (start is None or created_at >= start) and (end is None or created_at <= end):
            failed_rules.append("blocked_order_created_between")

    blocked_tags = _split_tags(type_rules.get("blocked_product_tags"))
    if blocked_tags and selected_items:
        product_ids = {
            product_id
            for item in selected_items
            if isinstance(item, dict) and (product_id := _line_item_product_id(item))
        }
        observed_items = []
        fetched_tags = await _aget_product_tags(client_id, product_ids)
        for item in selected_items:
            if not isinstance(item, dict):
                continue
            tags = set(_line_item_tags(item))
            product_id = _line_item_product_id(item)
            if product_id:
                tags.update(fetched_tags.get(product_id, set()))
            observed_items.append({"tags": list(tags)})
        matched_tags = _line_matches_blocked_tags(observed_items, blocked_tags, str(type_rules.get("match_mode") or "any"))
        details["blocked_product_tags"] = sorted(blocked_tags)
        details["matched_blocked_product_tags"] = matched_tags
        if matched_tags:
            failed_rules.append("blocked_product_tag")

    if needs_input:
        return {
            "success": True,
            "valid": False,
            "needs_customer_input": True,
            "validation_applied": True,
            "source": RETURN_PRIME_RULES_CONFIG_KEY,
            "request_type": normalized_type,
            "message": "Please confirm which item you want to return/exchange.",
            "failed_rules": failed_rules,
            "needs_input": needs_input,
            "details": details,
        }

    if failed_rules:
        return {
            "success": True,
            "valid": False,
            "validation_applied": True,
            "source": RETURN_PRIME_RULES_CONFIG_KEY,
            "request_type": normalized_type,
            "message": _failure_message(normalized_type, failed_rules, details),
            "failed_rules": failed_rules,
            "details": details,
        }

    return {
        "success": True,
        "valid": True,
        "validation_applied": True,
        "source": RETURN_PRIME_RULES_CONFIG_KEY,
        "request_type": normalized_type,
        "message": f"Order is eligible for Return Prime {normalized_type}.",
        "details": details,
    }


def _failure_message(request_type: str, failed_rules: list[str], details: dict[str, Any]) -> str:
    label = "exchange" if request_type == "exchange" else "return"
    if f"{request_type}_disabled" in failed_rules or "window_disabled" in failed_rules:
        return f"{label.title()} requests are disabled by policy."
    if "order_not_delivered" in failed_rules:
        return f"This order is not eligible for {label} yet because it is not marked delivered."
    if "outside_window" in failed_rules:
        return f"This order is outside the {details.get('window_days')}-day {label} window, so it is not eligible for {label}."
    if "delivery_date_missing" in failed_rules:
        return f"I couldn't verify the delivery date for this order, so I can't start the {label} right now."
    if "blocked_product_tag" in failed_rules:
        tags = ", ".join(details.get("matched_blocked_product_tags") or [])
        return f"This item is not eligible for {label} because it matches a restricted Return Prime product tag: {tags}."
    if "blocked_discount_code" in failed_rules:
        codes = ", ".join(details.get("matched_blocked_discount_codes") or [])
        return f"This order is not eligible for {label} because discount code {codes} is restricted by policy."
    if "blocked_order_created_between" in failed_rules:
        return f"This order is not eligible for {label} because its order date falls in a restricted policy window."
    if "multiple_item_returns_disabled" in failed_rules:
        return f"Multiple-item {label}s are disabled by policy. Please select one item."
    return f"This order is not eligible for {label} based on Return Prime rules."
