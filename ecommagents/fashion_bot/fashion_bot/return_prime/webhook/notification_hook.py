"""Return Prime -> enterprise WhatsApp template + client-team email hook.

Runs after a Return Prime webhook event is persisted to
``return_prime_webhook_events`` (see ``return_prime_webhook`` in
``webhook/router.py``). Fully opt-in per client via the
``return_prime_notifications`` switch (default OFF, AGENTS.md-style
graceful degradation) and a no-op until an approved Meta template has a row
in ``gupshup_templates`` for ``channel='return_prime'`` + the resolved event
key — so this can ship now and simply start sending the moment a client's
template is created and registered, no code change required.

Reuses the existing shared building blocks rather than a new integration:
* ``gupshup_templates`` / ``aget_client_template`` for the template lookup
  (same table shiprocket/shopify/delhivery use).
* ``whatsapp_enterprise_client`` for the actual enterprise Gupshup send
  (this is an enterprise-only client per the current requirement). All
  Return Prime templates are media/image-header templates, so this always
  sends via the media path (``asend_enterprise_template``) — never the
  text-only HSM path. A ``gupshup_templates`` row missing ``image_url`` is
  treated as an incomplete config, not silently sent as text.
* ``template_delivery_logger`` for send analytics.
* ``utils.utils.asend_email`` for the client-team email.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, List, Optional

from fashion_bot.database_manager import get_async_postgres_connection
from fashion_bot.return_prime.webhook.event_extraction import (
    REFUND_INITIATED,
    ReturnPrimeEvent,
    ReturnPrimeLineItem,
    extract_return_prime_event,
)
from fashion_bot.return_prime.webhook.notification_config import (
    aget_return_exchange_client_emails,
    aget_return_prime_auto_process_days,
    aget_return_prime_merchant_dashboard_url,
    aget_return_prime_notifications_enabled,
)
from fashion_bot.shopify.webhook.templates_db import aget_client_template
from fashion_bot.utils.template_param_resolver import build_template_params_from_context

logger = logging.getLogger(__name__)

RETURN_PRIME_TEMPLATE_CHANNEL = "return_prime"

# --------------------------------------------------------------------------- #
# STAGING TEST WHITELIST — TEMPORARY, REMOVE BEFORE GOING LIVE ON REAL TRAFFIC
# --------------------------------------------------------------------------- #
# While templates are being verified, only this number receives the actual
# WhatsApp send; every other customer's send is skipped (logged, recorded as
# ``whatsapp_status = "skipped_whitelist"``) so staging never messages real
# shoppers. Email + everything else in the flow runs unaffected.
#
# THE HOOK TO OPEN THE GATE FOR EVERYONE: set the env var
# ``RETURN_PRIME_WHATSAPP_WHITELIST_ENABLED=false`` (staging config / .env).
# That one flag is the single kill switch — no code change needed. Once
# templates are verified end-to-end, delete this whole block (and the env
# var) instead of just flipping it off, so the guard doesn't linger.
_WHATSAPP_WHITELIST_ENABLED_ENV = "RETURN_PRIME_WHATSAPP_WHITELIST_ENABLED"
_WHATSAPP_WHITELIST_NUMBERS_ENV = "RETURN_PRIME_WHATSAPP_TEST_NUMBERS"
_DEFAULT_WHATSAPP_TEST_NUMBERS = ("919611703832",)


def _is_whatsapp_whitelist_enabled() -> bool:
    raw = os.environ.get(_WHATSAPP_WHITELIST_ENABLED_ENV, "true")
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "")


def _whatsapp_whitelisted_numbers() -> tuple[str, ...]:
    raw = os.environ.get(_WHATSAPP_WHITELIST_NUMBERS_ENV)
    if not raw:
        return _DEFAULT_WHATSAPP_TEST_NUMBERS
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _is_whatsapp_number_whitelisted(phone: str) -> bool:
    from fashion_bot.utils.whatsapp_enterprise_client import normalize_msisdn

    normalized = normalize_msisdn(phone)
    return normalized in {normalize_msisdn(n) for n in _whatsapp_whitelisted_numbers()}


async def _aclaim_notification_row(
    *,
    client_id: str,
    webhook_event_id: Optional[int],
    request_id: Optional[str],
    event_key: str,
    order_number: Optional[str],
    customer_phone: Optional[str],
) -> Optional[int]:
    """Atomically claim (client_id, request_id, event_key).

    A redelivered Return Prime webhook inserts a *new* row in
    ``return_prime_webhook_events`` each time (that table has no dedupe key),
    so the claim here — not the raw event row — is what prevents duplicate
    sends. Mirrors the shiprocket ``ON CONFLICT DO NOTHING RETURNING id``
    pattern.
    """
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO return_prime_event_notifications (
                    client_id, webhook_event_id, request_id, event_key,
                    order_number, customer_phone, whatsapp_status, email_status
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', 'pending')
                ON CONFLICT (client_id, request_id, event_key) DO NOTHING
                RETURNING id
                """,
                (
                    client_id,
                    webhook_event_id,
                    request_id,
                    event_key,
                    order_number,
                    customer_phone,
                ),
            )
            row = await cur.fetchone()
    return int(row["id"]) if row else None


