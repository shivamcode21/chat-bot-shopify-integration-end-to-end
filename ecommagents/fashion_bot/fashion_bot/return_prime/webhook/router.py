"""Return Prime webhook receiver.

Mounted from ``agent_controller.py`` at ``/return-prime``:

    POST /return-prime/webhook

The first version intentionally persists the raw event and returns a fast ack.
Business processing can be layered on top once the exact Return Prime event
shapes are confirmed in production.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from fastapi import APIRouter, Request
from psycopg.types.json import Jsonb

from fashion_bot.database_manager import get_async_postgres_connection
from fashion_bot.trace_context import generate_trace_id, set_trace_id
from fashion_bot.utils.utils import log_with_trace_id

logger = logging.getLogger(__name__)
router = APIRouter()

_SENSITIVE_HEADERS = {
    "authorization",
    "x-rp-token",
    "x-returnprime-token",
    "x-api-key",
    "cookie",
}

def _hash_secret(value: str | None) -> str | None:
    token = str(value or "").strip()
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        normalized = key.lower()
        redacted[key] = "[REDACTED]" if normalized in _SENSITIVE_HEADERS else value
    return redacted


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _to_text(value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    return str(value)


def _normalize_shop_domain(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    text = text.removeprefix("https://").removeprefix("http://")
    text = text.split("/", 1)[0].strip()
    return text or None


def _nested_get(data: Any, *path: str) -> Any:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _payload_value(payload: Any, key: str) -> Any:
    return payload.get(key) if isinstance(payload, dict) else None


def _request_object(payload: Any) -> Any:
    if isinstance(payload, dict) and isinstance(payload.get("request"), dict):
        return payload["request"]
    return payload


def _first_line_item(payload: Any) -> dict:
    request_data = _request_object(payload)
    line_items = request_data.get("line_items") if isinstance(request_data, dict) else None
    if isinstance(line_items, list) and line_items and isinstance(line_items[0], dict):
        return line_items[0]
    return {}


def _first_shipping(payload: Any) -> dict:
    line_item = _first_line_item(payload)
    shipping = line_item.get("shipping")
    if isinstance(shipping, list) and shipping and isinstance(shipping[0], dict):
        return shipping[0]
    return {}


def _event_topic(payload: Any, headers: dict[str, str]) -> str | None:
    return _first_non_empty(
        _payload_value(payload, "topic"),
        _payload_value(payload, "event"),
        _payload_value(payload, "event_name"),
        _payload_value(payload, "eventType"),
        _payload_value(payload, "type"),
        headers.get("x-rp-topic"),
        headers.get("x-returnprime-topic"),
    )


def _event_id(payload: Any, headers: dict[str, str]) -> str | None:
    request_data = _request_object(payload)
    return _first_non_empty(
        _payload_value(payload, "id"),
        _payload_value(payload, "event_id"),
        _payload_value(payload, "eventId"),
        _payload_value(payload, "webhook_id"),
        _payload_value(payload, "webhookId"),
        _payload_value(request_data, "event_id"),
        _payload_value(request_data, "request_number"),
        headers.get("x-rp-webhook-id"),
        headers.get("x-request-id"),
    )


def _request_id(payload: Any) -> str | None:
    request_data = _request_object(payload)
    return _first_non_empty(
        _nested_get(payload, "request", "id"),
        _payload_value(request_data, "id"),
        _payload_value(payload, "request_id"),
        _payload_value(payload, "requestId"),
        _payload_value(payload, "return_id"),
        _payload_value(payload, "returnId"),
        _payload_value(payload, "return_request_id"),
        _payload_value(payload, "returnRequestId"),
        _nested_get(payload, "return", "id"),
        _nested_get(payload, "data", "request_id"),
        _nested_get(payload, "data", "requestId"),
    )


def _order_number(payload: Any) -> str | None:
    request_data = _request_object(payload)
    return _first_non_empty(
        _payload_value(payload, "order_number"),
        _payload_value(payload, "orderNumber"),
        _payload_value(payload, "order_name"),
        _payload_value(payload, "orderName"),
        _nested_get(request_data, "order", "name"),
        _nested_get(request_data, "order", "number"),
        _nested_get(payload, "order", "name"),
        _nested_get(payload, "order", "number"),
        _nested_get(payload, "data", "order_number"),
        _nested_get(payload, "data", "orderNumber"),
        _payload_value(payload, "order_id"),
        _payload_value(payload, "orderId"),
        _nested_get(request_data, "order", "id"),
        _nested_get(payload, "order", "id"),
    )


def _webhook_metadata(payload: Any, headers: dict[str, str]) -> dict[str, Any]:
    request_data = _request_object(payload)
    line_item = _first_line_item(payload)
    shipping = _first_shipping(payload)
    refund = line_item.get("refund") if isinstance(line_item.get("refund"), dict) else {}
    exchange = line_item.get("exchange") if isinstance(line_item.get("exchange"), dict) else {}
    exchange_order = exchange.get("order") if isinstance(exchange.get("order"), dict) else {}
    original_product = (
        line_item.get("original_product")
        if isinstance(line_item.get("original_product"), dict)
        else {}
    )
    exchange_product = (
        line_item.get("exchange_product")
        if isinstance(line_item.get("exchange_product"), dict)
        else {}
    )
    return {
        "topic": _event_topic(payload, headers),
        "event_id": _event_id(payload, headers),
        "request_id": _request_id(payload),
        "request_number": _payload_value(request_data, "request_number"),
        "request_type": _payload_value(request_data, "request_type"),
        "request_status": _payload_value(request_data, "status"),
        "order_number": _order_number(payload),
        "shopify_order_id": _nested_get(request_data, "order", "id"),
        "customer_phone": _nested_get(request_data, "customer", "phone"),
        "customer_email": _nested_get(request_data, "customer", "email"),
        "refund_status": _payload_value(refund, "status"),
        "refund_mode": _first_non_empty(
            _payload_value(refund, "actual_mode"),
            _payload_value(refund, "requested_mode"),
        ),
        "awb": _payload_value(shipping, "awb"),
        "shipping_company": _payload_value(shipping, "shipping_company"),
        "shipment_status": _payload_value(shipping, "shipment_status"),
        "exchange_order_id": _payload_value(exchange_order, "id"),
        "exchange_order_name": _payload_value(exchange_order, "name"),
        "original_variant_id": _payload_value(original_product, "variant_id"),
        "exchange_variant_id": _payload_value(exchange_product, "variant_id"),
    }


async def _aresolve_client_id_by_token(token: str | None) -> str | None:
    token_value = str(token or "").strip()
    if not token_value:
        return None
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT client_id
                FROM client_configs
                WHERE config_key = 'return_prime_details'
                  AND (
                    config_value ->> 'x_rp_token' = %s
                    OR config_value ->> 'api_token' = %s
                  )
                LIMIT 1
                """,
                (token_value, token_value),
            )
            row = await cur.fetchone()
    return str(row["client_id"]) if row and row.get("client_id") else None


