"""Dramatiq actors — thin wrappers over the EXISTING webhook handlers.

This module is the worker CLI target's actor source and is also imported by the
producer to obtain ``.send()`` handles. Heavy handler imports are done lazily
inside each actor body so importing this module stays cheap on the producer side
(it only needs the actor objects, not the handler's transitive deps).

Each actor:
  1. dedups on the source webhook id (at-least-once + Shopify retries, §10);
  2. sets the app trace_id for log correlation;
  3. delegates to the unchanged async handler;
  4. raises on a reported failure so Dramatiq's Retries middleware backs off and
     eventually dead-letters (§10).
"""
from __future__ import annotations

import logging
import re
import uuid

import dramatiq
from dramatiq import Retry as DramatiqRetry

# Importing the broker module configures the process-wide broker before the
# @dramatiq.actor decorators run.
from fashion_bot.workers import broker as _broker  # noqa: F401  (side-effect import)
from fashion_bot.workers import config
from fashion_bot.workers.idempotency import already_processed
from fashion_bot.trace_context import set_trace_id

logger = logging.getLogger(__name__)

_COMMON = dict(
    max_retries=config.WEBHOOK_JOB_MAX_RETRIES,
    min_backoff=config.WEBHOOK_JOB_MIN_BACKOFF_MS,
    max_backoff=config.WEBHOOK_JOB_MAX_BACKOFF_MS,
    time_limit=config.WEBHOOK_JOB_TIME_LIMIT_MS,
)

_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")
_RATE_LIMIT_RETRY_DELAY_MS = 30_000


def _raise_if_failed(result, *, job: str, ident: str) -> dict:
    if isinstance(result, dict) and result.get("success") is False:
        error_msg = result.get("error") or ""
        status_match = _HTTP_STATUS_RE.search(error_msg)
        if status_match and status_match.group(1) == "429":
            logger.warning(
                "[WORKER] %s (id=%s) hit Shopify 429 — scheduling retry in %dms",
                job, ident, _RATE_LIMIT_RETRY_DELAY_MS,
            )
            raise DramatiqRetry(delay=_RATE_LIMIT_RETRY_DELAY_MS)
        raise RuntimeError(
            f"{job} reported failure (id={ident}): {error_msg}"
        )
    return result if isinstance(result, dict) else {"success": True}


@dramatiq.actor(queue_name=config.QUEUE_SHOPIFY_INVENTORY, **_COMMON)
async def inventory_update(*, inventory_item_id, client_id, trace_id=None,
                           shopify_webhook_id=None, shopify_shop_domain=None,
                           webhook_available=None):
    trace_id = trace_id or uuid.uuid4().hex[:8]
    set_trace_id(trace_id)
    if await already_processed(shopify_webhook_id):
        logger.info("[WORKER] inventory_update skipped (dup id=%s)", shopify_webhook_id)
        return
    from fashion_bot.shopify.webhook.product_webhook import _handle_inventory_level_update
    result = await _handle_inventory_level_update(
        inventory_item_id=inventory_item_id,
        client_id=client_id,
        trace_id=trace_id,
        shopify_webhook_id=shopify_webhook_id,
        shopify_shop_domain=shopify_shop_domain,
        webhook_available=webhook_available,
    )
    _raise_if_failed(result, job="inventory_update", ident=str(inventory_item_id))


@dramatiq.actor(queue_name=config.QUEUE_SHOPIFY_PRODUCT, **_COMMON)
async def product_upsert(*, webhook_data, client_id, trace_id=None, is_create=False,
                         shopify_webhook_id=None, shopify_shop_domain=None):
    trace_id = trace_id or uuid.uuid4().hex[:8]
    set_trace_id(trace_id)
    if await already_processed(shopify_webhook_id):
        logger.info("[WORKER] product_upsert skipped (dup id=%s)", shopify_webhook_id)
        return
    from fashion_bot.shopify.webhook.product_webhook import handle_product_upsert
    result = await handle_product_upsert(
        webhook_data,
        client_id,
        trace_id,
        is_create=is_create,
        shopify_webhook_id=shopify_webhook_id,
        shopify_shop_domain=shopify_shop_domain,
    )
    _raise_if_failed(result, job="product_upsert", ident=str(webhook_data.get("id")))


@dramatiq.actor(queue_name=config.QUEUE_SHOPIFY_PRODUCT, **_COMMON)
async def product_delete(*, product_id, client_id, trace_id=None,
                         shopify_webhook_id=None, shopify_shop_domain=None):
    trace_id = trace_id or uuid.uuid4().hex[:8]
    set_trace_id(trace_id)
    if await already_processed(shopify_webhook_id):
        logger.info("[WORKER] product_delete skipped (dup id=%s)", shopify_webhook_id)
        return
    from fashion_bot.shopify.webhook.product_webhook import handle_product_delete
    result = await handle_product_delete(
        product_id,
        client_id,
        trace_id,
        shopify_webhook_id=shopify_webhook_id,
        shopify_shop_domain=shopify_shop_domain,
    )
    _raise_if_failed(result, job="product_delete", ident=str(product_id))