async def _aupdate_notification_row(notification_id: Optional[int], **fields: Any) -> None:
    if not notification_id or not fields:
        return
    set_clause = ", ".join(f"{key} = %s" for key in fields)
    values = list(fields.values()) + [notification_id]
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE return_prime_event_notifications SET {set_clause}, updated_at = NOW() WHERE id = %s",
                values,
            )


async def _aresolve_template_image_url(
    *,
    client_id: str,
    product_id: Optional[Any],
    variant_id: Optional[Any],
    db_image_url: Optional[str],
    event_key: str,
) -> Optional[str]:
    """Dynamic Shopify product image first, DB ``image_url`` as fallback.

    Mirrors the shiprocket/shopify send path (``_fetch_shopify_image_url``) —
    product photos can change after the return was filed, so the current
    Shopify image is preferred over whatever was embedded in the webhook or
    configured in ``gupshup_templates`` at template-setup time.
    """
    dynamic_image_url = None
    if product_id or variant_id:
        try:
            from fashion_bot.shopify.webhook.event_processor import _fetch_shopify_image_url

            dynamic_image_url = await _fetch_shopify_image_url(product_id, variant_id, client_id)
            dynamic_image_url = (dynamic_image_url or "").strip() or None
        except Exception as exc:  # noqa: BLE001 - graceful degradation, DB image still available
            logger.warning(
                "[RETURN_PRIME_NOTIFY] dynamic Shopify image fetch failed client_id=%s "
                "event_key=%s product_id=%s variant_id=%s err=%s",
                client_id,
                event_key,
                product_id,
                variant_id,
                exc,
            )
            dynamic_image_url = None

    if dynamic_image_url:
        return dynamic_image_url
    return (db_image_url or "").strip() or None


