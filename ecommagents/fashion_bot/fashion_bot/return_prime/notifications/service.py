"""Return Prime webhook notification orchestration."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fashion_bot.return_prime.db import db
from fashion_bot.return_prime.notifications import gupshup as gupshup_notifications


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_event_key(event_type: str | None) -> str:
    return (event_type or "").strip().lower().replace("/", "_").replace("-", "_")


def _template_key_for_event(event_type: str | None, payload: dict, extracted: dict) -> str | None:
    event_key = _normalize_event_key(event_type)
    request = payload.get("request") or payload.get("data") or payload
    request = request if isinstance(request, dict) else {}
    exchange = request.get("exchange") or {}
    exchange_order_name = (exchange.get("order") or {}).get("name") or exchange.get("order_name")

    mapping = {
        "request_created": "return_prime_request_created",
        "request_approved": "return_prime_request_approved",
        "request_cancelled": "return_prime_request_cancelled",
        "request_rejected": "return_prime_request_rejected",
        "request_received": "return_prime_request_received",
        "request_inspected": "return_prime_request_inspected",
        "request_refunded": "return_prime_refund_processed",
        "request_updated": "return_prime_request_updated",
        "request_archived": "return_prime_request_archived",
    }
    template_key = mapping.get(event_key)
    if event_key == "request_updated" and exchange_order_name:
        return "return_prime_exchange_created"
    if template_key:
        return template_key

    status_key = _normalize_event_key(extracted.get("request_status"))
    status_mapping = {
        "approved": "return_prime_request_approved",
        "cancelled": "return_prime_request_cancelled",
        "rejected": "return_prime_request_rejected",
        "received": "return_prime_request_received",
        "inspected": "return_prime_request_inspected",
        "refunded": "return_prime_refund_processed",
        "archived": "return_prime_request_archived",
    }
    if status_mapping.get(status_key):
        return status_mapping[status_key]
    if exchange_order_name:
        return "return_prime_exchange_created"
    return None


def _build_message_text(template_key: str, payload: dict, extracted: dict) -> str:
    request = payload.get("request") or payload.get("data") or payload
    request = request if isinstance(request, dict) else {}
    order_name = extracted.get("order_name") or ((request.get("order") or {}).get("name"))
    request_number = extracted.get("request_number") or request.get("request_number")
    request_type = extracted.get("request_type") or request.get("request_type")
    request_status = extracted.get("request_status") or request.get("status")
    exchange = request.get("exchange") or {}
    exchange_order = (exchange.get("order") or {}).get("name") or exchange.get("order_name")
    rejected = request.get("rejected") or {}
    rejected_comment = rejected.get("comment") or "Reason not provided"

    messages = {
        "return_prime_request_created": (
            f"Request {request_number} created for order {order_name}. "
            f"Type: {request_type}. Status: {request_status}."
        ),
        "return_prime_request_approved": (
            f"Request {request_number} for order {order_name} has been approved."
        ),
        "return_prime_request_cancelled": (
            f"Request {request_number} for order {order_name} was cancelled."
        ),
        "return_prime_request_rejected": (
            f"Request {request_number} for order {order_name} was rejected. {rejected_comment}"
        ),
        "return_prime_request_received": (
            f"Returned item for request {request_number} has been received."
        ),
        "return_prime_request_inspected": (
            f"Returned item for request {request_number} has been inspected."
        ),
        "return_prime_refund_processed": (
            f"Refund for request {request_number} on order {order_name} has been processed."
        ),
        "return_prime_exchange_created": (
            f"Exchange order {exchange_order or ''} for request {request_number} on order {order_name}."
        ),
        "return_prime_request_updated": (
            f"Request {request_number} for order {order_name} was updated."
        ),
        "return_prime_request_archived": (
            f"Request {request_number} for order {order_name} was archived."
        ),
    }
    return messages.get(template_key, f"Update for request {request_number} on order {order_name}.")


def _build_template_params(template_key: str, payload: dict, extracted: dict) -> list[str]:
    request = payload.get("request") or payload.get("data") or payload
    request = request if isinstance(request, dict) else {}
    exchange = request.get("exchange") or {}
    exchange_order = (exchange.get("order") or {}).get("name") or exchange.get("order_name")
    rejected_comment = ((request.get("rejected") or {}).get("comment")) or "Reason not provided"
    params = [
        extracted.get("request_number") or request.get("request_number") or "",
        extracted.get("order_name") or ((request.get("order") or {}).get("name")) or "",
    ]
    if template_key == "return_prime_request_created":
        params.extend(
            [
                extracted.get("request_type") or request.get("request_type") or "",
                extracted.get("request_status") or request.get("status") or "",
            ]
        )
    elif template_key == "return_prime_request_rejected":
        params.append(rejected_comment)
    elif template_key == "return_prime_exchange_created":
        params.append(exchange_order or "")
    return params


def _template_is_optional(template_key: str) -> bool:
    return template_key in {
        "return_prime_request_updated",
        "return_prime_request_archived",
        "return_prime_request_cancelled",
    }


async def _create_notification_row(
    *,
    client_id: str,
    webhook_event_id: int,
    extracted: dict,
    customer_phone: str | None,
    template_key: str | None,
    template_id: str | None,
    template_params: list[str],
    message_text: str | None,
    status: str = "pending",
) -> int | None:
    row = await db.postgres.fetch_one(
        """
        INSERT INTO return_prime_whatsapp_notifications (
            client_id, webhook_event_id, request_id, request_number, order_name,
            customer_phone, template_key, template_id, template_params_json,
            message_text, status, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            client_id,
            webhook_event_id,
            extracted.get("request_id"),
            extracted.get("request_number"),
            extracted.get("order_name"),
            customer_phone,
            template_key,
            template_id,
            json.dumps(template_params, default=str),
            message_text,
            status,
            _iso_now(),
            _iso_now(),
        ),
    )
    return row["id"] if row else None