async def _aresolve_client_id_by_shopify_store(store: str | None) -> str | None:
    shop_domain = _normalize_shop_domain(store)
    if not shop_domain:
        return None
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id
                FROM clients
                WHERE LOWER(COALESCE(shopify_domain_name, '')) = %s
                   OR LOWER(COALESCE(domain, '')) = %s
                LIMIT 1
                """,
                (shop_domain, shop_domain),
            )
            row = await cur.fetchone()
    return str(row["id"]) if row and row.get("id") else None


async def _aresolve_client_id(
    *,
    token: str | None,
    headers: dict[str, str],
    payload: Any,
) -> tuple[str | None, str]:
    token_client_id = await _aresolve_client_id_by_token(token)
    if token_client_id:
        return token_client_id, "return_prime_token"

    store = _first_non_empty(
        headers.get("x-rp-store"),
        headers.get("x-returnprime-store"),
        _nested_get(payload, "request", "return_location", "store"),
        _nested_get(payload, "request", "shopify_order_fulfillment_location", "store"),
        _nested_get(payload, "return_location", "store"),
    )
    store_client_id = await _aresolve_client_id_by_shopify_store(store)
    if store_client_id:
        return store_client_id, f"x-rp-store:{_normalize_shop_domain(store)}"

    return None, "unresolved"


async def _ainsert_webhook_event(
    *,
    client_id: str | None,
    payload: Any,
    headers: dict[str, str],
    token_hash: str | None,
    trace_id: str,
) -> int:
    try:
        metadata = _webhook_metadata(payload, headers)
    except Exception as exc:
        logger.warning(
            "Return Prime webhook metadata extraction failed; storing payload only",
            extra={"trace_id": trace_id, "error": str(exc)},
        )
        metadata = {}

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO return_prime_webhook_events (
                        client_id,
                        topic,
                        event_id,
                        request_id,
                        request_number,
                        request_type,
                        request_status,
                        order_number,
                        return_request_id,
                        shopify_order_id,
                        customer_phone,
                        customer_email,
                        refund_status,
                        refund_mode,
                        awb,
                        shipping_company,
                        shipment_status,
                        exchange_order_id,
                        exchange_order_name,
                        original_variant_id,
                        exchange_variant_id,
                        token_hash,
                        payload,
                        headers,
                        trace_id
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        client_id,
                        _to_text(metadata.get("topic")),
                        _to_text(metadata.get("event_id")),
                        _to_text(metadata.get("request_id")),
                        _to_text(metadata.get("request_number")),
                        _to_text(metadata.get("request_type")),
                        _to_text(metadata.get("request_status")),
                        _to_text(metadata.get("order_number")),
                        _to_text(metadata.get("request_id")),
                        _to_text(metadata.get("shopify_order_id")),
                        _to_text(metadata.get("customer_phone")),
                        _to_text(metadata.get("customer_email")),
                        _to_text(metadata.get("refund_status")),
                        _to_text(metadata.get("refund_mode")),
                        _to_text(metadata.get("awb")),
                        _to_text(metadata.get("shipping_company")),
                        _to_text(metadata.get("shipment_status")),
                        _to_text(metadata.get("exchange_order_id")),
                        _to_text(metadata.get("exchange_order_name")),
                        _to_text(metadata.get("original_variant_id")),
                        _to_text(metadata.get("exchange_variant_id")),
                        token_hash,
                        Jsonb(payload),
                        Jsonb(headers),
                        trace_id,
                    ),
                )
                row = await cur.fetchone()
        return int(row["id"])
    except Exception as exc:
        logger.warning(
            "Return Prime webhook full insert failed; retrying minimal payload insert",
            extra={"trace_id": trace_id, "error": str(exc)},
        )

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO return_prime_webhook_events (
                        client_id,
                        topic,
                        event_id,
                        request_id,
                        order_number,
                        return_request_id,
                        token_hash,
                        payload,
                        headers,
                        trace_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        client_id,
                        _to_text(metadata.get("topic")),
                        _to_text(metadata.get("event_id")),
                        _to_text(metadata.get("request_id")),
                        _to_text(metadata.get("order_number")),
                        _to_text(metadata.get("request_id")),
                        token_hash,
                        Jsonb(payload),
                        Jsonb(headers),
                        trace_id,
                    ),
                )
                row = await cur.fetchone()
        return int(row["id"])
    except Exception as exc:
        logger.warning(
            "Return Prime webhook minimal insert failed; retrying payload-only insert",
            extra={"trace_id": trace_id, "error": str(exc)},
        )

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO return_prime_webhook_events (
                        request_id,
                        return_request_id,
                        order_number,
                        payload
                    )
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        _to_text(metadata.get("request_id")),
                        _to_text(metadata.get("request_id")),
                        _to_text(metadata.get("order_number")),
                        Jsonb(payload),
                    ),
                )
                row = await cur.fetchone()
        return int(row["id"])
    except Exception as exc:
        logger.warning(
            "Return Prime webhook request-id insert failed; retrying payload-only insert",
            extra={"trace_id": trace_id, "error": str(exc)},
        )

    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO return_prime_webhook_events (payload)
                VALUES (%s)
                RETURNING id
                """,
                (Jsonb(payload),),
            )
            row = await cur.fetchone()
    return int(row["id"])