async def _asend_whatsapp_template(
    *,
    client_id: str,
    phone: str,
    event_key: str,
    template_cfg: dict,
    context: dict,
    product_id: Optional[Any],
    variant_id: Optional[Any],
    trace_id: Optional[str],
) -> dict:
    logger.info(
        "[RETURN_PRIME_NOTIFY] _asend_whatsapp_template client_id=%s event_key=%s phone=%s",
        client_id, event_key, phone,
    )

    from fashion_bot.utils.whatsapp_api_version import (
        WHATSAPP_API_VERSION_ENTERPRISE,
        aget_whatsapp_api_version,
    )

    api_version = await aget_whatsapp_api_version(client_id, trace_id)
    logger.info(
        "[RETURN_PRIME_NOTIFY] whatsapp_api_version=%s (need=%s) client_id=%s",
        api_version, WHATSAPP_API_VERSION_ENTERPRISE, client_id,
    )
    if api_version != WHATSAPP_API_VERSION_ENTERPRISE:
        logger.warning(
            "[RETURN_PRIME_NOTIFY] client_id=%s not on enterprise WhatsApp transport; "
            "skipping template send for event_key=%s — set whatsapp_api_version=enterprise in client_configs",
            client_id, event_key,
        )
        return {"status": "skipped_not_enterprise"}

    template_id = template_cfg.get("template_id")
    param_order = template_cfg.get("param_order") or []
    params = build_template_params_from_context(param_order, context)

    # All Return Prime templates are media (image-header) templates on Meta —
    # always send via the media/template path, never the text-only HSM path.
    # Dynamic Shopify image first (current product photo), gupshup_templates
    # image_url as fallback. Neither present is a real config gap, not
    # something to silently degrade to a text send.
    image_url = await _aresolve_template_image_url(
        client_id=client_id,
        product_id=product_id,
        variant_id=variant_id,
        db_image_url=template_cfg.get("image_url"),
        event_key=event_key,
    )
    if not image_url:
        logger.error(
            "[RETURN_PRIME_NOTIFY] no image available (dynamic Shopify fetch empty and "
            "gupshup_templates.image_url unset) for client_id=%s event_key=%s "
            "template_id=%s; Return Prime templates are media templates — set "
            "image_url before this can send",
            client_id,
            event_key,
            template_id,
        )
        return {"status": "skipped_missing_image_url"}

    from fashion_bot.utils.whatsapp_enterprise_client import asend_enterprise_template

    response = await asend_enterprise_template(
        phone,
        template_id,
        params,
        image_url=image_url,
        client_id=client_id,
        trace_id=trace_id,
        log_tag="RETURN_PRIME",
    )

    success = response is not None
    message_id = None
    if isinstance(response, dict):
        message_id = (response.get("response") or {}).get("id")

    try:
        from fashion_bot.utils.template_delivery_logger import alog_template_delivery

        await alog_template_delivery(
            client_id=client_id,
            phone_number=phone,
            template_id=template_id,
            success=success,
            event_key=event_key,
            channel="whatsapp",
            response_data=response,
            error_message=None if success else "enterprise gateway send failed",
            template_name=template_cfg.get("template_name"),
            template_params={"params": params},
            message_id=message_id,
        )
    except Exception as log_error:  # noqa: BLE001 - analytics logging must not break the send
        logger.warning("[RETURN_PRIME_NOTIFY] failed to log template delivery: %s", log_error)

    if not success:
        return {"status": "failed", "error": "enterprise gateway send failed"}
    return {"status": "sent", "message_id": message_id}


def _format_datetime(value: Optional[str]) -> str:
    if not value:
        return "N/A"
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).strftime("%d %b %Y, %I:%M %p")
    except (TypeError, ValueError):
        return text


def _format_line_items(line_items: List[ReturnPrimeLineItem]) -> str:
    if not line_items:
        return "N/A"
    parts = []
    for item in line_items:
        title = item.title or "Item"
        sku = item.sku or "N/A"
        qty = item.quantity if item.quantity not in (None, "") else "N/A"
        parts.append(f"{title} (SKU: {sku}, Qty: {qty})")
    return "; ".join(parts)


def _format_reasons(line_items: List[ReturnPrimeLineItem]) -> str:
    reasons = [item.reason for item in line_items if item.reason]
    if not reasons:
        return "N/A"
    return "; ".join(dict.fromkeys(reasons))


def _format_comments(line_items: List[ReturnPrimeLineItem]) -> str:
    notes = [item.notes for item in line_items if item.notes]
    if not notes:
        return "None"
    return "; ".join(dict.fromkeys(notes))