# ── Order / cart / shipping lanes ─────────────────────────────────────────
# These call processors that already do their own dedup/idempotency, so we do
# NOT raise on a logical ``success: False`` (that would dead-letter benign
# "not handled" events). Real infra errors raise out of the processor and are
# retried by Dramatiq's Retries middleware as usual. Order/cart additionally
# dedup on the Shopify webhook id to short-circuit Shopify's at-least-once
# retries before any DB work.

@dramatiq.actor(queue_name=config.QUEUE_SHOPIFY_ORDER, **_COMMON)
async def order_event(*, webhook_data, client_id, trace_id=None,
                      shopify_topic=None, shopify_webhook_id=None):
    trace_id = trace_id or uuid.uuid4().hex[:8]
    set_trace_id(trace_id)
    if await already_processed(shopify_webhook_id, namespace="order"):
        logger.info("[WORKER] order_event skipped (dup id=%s)", shopify_webhook_id)
        return
    from fashion_bot.shopify.webhook.shopify_webhook import _process_order_event
    await _process_order_event(webhook_data, client_id, trace_id, shopify_topic)


@dramatiq.actor(queue_name=config.QUEUE_SHOPIFY_CART, **_COMMON)
async def cart_event(*, destination_phone, template_id, params, image_url,
                     client_id, event_key, template_name,
                     trace_id=None, shopify_webhook_id=None):
    """Send the abandoned-checkout template. The cheap phone/template extraction
    stays inline on the producer; only the external Gupshup send is offloaded."""
    trace_id = trace_id or uuid.uuid4().hex[:8]
    set_trace_id(trace_id)
    if await already_processed(shopify_webhook_id, namespace="cart"):
        logger.info("[WORKER] cart_event skipped (dup id=%s)", shopify_webhook_id)
        return
    from fashion_bot.shopify.webhook.gupshup_template_sender import (
        asend_shopify_gupshup_template_generic,
    )
    result = await asend_shopify_gupshup_template_generic(
        destination_phone=destination_phone,
        template_id=template_id,
        params=params,
        image_url=image_url,
        client_id=client_id,
        event_key=event_key,
        template_name=template_name,
    )
    if not result:
        raise RuntimeError(f"abandoned-checkout template send failed (phone={destination_phone})")


@dramatiq.actor(queue_name=config.QUEUE_SHIPROCKET, **_COMMON)
async def shiprocket_event(*, webhook_data, client_id, trace_id=None):
    trace_id = trace_id or uuid.uuid4().hex[:8]
    set_trace_id(trace_id)
    # The Shiprocket processor stores + dedups events itself, and the same order
    # legitimately emits many distinct status events — so no queue-level dedup.
    from fashion_bot.shiprocket.webhook.shiprocket_webhook import event_processor
    await event_processor.process_webhook_event(webhook_data, client_id=client_id)


@dramatiq.actor(queue_name=config.QUEUE_SHIPROCKET_CART, **_COMMON)
async def shiprocket_cart_event(*, destination_phone, template_id, params, image_url,
                                client_id, event_key, template_name, trace_id=None):
    """Send the Fastrr/Shiprocket abandon-cart template on its own lane."""
    trace_id = trace_id or uuid.uuid4().hex[:8]
    set_trace_id(trace_id)
    from fashion_bot.shopify.webhook.gupshup_template_sender import (
        asend_shopify_gupshup_template_generic,
    )

    result = await asend_shopify_gupshup_template_generic(
        destination_phone=destination_phone,
        template_id=template_id,
        params=params,
        image_url=image_url,
        client_id=client_id,
        event_key=event_key,
        template_name=template_name,
    )
    if not result:
        raise RuntimeError(
            f"shiprocket abandon-cart template send failed (phone={destination_phone})"
        )


@dramatiq.actor(queue_name=config.QUEUE_DELHIVERY, **_COMMON)
async def delhivery_event(*, normalized, client_id, trace_id=None):
    trace_id = trace_id or uuid.uuid4().hex[:8]
    set_trace_id(trace_id)
    from fashion_bot.delhivery.webhook.delhivery_webhook import _event_processor
    await _event_processor.process_webhook_event(normalized, client_id=client_id)