async def _update_notification_row(
    notification_id: int | None,
    *,
    status: str,
    error_message: str | None = None,
    gupshup_message_id: str | None = None,
    gupshup_response_json: Any = None,
    sent: bool = False,
) -> None:
    if not notification_id:
        return
    await db.execute(
        """
        UPDATE return_prime_whatsapp_notifications
        SET status = %s,
            error_message = %s,
            gupshup_message_id = %s,
            gupshup_response_json = %s::jsonb,
            sent_at = %s,
            updated_at = %s
        WHERE id = %s
        """,
        (
            status,
            error_message,
            gupshup_message_id,
            json.dumps(gupshup_response_json, default=str) if gupshup_response_json is not None else None,
            _iso_now() if sent else None,
            _iso_now(),
            notification_id,
        ),
    )


async def process_return_prime_notification(
    *,
    client_id: str,
    webhook_event_id: int,
    payload: dict,
    extracted: dict,
) -> dict:
    template_key = _template_key_for_event(extracted.get("event_type"), payload, extracted)
    if not template_key:
        event_key = _normalize_event_key(extracted.get("event_type"))
        return {
            "success": False,
            "status": "ignored",
            "error": None,
            "status_reason": f"unmapped_event_type: {event_key or 'unknown'}",
        }

    existing = await db.postgres.fetch_one(
        """
        SELECT id, status
        FROM return_prime_whatsapp_notifications
        WHERE webhook_event_id = %s AND template_key = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (webhook_event_id, template_key),
    )
    if existing and existing.get("status") == "sent":
        return {
            "success": False,
            "status": "ignored",
            "error": None,
            "notification_id": existing.get("id"),
        }

    templates = await gupshup_notifications.load_template_config(client_id)
    template_cfg = templates.get(template_key) if isinstance(templates, dict) else None
    template_id = None
    if isinstance(template_cfg, dict):
        template_id = template_cfg.get("template_id") or template_cfg.get("id") or template_cfg.get("name")
    elif isinstance(template_cfg, str):
        template_id = template_cfg

    normalized_phone = await gupshup_notifications.normalize_phone_for_whatsapp(
        extracted.get("customer_phone"),
        client_id=client_id,
    )
    message_text = _build_message_text(template_key, payload, extracted)
    template_params = _build_template_params(template_key, payload, extracted)
    notification_id = await _create_notification_row(
        client_id=client_id,
        webhook_event_id=webhook_event_id,
        extracted=extracted,
        customer_phone=normalized_phone,
        template_key=template_key,
        template_id=template_id,
        template_params=template_params,
        message_text=message_text,
    )

    if not normalized_phone:
        await _update_notification_row(notification_id, status="failed", error_message="Missing customer phone.")
        return {"success": False, "status": "failed", "error": "Missing customer phone."}

    if not template_id and _template_is_optional(template_key):
        await _update_notification_row(notification_id, status="ignored")
        return {
            "success": False,
            "status": "ignored",
            "error": None,
            "status_reason": f"template_not_configured: {template_key}",
        }

    if not template_id:
        await _update_notification_row(notification_id, status="failed", error_message="Missing Gupshup template.")
        return {"success": False, "status": "failed", "error": "Missing Gupshup template."}

    send_result = await gupshup_notifications.send_template_message(
        client_id=client_id,
        destination=normalized_phone,
        template_id=template_id,
        template_params=template_params,
    )
    if send_result.get("success"):
        await _update_notification_row(
            notification_id,
            status="sent",
            gupshup_message_id=send_result.get("message_id"),
            gupshup_response_json=send_result.get("raw"),
            sent=True,
        )
        return {
            "success": True,
            "status": "sent",
            "notification_id": notification_id,
            "message_id": send_result.get("message_id"),
        }

    await _update_notification_row(
        notification_id,
        status="failed",
        error_message=send_result.get("error"),
        gupshup_response_json=send_result.get("raw"),
        gupshup_message_id=send_result.get("message_id"),
    )
    return {
        "success": False,
        "status": "failed",
        "error": send_result.get("error"),
        "notification_id": notification_id,
    }