@router.post("/webhook")
async def return_prime_webhook(request: Request) -> dict[str, Any]:
    trace_id = generate_trace_id()
    set_trace_id(trace_id)

    raw_headers = dict(request.headers)
    redacted_headers = _redact_headers(raw_headers)
    token = raw_headers.get("x-rp-token") or raw_headers.get("x-returnprime-token")
    token_hash = _hash_secret(token)

    try:
        payload = await request.json()
    except Exception as exc:
        log_with_trace_id(trace_id, f"Return Prime webhook invalid JSON: {exc}", "warning")
        return {"status": "error", "message": f"Invalid JSON: {exc}", "trace_id": trace_id}

    try:
        client_id, client_resolution_source = await _aresolve_client_id(
            token=token,
            headers=raw_headers,
            payload=payload,
        )
    except Exception as exc:
        client_id = None
        client_resolution_source = "error"
        log_with_trace_id(
            trace_id,
            f"Return Prime webhook client resolution failed; storing without client_id: {exc}",
            "warning",
        )
    payload_keys = list(payload.keys()) if isinstance(payload, dict) else []
    extracted_request_id = _request_id(payload)
    extracted_order_number = _order_number(payload)
    log_with_trace_id(
        trace_id,
        (
            "Return Prime webhook received "
            f"client_id={client_id} topic={_event_topic(payload, raw_headers)} "
            f"client_resolution={client_resolution_source} "
            f"request_id={extracted_request_id} order_number={extracted_order_number} keys={payload_keys} "
            f"payload={payload} headers={redacted_headers}"
        ),
        "info",
        client_id=client_id,
    )

    # Raw-event persistence and the WhatsApp/email notification hook are
    # independent concerns: the notification hook derives everything it needs
    # from the in-memory `payload` (see `extract_return_prime_event`) and
    # `return_prime_event_notifications.webhook_event_id` is a nullable FK
    # (ON DELETE SET NULL). A transient DB/SSL failure on the audit-log insert
    # must not block the customer-facing send — that would violate this
    # repo's graceful-degradation rule (AGENTS.md). So persistence failure is
    # logged, not fatal, and the notification hook always runs.
    event_row_id: int | None = None
    persistence_error: str | None = None
    try:
        event_row_id = await _ainsert_webhook_event(
            client_id=client_id,
            payload=payload,
            headers=redacted_headers,
            token_hash=token_hash,
            trace_id=trace_id,
        )
    except Exception as exc:
        persistence_error = str(exc)
        logger.exception(
            "Return Prime webhook persistence failed; continuing to notification hook",
            extra={"trace_id": trace_id},
        )

    try:
        from fashion_bot.return_prime.webhook.notification_hook import (
            amaybe_notify_return_prime_event,
        )

        await amaybe_notify_return_prime_event(
            client_id=client_id,
            webhook_event_id=event_row_id,
            payload=payload,
            trace_id=trace_id,
        )
    except Exception as exc:
        logger.warning(
            "Return Prime notification hook failed",
            extra={"trace_id": trace_id, "error": str(exc), "client_id": client_id},
        )

    response: dict[str, Any] = {
        "status": "ok" if persistence_error is None else "degraded",
        "message": "Return Prime webhook received",
        "event_row_id": event_row_id,
        "client_id": client_id,
        "trace_id": trace_id,
    }
    if persistence_error:
        response["persistence_error"] = persistence_error
    return response
