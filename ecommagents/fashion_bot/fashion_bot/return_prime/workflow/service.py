"""Workflow helpers for Return Prime lookups and webhook processing."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Any

from fashion_bot.return_prime.adapter.client import return_prime_service
from fashion_bot.config_manager import aget_json_config, aget_shopify_config
from fashion_bot.env_loader import get_env, get_int
from fashion_bot.return_prime.db import db
from fashion_bot.return_prime.shopify_lookup import fetch_order_by_name
from fashion_bot.return_prime.webhook.service import return_prime_webhook_service
from fashion_bot.return_prime.workflow.constants import NO_REQUEST_MESSAGE
from fashion_bot.utils.phone_number_utils import normalize_phone_number
from fashion_bot.utils.redis_client import get_shared_async_redis_client
from fashion_bot.utils.tiered_cache import aget_with_tiered_cache
logger = logging.getLogger(__name__)

RETURN_PRIME_LIST_CACHE_TTL_SECONDS = get_int("RETURN_PRIME_LIST_CACHE_TTL_SECONDS", 600)


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _coerce_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _coerce_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _first_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return {}


def _digits(value: Any) -> str:
    # Shared digit-stripping rule (see phone_number_utils); the str() coercion is
    # kept here because webhook payloads may carry numeric phone fields.
    return normalize_phone_number(str(value or ""))


# Return Prime's by-id endpoint accepts only its internal 24-char hex ObjectId.
# Anything else (e.g. the customer-facing "RET777") is rejected with HTTP 412
# "Invalid id", so the identifier shape decides which endpoint we can use.
_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{24}$", re.IGNORECASE)


def _is_object_id(value: Any) -> bool:
    return bool(_OBJECT_ID_RE.match(str(value or "").strip()))


class ReturnPrimeWorkflowService:
    """Normalize inputs and outputs around the Return Prime adapter."""

    def _sanitize_adapter_failure(
        self,
        *,
        order_name: str | None = None,
        request_id: str | None = None,
        status_code: int | None = None,
        message: str,
    ) -> dict:
        safe_status_code = status_code if isinstance(status_code, int) and status_code > 0 else 502
        safe_status_code = max(safe_status_code, 502) if safe_status_code < 500 else safe_status_code
        result = {
            "success": False,
            "status_code": safe_status_code,
            "message": message,
        }
        if order_name is not None:
            result["order_name"] = order_name
        if request_id is not None:
            result["request_id"] = request_id
        return result

    def normalize_order_name(self, order_number: str) -> str:
        cleaned = (order_number or "").strip()
        if not cleaned:
            return ""
        return cleaned if cleaned.startswith("#") else f"#{cleaned}"

    def _normalize_store_value(self, store: str | None) -> str:
        cleaned = (store or "").strip()
        if not cleaned:
            return ""
        if "://" not in cleaned and "/" not in cleaned:
            return cleaned
        parsed = urlparse(cleaned if "://" in cleaned else f"https://{cleaned}")
        return (parsed.netloc or parsed.path or "").strip().strip("/")

    async def _resolve_return_prime_portal_config(
        self,
        client_id: str,
        *,
        store: str | None = None,
        channel_id: str | None = None,
    ) -> dict:
        return_prime_details = await aget_json_config("return_prime_details", client_id=client_id) or {}
        shopify_details = await aget_shopify_config(client_id=client_id) or {}
        config_store = self._normalize_store_value(
            _first_non_empty(
                return_prime_details.get("store"),
                return_prime_details.get("shopify_store_url"),
                return_prime_details.get("shopify_domain"),
                return_prime_details.get("SHOPIFY_DOMAIN"),
                shopify_details.get("SHOPIFY_DOMAIN"),
                shopify_details.get("shop_url"),
                get_env("RETURN_PRIME_STORE"),
            )
        )
        resolved_store = self._normalize_store_value(
            _first_non_empty(
                store,
                config_store,
            )
        )
        config_channel_id = str(
            _first_non_empty(
                return_prime_details.get("channel_id"),
                get_env("RETURN_PRIME_CHANNEL_ID"),
            )
            or ""
        ).strip()
        resolved_channel_id = str(
            _first_non_empty(
                channel_id,
                config_channel_id,
            )
            or ""
        ).strip()
        portal_url = str(
            _first_non_empty(
                return_prime_details.get("portal_url"),
                return_prime_details.get("return_portal_url"),
                return_prime_details.get("return_exchange_portal_url"),
                get_env("RETURN_PRIME_PORTAL_URL"),
            )
            or ""
        ).strip()
        return {
            "store": resolved_store,
            "channel_id": resolved_channel_id,
            "portal_url": portal_url,
            "return_prime_details": return_prime_details,
            "shopify_details": shopify_details,
        }

    def _extract_request_list(self, payload: Any) -> list[dict]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            if self._is_request_payload(payload):
                return [payload]
            request = payload.get("request")
            if isinstance(request, dict):
                return [request]
            for key in ("data", "list", "requests", "results", "items"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
                if isinstance(value, dict):
                    requests = self._extract_request_list(value)
                    if requests:
                        return requests
            nested = payload.get("data")
            if isinstance(nested, dict):
                request = nested.get("request")
                if isinstance(request, dict):
                    return [request]
                for key in ("list", "requests", "results", "items"):
                    value = nested.get(key)
                    if isinstance(value, list):
                        return [item for item in value if isinstance(item, dict)]
        return []

    def _extract_request_object(self, payload: Any) -> dict:
        if isinstance(payload, dict):
            if self._is_request_payload(payload):
                return payload
            for key in ("data", "request", "result"):
                value = payload.get(key)
                if isinstance(value, dict):
                    request = self._extract_request_object(value)
                    if request:
                        return request
        return {}

    def _is_request_payload(self, payload: dict) -> bool:
        return any(key in payload for key in ("id", "request_number", "request_type")) or (
            "status" in payload and any(key in payload for key in ("order", "customer", "line_items"))
        )

    def _normalize_line_item(self, item: dict) -> dict:
        original = _coerce_dict(item.get("original_product")) or _coerce_dict(item.get("product"))
        exchange = _coerce_dict(item.get("exchange_product")) or _coerce_dict(item.get("exchange"))
        refund = _coerce_dict(item.get("refund"))
        shipping = _first_dict(item.get("shipping"))
        return {
            "original_product": _first_non_empty(
                item.get("title"),
                original.get("title"),
                original.get("name"),
                item.get("product_title"),
            ),
            "original_variant": _first_non_empty(
                item.get("variant_title"),
                original.get("variant"),
                original.get("variant_title"),
            ),
            "exchange_product": _first_non_empty(
                exchange.get("title"),
                exchange.get("name"),
                item.get("exchange_product_title"),
            ),
            "exchange_variant": _first_non_empty(
                exchange.get("variant"),
                exchange.get("variant_title"),
                item.get("exchange_variant_title"),
            ),
            "quantity": item.get("quantity"),
            "reason": _first_non_empty(item.get("reason"), _coerce_dict(item.get("reason_details")).get("label")),
            "refund": refund or item.get("refund_amount"),
            "return_fee": _first_non_empty(item.get("return_fee"), item.get("fees")),
            "exchange_fee": item.get("exchange_fee"),
            "shipping": shipping or item.get("shipping_amount"),
            "awb": _first_non_empty(
                shipping.get("awb"),
                shipping.get("tracking_number"),
                shipping.get("waybill"),
                shipping.get("tracking_id"),
            ),
            "tracking_url": _first_non_empty(
                shipping.get("tracking_url"),
                shipping.get("tracking_link"),
                shipping.get("track_url"),
            ),
            "shipping_company": _first_non_empty(
                shipping.get("shipping_company"),
                shipping.get("carrier"),
                shipping.get("courier"),
                shipping.get("tracking_company"),
            ),
            "shipment_status": _first_non_empty(
                shipping.get("shipment_status"),
                shipping.get("status"),
                shipping.get("delivery_status"),
            ),
            "exchange_order": _coerce_dict(_coerce_dict(item.get("exchange")).get("order")),
        }

    def _checkpoint_status(self, request: dict, key: str) -> bool:
        value = request.get(key)
        if isinstance(value, dict):
            return bool(value.get("status"))
        return bool(value) or request.get("status") == key

    def normalize_request(self, raw_request: dict) -> dict:
        request = _coerce_dict(raw_request)
        order = _coerce_dict(request.get("order"))
        customer = _coerce_dict(request.get("customer"))
        rejected = _coerce_dict(request.get("rejected"))
        exchange = _coerce_dict(request.get("exchange"))
        line_items = [
            self._normalize_line_item(item)
            for item in _coerce_list(
                _first_non_empty(request.get("line_items"), request.get("items"), request.get("products"), [])
            )
            if isinstance(item, dict)
        ]
        line_exchange_order = _first_non_empty(
            *(line.get("exchange_order") for line in line_items)
        )
        normalized = {
            "request_id": _first_non_empty(request.get("request_id"), request.get("id")),
            "request_number": request.get("request_number"),
            "request_type": request.get("request_type"),
            "status": request.get("status"),
            "order_id": _first_non_empty(order.get("id"), request.get("order_id")),
            "order_name": _first_non_empty(order.get("name"), request.get("order_name")),
            "customer_name": _first_non_empty(customer.get("name"), request.get("customer_name")),
            "customer_email": _first_non_empty(customer.get("email"), request.get("customer_email")),
            "customer_phone": _first_non_empty(customer.get("phone"), request.get("customer_phone")),
            "status_checkpoints": {
                "approved": self._checkpoint_status(request, "approved"),
                "received": self._checkpoint_status(request, "received"),
                "inspected": self._checkpoint_status(request, "inspected"),
                "rejected": self._checkpoint_status(request, "rejected"),
                "archived": self._checkpoint_status(request, "archived"),
            },
            "line_items": line_items,
            "refund": _coerce_dict(request.get("refund")) or request.get("refund_amount"),
            "return_fee": request.get("return_fee"),
            "exchange_fee": request.get("exchange_fee"),
            "shipping": _coerce_dict(request.get("shipping")) or request.get("shipping_amount"),
            "exchange_order": (
                {
                    "id": _first_non_empty(exchange.get("order_id"), _coerce_dict(exchange.get("order")).get("id")),
                    "name": _first_non_empty(exchange.get("order_name"), _coerce_dict(exchange.get("order")).get("name")),
                }
                if request.get("request_type") == "exchange"
                else None
            ),
            "delivery": {
                "status": request.get("delivery_status"),
                "date": request.get("delivery_date"),
            },
            "rejection_comment": rejected.get("comment"),
            "raw": request,
        }
        if request.get("request_type") == "exchange" and line_exchange_order:
            existing_exchange_order = normalized.get("exchange_order") or {}
            normalized["exchange_order"] = {
                "id": _first_non_empty(line_exchange_order.get("id"), existing_exchange_order.get("id")),
                "name": _first_non_empty(line_exchange_order.get("name"), existing_exchange_order.get("name")),
                "status": line_exchange_order.get("exchanged_status"),
                "created_at": line_exchange_order.get("created_at"),
                "raw": line_exchange_order,
            }
        if request.get("request_type") == "exchange" and not normalized["exchange_order"]:
            normalized["exchange_order"] = {"id": None, "name": None}
        return normalized

    async def _resolve_customer_contact(
        self,
        client_id: str,
        order_name: str,
        customer_email: str | None,
        customer_phone: str | None,
    ) -> dict:
        if customer_email or customer_phone:
            return {
                "customer_email": customer_email,
                "customer_phone": customer_phone,
                "shopify_lookup_used": False,
                "shopify_order": None,
            }
        shopify_order = await fetch_order_by_name(client_id, order_name)
        return {
            "customer_email": shopify_order.get("customer_email"),
            "customer_phone": shopify_order.get("customer_phone"),
            "shopify_lookup_used": True,
            "shopify_order": shopify_order,
        }

    async def get_return_portal_link(
        self,
        client_id: str,
        order_number: str,
        *,
        customer_email: str | None = None,
        store: str | None = None,
        channel_id: str | None = None,
    ) -> dict:
        order_name = self.normalize_order_name(order_number)
        if not order_name:
            return {
                "success": False,
                "status_code": 400,
                "order_name": "",
                "customer_email": None,
                "portal_url": None,
                "message": "Missing required order number.",
                "shopify_lookup_used": False,
            }

        resolved_config = await self._resolve_return_prime_portal_config(
            client_id,
            store=store,
            channel_id=channel_id,
        )
        fallback_portal_url = resolved_config.get("portal_url")
        resolved_contact = await self._resolve_customer_contact(
            client_id,
            order_name,
            customer_email=(customer_email or "").strip() or None,
            customer_phone=None,
        )
        resolved_email = (resolved_contact.get("customer_email") or "").strip()
        if not resolved_email:
            if fallback_portal_url:
                return {
                    "success": True,
                    "status_code": 200,
                    "order_name": order_name,
                    "customer_email": None,
                    "portal_url": fallback_portal_url,
                    "message": "Return Prime portal link available. Share this link with the customer to raise a return/exchange request.",
                    "shopify_lookup_used": resolved_contact.get("shopify_lookup_used", False),
                    "portal_mode": "generic",
                }
            return {
                "success": False,
                "status_code": 400,
                "order_name": order_name,
                "customer_email": None,
                "portal_url": None,
                "message": "Missing customer email. Provide customer_email or ensure Shopify order lookup can resolve it.",
                "shopify_lookup_used": resolved_contact.get("shopify_lookup_used", False),
            }

        missing_fields = [
            field_name
            for field_name in ("store", "channel_id")
            if not resolved_config.get(field_name)
        ]
        if missing_fields:
            if fallback_portal_url:
                return {
                    "success": True,
                    "status_code": 200,
                    "order_name": order_name,
                    "customer_email": resolved_email,
                    "portal_url": fallback_portal_url,
                    "message": "Return Prime portal link available. Share this link with the customer to raise a return/exchange request.",
                    "shopify_lookup_used": resolved_contact.get("shopify_lookup_used", False),
                    "portal_mode": "generic",
                    "missing_deep_link_fields": missing_fields,
                }
            return {
                "success": False,
                "status_code": 400,
                "order_name": order_name,
                "customer_email": resolved_email,
                "portal_url": None,
                "message": f"Missing required Return Prime configuration: {', '.join(missing_fields)}.",
                "shopify_lookup_used": resolved_contact.get("shopify_lookup_used", False),
            }

        portal_url = return_prime_service.build_return_portal_link(
            order_number=order_name,
            email=resolved_email,
            store=resolved_config["store"],
            channel_id=resolved_config["channel_id"],
        )
        return {
            "success": True,
            "status_code": 200,
            "order_name": order_name,
            "customer_email": resolved_email,
            "portal_url": portal_url,
            "message": "Return Prime portal link generated. Share this link with the customer to raise a return/exchange request.",
            "shopify_lookup_used": resolved_contact.get("shopify_lookup_used", False),
            "portal_mode": "deep_link",
        }

    def _request_matches_filters(
        self,
        request: dict,
        *,
        order_name: str | None = None,
        customer_email: str | None = None,
        customer_phone: str | None = None,
        request_type: str | None = None,
    ) -> bool:
        normalized = self.normalize_request(request)
        if order_name and str(normalized.get("order_name") or "").lower() != order_name.lower():
            return False
        if request_type and str(normalized.get("request_type") or "").lower() != request_type.lower():
            return False
        if customer_email and str(normalized.get("customer_email") or "").lower() != customer_email.lower():
            return False
        if customer_phone:
            expected = _digits(customer_phone)[-10:]
            actual = _digits(normalized.get("customer_phone"))[-10:]
            if expected and actual != expected:
                return False
        return True

    async def _aget_latest_webhook_rows(
        self,
        *,
        client_id: str,
        order_name: str | None = None,
        customer_phone: str | None = None,
        customer_email: str | None = None,
        request_type: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        conditions = ["client_id = %s"]
        params: list[Any] = [client_id]

        if order_name:
            conditions.append(
                """
                (
                    LOWER(COALESCE(order_number, '')) = LOWER(%s)
                    OR LOWER(COALESCE(payload #>> '{request,order,name}', '')) = LOWER(%s)
                )
                """
            )
            params.extend([order_name, order_name])

        phone_digits = _digits(customer_phone)
        if phone_digits:
            last10 = phone_digits[-10:]
            conditions.append(
                """
                (
                    RIGHT(regexp_replace(COALESCE(customer_phone, ''), '\\D', '', 'g'), 10) = %s
                    OR RIGHT(regexp_replace(COALESCE(payload #>> '{request,customer,phone}', ''), '\\D', '', 'g'), 10) = %s
                )
                """
            )
            params.extend([last10, last10])

        if customer_email:
            conditions.append(
                """
                (
                    LOWER(COALESCE(customer_email, '')) = LOWER(%s)
                    OR LOWER(COALESCE(payload #>> '{request,customer,email}', '')) = LOWER(%s)
                )
                """
            )
            params.extend([customer_email, customer_email])

        if request_type:
            conditions.append(
                """
                (
                    LOWER(COALESCE(request_type, '')) = LOWER(%s)
                    OR LOWER(COALESCE(payload #>> '{request,request_type}', '')) = LOWER(%s)
                )
                """
            )
            params.extend([request_type, request_type])

        query = f"""
            SELECT *
            FROM return_prime_webhook_events
            WHERE {' AND '.join(conditions)}
            ORDER BY received_at DESC, created_at DESC, id DESC
            LIMIT %s
        """
        params.append(limit)
        return await db.postgres.fetch_all(query, tuple(params))

    def _payload_from_webhook_row(self, row: dict) -> dict:
        payload = row.get("payload")
        if isinstance(payload, dict):
            return payload
        return {}

    def _request_from_webhook_row(self, row: dict) -> dict:
        payload = self._payload_from_webhook_row(row)
        request = self._extract_request_object(payload)
        if request:
            return request
        return {
            "id": row.get("request_id") or row.get("return_request_id"),
            "request_number": row.get("request_number"),
            "request_type": row.get("request_type"),
            "status": row.get("request_status"),
            "order": {
                "id": row.get("shopify_order_id"),
                "name": row.get("order_number"),
            },
            "customer": {
                "email": row.get("customer_email"),
                "phone": row.get("customer_phone"),
            },
            "line_items": [
                {
                    "refund": {
                        "status": row.get("refund_status"),
                        "requested_mode": row.get("refund_mode"),
                    },
                    "shipping": [
                        {
                            "awb": row.get("awb"),
                            "shipping_company": row.get("shipping_company"),
                            "shipment_status": row.get("shipment_status"),
                        }
                    ],
                    "exchange": {
                        "order": {
                            "id": row.get("exchange_order_id"),
                            "name": row.get("exchange_order_name"),
                        }
                    },
                    "original_product": {"variant_id": row.get("original_variant_id")},
                    "exchange_product": {"variant_id": row.get("exchange_variant_id")},
                }
            ],
        }

    async def _aget_cached_list_requests(
        self,
        client_id: str,
        *,
        order_name: str | None = None,
        customer_email: str | None = None,
        customer_phone: str | None = None,
        request_type: str | None = None,
    ) -> dict:
        filter_parts = {
            "order_name": order_name or "",
            "customer_email": customer_email or "",
            "customer_phone": _digits(customer_phone)[-10:] if customer_phone else "",
            "request_type": request_type or "",
        }
        cache_key = f"return_prime:list_requests:{client_id}:{json.dumps(filter_parts, sort_keys=True)}"
        source_result: dict[str, Any] = {}

        async def _redis_get() -> dict | None:
            redis_client = await get_shared_async_redis_client()
            if redis_client is None:
                return None
            raw = await redis_client.get(cache_key)
            if not raw:
                return None
            value = json.loads(raw)
            return value if isinstance(value, dict) else None

        async def _redis_set(value: dict) -> None:
            redis_client = await get_shared_async_redis_client()
            if redis_client is None:
                return
            await redis_client.setex(
                cache_key,
                RETURN_PRIME_LIST_CACHE_TTL_SECONDS,
                json.dumps(value, default=str),
            )

        async def _load_from_return_prime() -> dict | None:
            result = await return_prime_service.list_requests(
                client_id,
                order_name=order_name,
                customer_email=customer_email,
                customer_phone=customer_phone,
                request_type=request_type,
            )
            source_result["result"] = result
            return result if result.get("success") else None

        cached_result, cache_source = await aget_with_tiered_cache(
            cache_key=cache_key,
            ttl_seconds=RETURN_PRIME_LIST_CACHE_TTL_SECONDS,
            load_from_source_fn=_load_from_return_prime,
            get_from_redis_fn=_redis_get,
            set_to_redis_fn=_redis_set,
        )
        if cached_result is None:
            return source_result.get("result") or {
                "success": False,
                "status_code": 0,
                "message": "Unable to fetch Return Prime requests right now.",
            }
        return {**cached_result, "cache_source": cache_source}

    async def _aget_live_or_db_request_from_row(self, client_id: str, row: dict) -> tuple[dict, str]:
        request_id = row.get("request_id") or row.get("return_request_id")
        if request_id:
            adapter_result = await return_prime_service.get_request_by_id(client_id, str(request_id))
            if adapter_result.get("success"):
                live_request = self._extract_request_object(adapter_result.get("data"))
                if live_request:
                    return live_request, "return_prime_api"
            logger.warning(
                "Return Prime request-id API failed; using DB fallback client_id=%s request_id=%s result=%s",
                client_id,
                request_id,
                adapter_result,
            )
        return self._request_from_webhook_row(row), "return_prime_webhook_events"

    async def list_requests_by_order_number(
        self,
        client_id: str,
        order_number: str,
        *,
        customer_email: str | None = None,
        customer_phone: str | None = None,
        request_type: str | None = None,
    ) -> dict:
        order_name = self.normalize_order_name(order_number)
        resolved_email = (customer_email or "").strip() or None
        resolved_phone = (customer_phone or "").strip() or None
        db_rows = await self._aget_latest_webhook_rows(
            client_id=client_id,
            order_name=order_name or None,
            customer_phone=resolved_phone,
            customer_email=resolved_email,
            request_type=request_type,
            limit=10,
        )
        if db_rows:
            requests: list[dict] = []
            sources: list[str] = []
            seen_request_ids: set[str] = set()
            for row in db_rows:
                raw_request, source = await self._aget_live_or_db_request_from_row(client_id, row)
                normalized = self.normalize_request(raw_request)
                request_id = str(normalized.get("request_id") or "")
                if request_id and request_id in seen_request_ids:
                    continue
                if request_id:
                    seen_request_ids.add(request_id)
                requests.append(normalized)
                sources.append(source)
            return {
                "success": True,
                "status_code": 200,
                "order_name": order_name,
                "customer_email": resolved_email,
                "customer_phone": resolved_phone,
                "shopify_lookup_used": False,
                "source": "return_prime_api" if "return_prime_api" in sources else "return_prime_webhook_events",
                "requests": requests,
            }

        adapter_result = await self._aget_cached_list_requests(
            client_id,
            order_name=order_name or None,
            customer_email=resolved_email,
            customer_phone=resolved_phone,
            request_type=request_type,
        )
        if not adapter_result.get("success"):
            logger.warning(
                "Return Prime list_requests failed for client_id=%s order_name=%s: %s",
                client_id,
                order_name,
                adapter_result,
            )
            return self._sanitize_adapter_failure(
                order_name=order_name,
                status_code=adapter_result.get("status_code"),
                message="Unable to fetch Return Prime requests right now. Please try again later.",
            )
        requests = [
            self.normalize_request(item)
            for item in self._extract_request_list(adapter_result.get("data"))
            if self._request_matches_filters(
                item,
                order_name=order_name or None,
                customer_email=resolved_email,
                customer_phone=resolved_phone,
                request_type=request_type,
            )
        ]
        return {
            "success": adapter_result.get("success", False),
            "status_code": adapter_result.get("status_code", 200),
            "order_name": order_name,
            "customer_email": resolved_email,
            "customer_phone": resolved_phone,
            "shopify_lookup_used": False,
            "source": (
                "return_prime_list_api"
                if adapter_result.get("cache_source") == "source"
                else f"return_prime_list_cache_{adapter_result.get('cache_source')}"
            ),
            "requests": requests,
        }

    async def get_status_by_order_number(
        self,
        client_id: str,
        order_number: str,
        *,
        customer_email: str | None = None,
        customer_phone: str | None = None,
        request_type: str | None = None,
    ) -> dict:
        result = await self.list_requests_by_order_number(
            client_id,
            order_number,
            customer_email=customer_email,
            customer_phone=customer_phone,
            request_type=request_type,
        )
        if not result.get("success"):
            return self._sanitize_adapter_failure(
                order_name=result.get("order_name"),
                status_code=result.get("status_code"),
                message="Unable to fetch Return Prime status right now. Please try again later.",
            )

        requests = result["requests"]
        if not requests:
            return {
                "success": True,
                "status_code": 200,
                "order_name": result["order_name"],
                "message": NO_REQUEST_MESSAGE,
                "source": result.get("source"),
                "requests": [],
            }

        if len(requests) > 1:
            return {
                "success": True,
                "status_code": 200,
                "order_name": result["order_name"],
                "message": "Multiple return/exchange requests found for this order.",
                "requests": [
                    {
                        "request_id": item.get("request_id"),
                        "request_number": item.get("request_number"),
                        "request_type": item.get("request_type"),
                        "status": item.get("status"),
                        "item_title": _first_non_empty(
                            *(line.get("original_product") for line in item.get("line_items", []))
                        ),
                        "item_variant": _first_non_empty(
                            *(line.get("original_variant") for line in item.get("line_items", []))
                        ),
                    }
                    for item in requests
                ],
                # Full normalized request objects (refund, line_items, shipping/AWB, etc.)
                # for callers that need to inspect a specific request's real status rather
                # than just the summary list above — e.g. refund/pickup status lookups.
                "requests_full": requests,
            }

        request = requests[0]
        source = result.get("source")
        message = (
            f"Return Prime request {request.get('request_number') or request.get('request_id')} "
            f"is {request.get('status')}."
        )
        if request.get("request_type") == "exchange":
            exchange_order = request.get("exchange_order") or {}
            if not exchange_order.get("name"):
                message += " Exchange order is not created yet."
        return {
            "success": True,
            "status_code": 200,
            "order_name": result["order_name"],
            "message": message,
            "source": source,
            "request": request,
        }

    def _cached_request_response(self, row: dict, identifier: str) -> dict:
        """Build a response from the last webhook state we hold for a request.

        `received_at` is when Return Prime last pushed us this request's state, so
        it is surfaced as `last_updated_at` together with `is_cached` to let the
        agent tell the customer how fresh the answer is.
        """
        last_updated = row.get("received_at") or row.get("created_at")
        age_hours = None
        if isinstance(last_updated, datetime):
            reference = last_updated
            if reference.tzinfo is None:
                reference = reference.replace(tzinfo=timezone.utc)
            age_hours = round(
                (datetime.now(timezone.utc) - reference).total_seconds() / 3600, 1
            )
            last_updated = reference.isoformat()
        return {
            "success": True,
            "status_code": 200,
            "request": self.normalize_request(self._request_from_webhook_row(row)),
            "request_id": identifier,
            "source": "return_prime_webhook_events",
            "is_cached": True,
            "last_updated_at": last_updated,
            "cached_age_hours": age_hours,
            "message": (
                "Live Return Prime lookup is unavailable, so this is the last state Return Prime "
                f"sent us (last updated at {last_updated}). Tell the customer this status is as of "
                "that time and may have changed since."
            ),
        }

    async def get_request_by_id(self, client_id: str, request_id: str) -> dict:
        identifier = str(request_id or "").strip().lstrip("#")
        if not identifier:
            # Not routed through _sanitize_adapter_failure: that clamps anything
            # below 500 up to 502, which would report a bad argument as an outage.
            return {
                "success": False,
                "status_code": 400,
                "request_id": request_id,
                "message": "A Return Prime request number or request id is required.",
            }

        if _is_object_id(identifier):
            adapter_result = await return_prime_service.get_request_by_id(client_id, identifier)
            raw_request = self._extract_request_object(adapter_result.get("data"))
        else:
            # Customers quote the request NUMBER (e.g. "RET777"), which the by-id
            # endpoint rejects with 412. Look those up by request_number instead.
            adapter_result = await return_prime_service.list_requests(
                client_id,
                request_number=identifier,
            )
            raw_request = _first_dict(self._extract_request_list(adapter_result.get("data")))

        if adapter_result.get("success") and raw_request:
            return {
                "success": True,
                "status_code": adapter_result.get("status_code", 200),
                "request": self.normalize_request(raw_request),
                "request_id": identifier,
                "source": "return_prime_api",
                "is_cached": False,
                "raw": adapter_result.get("raw"),
            }

        if not adapter_result.get("success"):
            logger.warning(
                "Return Prime get_request_by_id failed for client_id=%s request_id=%s: %s",
                client_id,
                identifier,
                adapter_result,
            )
        rows = await db.postgres.fetch_all(
            """
            SELECT *
            FROM return_prime_webhook_events
            WHERE client_id = %s
              AND (
                request_id = %s
                OR return_request_id = %s
                OR UPPER(COALESCE(request_number, '')) = UPPER(%s)
              )
            ORDER BY received_at DESC, created_at DESC, id DESC
            LIMIT 1
            """,
            (client_id, identifier, identifier, identifier),
        )
        if rows:
            return self._cached_request_response(rows[0], identifier)

        if adapter_result.get("success"):
            # Return Prime answered cleanly and holds no such request. This is a
            # definitive "not found", not an outage, so do not report it as one.
            return {
                "success": True,
                "status_code": 200,
                "request": None,
                "request_id": identifier,
                "source": "return_prime_api",
                "is_cached": False,
                "message": f"Return Prime has no return/exchange request matching {identifier}.",
            }

        return self._sanitize_adapter_failure(
            request_id=identifier,
            status_code=adapter_result.get("status_code"),
            message="Unable to fetch Return Prime request details right now. Please try again later.",
        )

    async def verify_webhook_request(self, client_id: str, raw_body: bytes, headers: dict) -> dict:
        return await return_prime_webhook_service.verify_webhook_request(client_id, raw_body, headers)

    async def process_webhook(self, client_id: str, payload: dict, headers: dict) -> dict:
        return await return_prime_webhook_service.process_webhook(client_id, payload, headers)

    async def receive_webhook(self, client_id: str, payload: dict, headers: dict) -> dict:
        return await return_prime_webhook_service.store_webhook_event(client_id, payload, headers)

    async def process_stored_webhook(
        self,
        client_id: str,
        event_id: int,
        payload: dict,
        extracted: dict,
    ) -> dict:
        return await return_prime_webhook_service.process_stored_event(
            client_id, event_id, payload, extracted
        )

    def enqueue_stored_webhook(
        self,
        client_id: str,
        event_id: int,
        payload: dict,
        extracted: dict,
    ) -> dict:
        """Push a stored webhook event onto the Dramatiq queue for worker processing."""
        from fashion_bot.return_prime.workers.tasks import enqueue_return_prime_webhook_event

        message_id = enqueue_return_prime_webhook_event(
            client_id=client_id,
            event_id=event_id,
            payload=payload,
            extracted=extracted,
        )
        return {"enqueued": True, "event_id": event_id, "message_id": message_id}

    async def recover_stale_webhooks(self, *, stale_after_minutes: int | None = None) -> dict:
        return await return_prime_webhook_service.requeue_stale_processing_events(
            stale_after_minutes=stale_after_minutes
        )


return_prime_workflow = ReturnPrimeWorkflowService()