@dramatiq.actor(queue_name=config.QUEUE_GUPSHUP_EVENTS, **_COMMON)
async def gupshup_event(*, event_data, trace_id=None):
    trace_id = trace_id or uuid.uuid4().hex[:8]
    set_trace_id(trace_id)
    from fashion_bot.gupshup_events_webhook import process_gupshup_event_payload

    result = await process_gupshup_event_payload(event_data, trace_id=trace_id)
    _raise_if_failed(result, job="gupshup_event", ident=str(result.get("message_id")))


# conversation_scan_event / conversation_created_event disabled — inactivity only.
# @dramatiq.actor(queue_name=config.QUEUE_CONVERSATION_EVENTS, **_COMMON)
# async def conversation_scan_event(*, conversation_ids, interval_minutes,
#                                   conversation_count=0, emitted_at=None,
#                                   trace_id=None):
#     ...
#
# @dramatiq.actor(queue_name=config.QUEUE_CONVERSATION_EVENTS, **_COMMON)
# async def conversation_created_event(*, conversation_id, client_id, phone=None,
#                                      channel_type=None, first_message=None,
#                                      created_at=None, trace_id=None):
#     ...


@dramatiq.actor(queue_name=config.QUEUE_CONVERSATION_EVENTS, **_COMMON)
async def conversation_inactivity_event(*, conversation_id, client_id, phone=None,
                                        channel_type=None, from_cursor_at=None,
                                        from_cursor_message_id=None,
                                        to_inbound_message_at=None,
                                        to_inbound_message_id=None,
                                        threshold_minutes=10,
                                        open_window_minutes=None,
                                        candidate_scan_limit=None,
                                        emitted_at=None,
                                        trace_id=None):
    trace_id = trace_id or uuid.uuid4().hex[:8]
    set_trace_id(trace_id)
    logger.info(
        "[WORKER] conversation_inactivity_event consumed conversation_id=%s client_id=%s "
        "threshold_minutes=%s open_window_minutes=%s candidate_scan_limit=%s emitted_at=%s to_inbound=%s",
        conversation_id,
        client_id,
        threshold_minutes,
        open_window_minutes,
        candidate_scan_limit,
        emitted_at,
        to_inbound_message_at,
    )
    from fashion_bot.workers.conversation_inactivity_processor import (
        process_conversation_inactivity_event,
    )

    result = await process_conversation_inactivity_event(
        conversation_id=conversation_id,
        client_id=client_id,
        phone=phone,
        from_cursor_at=from_cursor_at,
        from_cursor_message_id=from_cursor_message_id,
        to_inbound_message_at=to_inbound_message_at,
        to_inbound_message_id=to_inbound_message_id,
        trace_id=trace_id,
    )
    _raise_if_failed(result, job="conversation_inactivity_event", ident=str(conversation_id))


# ── Heavy cron offload ────────────────────────────────────────────────────
# The weekly product delta sync used to run inline on the webhook tier and OOM'd
# a 512 MiB instance (7-Jun). It now fans out one message per client onto this
# lane, consumed by queue-background-worker. Its own time/retry limits (a sync
# can run for minutes; retrying a heavy sweep is wasteful) differ from the
# webhook _COMMON profile.
_PRODUCT_SYNC = dict(
    max_retries=config.PRODUCT_SYNC_JOB_MAX_RETRIES,
    min_backoff=config.WEBHOOK_JOB_MIN_BACKOFF_MS,
    max_backoff=config.WEBHOOK_JOB_MAX_BACKOFF_MS,
    time_limit=config.PRODUCT_SYNC_JOB_TIME_LIMIT_MS,
)


@dramatiq.actor(queue_name=config.QUEUE_PRODUCT_SYNC, **_PRODUCT_SYNC)
async def product_delta_sync(*, client_id, hours=168, trace_id=None):
    """Delta-sync one client's products to the Search/vector index.

    Idempotent by design (content-hash compare), so a redelivery is safe and we
    do not dedup on a webhook id. The per-client work (fetch → OCR → LLM →
    upsert) is itself memory-batched in the orchestrator.
    """
    trace_id = trace_id or uuid.uuid4().hex[:8]
    set_trace_id(trace_id)
    # Lazy imports keep this module cheap to import on the producer side.
    from fashion_bot.cron_jobs.product_vector_sync_job import delta_sync_single_client
    from fashion_bot.monitoring.otel_metrics import cron_items_processed, request_client_id

    with request_client_id(client_id):
        result = await delta_sync_single_client(client_id, hours=hours)

    # Keep the product_sync items metric flowing now that the fan-out producer
    # no longer counts per-client products (the worker does the work).
    if isinstance(result, dict):
        items = (
            result.get("products_added", 0)
            + result.get("products_updated", 0)
            + result.get("products_deleted", 0)
        )
        if items:
            cron_items_processed.add(
                items, {"job_name": "product_sync", "client_id": client_id}
            )
    _raise_if_failed(result, job="product_delta_sync", ident=str(client_id))