async def _build_client_email(client_id: str, event: ReturnPrimeEvent) -> tuple[str, str]:
    """Build (subject, body) for the client-team notification email.

    Same Order Details / Request Details format for all three events —
    RETURN_INITIATED, EXCHANGE_INITIATED, and REFUND_INITIATED.
    """
    from fashion_bot.utils.client_identity_cache import aget_client_name_by_id

    order_number = event.order_name or "N/A"
    client_name = await aget_client_name_by_id(client_id) or "Team"
    request_type_word = event.context.get("request_type") or "Return"
    contact = " / ".join(part for part in (event.phone, event.email) if part) or "N/A"
    auto_process_days = await aget_return_prime_auto_process_days(client_id)
    dashboard_url = await aget_return_prime_merchant_dashboard_url(client_id)

    subject = f"[{request_type_word} Initiated] Order {order_number} - {event.request_number or 'N/A'}"
    action_line = (
        f"Action Required: Please review and approve or reject this request from your dashboard: {dashboard_url}"
        if dashboard_url
        else "Action Required: Please review and approve or reject this request from your Return Prime dashboard."
    )

    request_details = [
        "Request Details",
        "",
        f"Request Type: {request_type_word}",
        f"Item(s): {_format_line_items(event.line_items)}",
        f"Reason: {_format_reasons(event.line_items)}",
        f"Customer Comments: {_format_comments(event.line_items)}",
        f"Requested On: {_format_datetime(event.requested_on)}",
    ]
    if event.event_key == REFUND_INITIATED and event.context.get("refund_amount"):
        request_details.append(f"Refund Amount: {event.context['refund_amount']}")

    body = "\n".join(
        [
            f"Hi {client_name},",
            "",
            f"A customer has initiated a {request_type_word.lower()} request for the following order:",
            "",
            "Order Details",
            "",
            f"Order ID: {order_number}",
            f"Order Date: {_format_datetime(event.order_created_at)}",
            f"Customer Name: {event.customer_full_name or 'N/A'}",
            f"Customer Contact: {contact}",
            "",
            *request_details,
            "",
            action_line,
            "",
            f"If no action is taken within {auto_process_days} days, the request may be "
            "auto-processed as per your return policy settings.",
        ]
    )
    return subject, body


async def _asend_client_email(
    *,
    client_id: str,
    event: ReturnPrimeEvent,
) -> dict:
    to, cc = await aget_return_exchange_client_emails(client_id)
    if not to:
        return {"status": "skipped_no_recipient"}

    from fashion_bot.utils.utils import asend_email

    from_addr = os.environ.get("RETURN_PRIME_NOTIFICATION_FROM_EMAIL", "notifications@bloomerce.ai")
    subject, body = await _build_client_email(client_id, event)

    try:
        await asend_email(from_addr, to, cc, subject, body)
        return {"status": "sent"}
    except Exception as exc:  # noqa: BLE001 - graceful degradation (AGENTS.md)
        logger.error("[RETURN_PRIME_NOTIFY] email send failed client_id=%s err=%s", client_id, exc)
        return {"status": "failed", "error": str(exc)}


async def amaybe_notify_return_prime_event(
    *,
    client_id: Optional[str],
    webhook_event_id: Optional[int],
    payload: dict,
    trace_id: Optional[str] = None,
) -> dict:
    """Send the WhatsApp template + client-team email for a Return Prime event.

    No-op (and safe to call unconditionally from the webhook handler) unless
    the client has the ``return_prime_notifications`` switch on AND the
    payload maps to one of RETURN_INITIATED / EXCHANGE_INITIATED /
    REFUND_INITIATED.

    ``webhook_event_id`` may be ``None`` if the raw-event audit-log insert
    failed (e.g. a transient DB/SSL error) — this hook derives everything it
    needs from ``payload`` directly and dedupes on
    ``(client_id, request_id, event_key)``, not on the raw event row, so it
    must not be skipped just because persistence failed.
    """
    tag = f"[RETURN_PRIME_NOTIFY] trace={trace_id} event_row={webhook_event_id} client={client_id}"

    logger.info("%s ▶ hook entered", tag)

    if not client_id:
        logger.warning("%s ✗ no client_id — skipping", tag)
        return {"status": "no_client_id"}

    notifications_enabled = await aget_return_prime_notifications_enabled(client_id)
    logger.info("%s notifications_enabled=%s", tag, notifications_enabled)
    if not notifications_enabled:
        logger.info("%s ✗ notifications disabled for client — set return_prime_notifications.enabled=true to activate", tag)
        return {"status": "disabled"}

    event = extract_return_prime_event(payload)
    if not event:
        request = payload.get("request") or payload
        raw_status = (request.get("status") or "") if isinstance(request, dict) else ""
        raw_type = (request.get("request_type") or "") if isinstance(request, dict) else ""
        raw_refund = ""
        if isinstance(request, dict):
            items = request.get("line_items") or []
            if items and isinstance(items[0], dict):
                raw_refund = (items[0].get("refund") or {}).get("status") or ""
        logger.info(
            "%s ✗ no actionable event — payload status=%r request_type=%r refund_status=%r "
            "(needs status=requested for RETURN/EXCHANGE, or non-pending refund_status for REFUND)",
            tag, raw_status, raw_type, raw_refund,
        )
        return {"status": "not_applicable"}

    logger.info(
        "%s ✓ event extracted: event_key=%s order=%s request_id=%s request_number=%s phone=%s email=%s",
        tag, event.event_key, event.order_name, event.request_id,
        event.request_number, event.phone, event.email,
    )

    notification_id = await _aclaim_notification_row(
        client_id=client_id,
        webhook_event_id=webhook_event_id,
        request_id=event.request_id,
        event_key=event.event_key,
        order_number=event.order_name,
        customer_phone=event.phone,
    )
    if not notification_id:
        logger.info(
            "%s ✗ duplicate — notification already claimed for request_id=%s event_key=%s",
            tag, event.request_id, event.event_key,
        )
        return {"status": "duplicate", "event_key": event.event_key}

    logger.info("%s ✓ notification row claimed id=%s", tag, notification_id)

    template_cfg = await aget_client_template(client_id, RETURN_PRIME_TEMPLATE_CHANNEL, event.event_key)
    logger.info(
        "%s template lookup channel=%s event_key=%s → found=%s template_id=%s",
        tag, RETURN_PRIME_TEMPLATE_CHANNEL, event.event_key,
        bool(template_cfg and template_cfg.get("template_id")),
        (template_cfg or {}).get("template_id"),
    )

    if not template_cfg or not template_cfg.get("template_id"):
        logger.info(
            "%s ✗ no template row — insert into gupshup_templates with "
            "channel='return_prime' event_key='%s' to enable WhatsApp sends",
            tag, event.event_key,
        )
        await _aupdate_notification_row(notification_id, whatsapp_status="skipped_no_template")
        whatsapp_result: dict = {"status": "skipped_no_template"}
    elif not event.phone:
        logger.info("%s ✗ no customer phone in payload — skipping WhatsApp send", tag)
        await _aupdate_notification_row(notification_id, whatsapp_status="skipped_no_phone")
        whatsapp_result = {"status": "skipped_no_phone"}
    else:
        logger.info(
            "%s 📲 sending WhatsApp template_id=%s to customer_phone=%s",
            tag, template_cfg.get("template_id"), event.phone,
        )
        whatsapp_result = await _asend_whatsapp_template(
            client_id=client_id,
            phone=event.phone,
            event_key=event.event_key,
            template_cfg=template_cfg,
            context=event.context,
            product_id=event.product_id,
            variant_id=event.variant_id,
            trace_id=trace_id,
        )
        logger.info("%s WhatsApp result=%s", tag, whatsapp_result)
        await _aupdate_notification_row(
            notification_id,
            whatsapp_status=whatsapp_result.get("status"),
            whatsapp_error=whatsapp_result.get("error"),
            whatsapp_message_id=whatsapp_result.get("message_id"),
        )

    to_emails, cc_emails = await aget_return_exchange_client_emails(client_id)
    logger.info("%s 📧 sending email to=%s cc=%s", tag, to_emails, cc_emails)
    email_result = await _asend_client_email(
        client_id=client_id,
        event=event,
    )
    logger.info("%s email result=%s", tag, email_result)
    await _aupdate_notification_row(
        notification_id,
        email_status=email_result.get("status"),
        email_error=email_result.get("error"),
    )

    logger.info(
        "%s ✅ done event_key=%s whatsapp=%s email=%s",
        tag, event.event_key, whatsapp_result.get("status"), email_result.get("status"),
    )
    return {
        "status": "processed",
        "event_key": event.event_key,
        "whatsapp": whatsapp_result,
        "email": email_result,
    }