@dramatiq.actor(queue_name=config.QUEUE_ESCALATION_EVENTS, **_COMMON)
async def escalation_event(*, escalation_id, client_id, customer_phone=None,
                           conversation_id=None, category=None, reason=None,
                           action_required=None, metadata=None, trace_id=None):
    trace_id = trace_id or uuid.uuid4().hex[:8]
    set_trace_id(trace_id)
    logger.info(
        "[WORKER] escalation_event consumed escalation_id=%s client_id=%s conversation_id=%s category=%s phone=%s reason=%s action_required=%s metadata=%s",
        escalation_id,
        client_id,
        conversation_id,
        category,
        customer_phone,
        (reason or "")[:200],
        (action_required or "")[:200],
        metadata,
    )


@dramatiq.actor(queue_name=config.QUEUE_ESCALATION_WHATSAPP, **_COMMON)
async def escalation_whatsapp(*, notify_id=None, client_id=None, trace_id=None,
                              phones=None, notification=None, template_fields=None,
                              immediate_attention=False, route_template=None):
    trace_id = trace_id or uuid.uuid4().hex[:8]
    set_trace_id(trace_id)
    # Per-channel dedup on notify_id: a redelivery of THIS lane can't re-send, and
    # the shared notify_id can't collide with the email lane (different namespace).
    # Marking here also makes any unexpected re-run a safe no-op — the state-free
    # sender is not per-recipient idempotent, so we must never re-run a partial send.
    if await already_processed(notify_id, namespace="escalation_whatsapp"):
        logger.info("[WORKER] escalation_whatsapp skipped (dup id=%s)", notify_id)
        return
    from fashion_bot.utils.escalation_helper import asend_escalation_whatsapp
    result = await asend_escalation_whatsapp(
        phones=phones or [],
        notification=notification,
        client_id=client_id,
        template_fields=template_fields,
        immediate_attention=immediate_attention,
        trace_id=trace_id,
        notify_id=notify_id,
        route_template=route_template,
    )
    # The sender is fail-open with per-recipient isolation (it never raises on an
    # individual send failure), so we do NOT re-raise to force a whole-channel
    # Dramatiq retry — that would re-alert recipients that already succeeded.
    # Email remains the guaranteed channel for anything WhatsApp couldn't deliver.
    logger.info(
        "[WORKER] escalation_whatsapp id=%s sent=%d failed=%d degraded=%d",
        notify_id,
        len(result.get("sent") or []),
        len(result.get("failed") or []),
        len(result.get("degraded") or []),
    )


@dramatiq.actor(queue_name=config.QUEUE_ESCALATION_EMAIL, **_COMMON)
async def escalation_email(*, notify_id=None, client_id=None, trace_id=None,
                           email=None, notification=None, subject=None,
                           email_html=None):
    trace_id = trace_id or uuid.uuid4().hex[:8]
    set_trace_id(trace_id)
    if await already_processed(notify_id, namespace="escalation_email"):
        logger.info("[WORKER] escalation_email skipped (dup id=%s)", notify_id)
        return
    from fashion_bot.utils.escalation_helper import asend_escalation_email
    result = await asend_escalation_email(
        email=email or {"to": [], "cc": []},
        notification=notification,
        subject=subject or "Escalation",
        client_id=client_id,
        email_html=email_html,
        trace_id=trace_id,
        notify_id=notify_id,
    )
    logger.info(
        "[WORKER] escalation_email id=%s sent=%d failed=%d",
        notify_id,
        len(result.get("email_sent") or []),
        len(result.get("email_failed") or []),
    )


# Map job types → actor objects so the producer can dispatch generically.
ACTOR_BY_JOB = {
    config.JOB_INVENTORY_UPDATE: inventory_update,
    config.JOB_PRODUCT_UPSERT: product_upsert,
    config.JOB_PRODUCT_DELETE: product_delete,
    config.JOB_ORDER_EVENT: order_event,
    config.JOB_CART_EVENT: cart_event,
    config.JOB_SHIPROCKET_EVENT: shiprocket_event,
    config.JOB_SHIPROCKET_CART_EVENT: shiprocket_cart_event,
    config.JOB_DELHIVERY_EVENT: delhivery_event,
    config.JOB_GUPSHUP_EVENT: gupshup_event,
    config.JOB_CONVERSATION_INACTIVITY_EVENT: conversation_inactivity_event,
    config.JOB_ESCALATION_EVENT: escalation_event,
    config.JOB_ESCALATION_WHATSAPP: escalation_whatsapp,
    config.JOB_ESCALATION_EMAIL: escalation_email,
    config.JOB_PRODUCT_DELTA_SYNC: product_delta_sync,
}
