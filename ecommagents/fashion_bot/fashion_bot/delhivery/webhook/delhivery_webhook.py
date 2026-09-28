"""
FastAPI router for Delhivery Push API webhooks.

Mounted at ``/shipping/delhivery`` from `agent_controller.py`. The primary
endpoint embeds a base64-encoded `client_id` so we can resolve which tenant
the event belongs to without reading the body.

Endpoints:
  POST /shipping/delhivery/event/webhook/{encoded_client_id}
  POST /shipping/delhivery/event/webhook                         (legacy)
  POST /shipping/delhivery/event/webhook/bulk/{encoded_client_id}
  POST /shipping/delhivery/event/webhook/bulk
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict

from fastapi import APIRouter, Request

from fashion_bot.config_manager import aresolve_client_id
from fashion_bot.delhivery.webhook.event_processor import (
    DelhiveryEventProcessor,
    normalize_delhivery_webhook,
)
from fashion_bot.trace_context import generate_trace_id, set_trace_id
from fashion_bot.utils.utils import log_with_trace_id
# Queue producer — dramatiq-free import; offloads to a worker only when the
# 'delhivery' lane is enabled, else awaits inline (unchanged behaviour).
from fashion_bot.workers.enqueue import submit_or_inline
from fashion_bot.workers.config import JOB_DELHIVERY_EVENT

logger = logging.getLogger(__name__)
router = APIRouter()

_event_processor = DelhiveryEventProcessor()


def _decode_client_id(encoded_client_id: str) -> str:
    try:
        return base64.b64decode(encoded_client_id).decode("utf-8")
    except Exception as exc:
        logger.error(f"[DELHIVERY-WEBHOOK] decode error for {encoded_client_id!r}: {exc}")
        raise ValueError(f"Invalid base64 encoded client_id: {exc}")


@router.post("/event/webhook/{encoded_client_id}")
async def webhook(request: Request, encoded_client_id: str) -> Dict[str, Any]:
    """Multi-tenant Delhivery webhook (preferred)."""
    trace_id = generate_trace_id()
    set_trace_id(trace_id)

    try:
        client_id = _decode_client_id(encoded_client_id)
    except ValueError as exc:
        return {"status": "error", "error": str(exc), "trace_id": trace_id}

    from fashion_bot.utils.client_id_utils import is_client_blocklisted
    if is_client_blocklisted(client_id):
        logger.info(f"[DELHIVERY-WEBHOOK] 🚫 blocked client_id: {client_id}")
        return {"status": "blocked", "message": "Client is blocklisted", "trace_id": trace_id}

    try:
        raw_payload = await request.json()
    except Exception as exc:
        return {"status": "error", "error": f"Invalid JSON: {exc}", "client_id": client_id, "trace_id": trace_id}

    log_with_trace_id(
        trace_id,
        f"[CLIENT: {client_id}] Received Delhivery webhook keys={list((raw_payload or {}).keys())}",
        "info",
    )

    normalized = normalize_delhivery_webhook(raw_payload)
    if not normalized:
        log_with_trace_id(trace_id, f"[CLIENT: {client_id}] Invalid Delhivery webhook payload", "warning")
        return {
            "status": "error",
            "message": "Invalid webhook data: order_id, awb, shipment_status required",
            "client_id": client_id,
            "trace_id": trace_id,
        }

    try:
        # Offloaded to a worker when the 'delhivery' lane is enabled; inline otherwise.
        result = await submit_or_inline(
            JOB_DELHIVERY_EVENT,
            {"normalized": normalized, "client_id": client_id, "trace_id": trace_id},
            lambda: _event_processor.process_webhook_event(normalized, client_id=client_id),
        )
    except Exception as exc:
        log_with_trace_id(trace_id, f"[CLIENT: {client_id}] Delhivery processing error: {exc}", "error")
        return {"status": "error", "error": str(exc), "client_id": client_id, "trace_id": trace_id}

    return {
        "status": "ok" if result.get("success") else "error",
        "message": result.get("message", "Event processed"),
        "order_id": result.get("order_id"),
        "event_name": result.get("event_name"),
        "notification_sent": result.get("notification_sent", False),
        "client_id": client_id,
        "trace_id": trace_id,
    }


@router.post("/event/webhook")
async def webhook_legacy(request: Request) -> Dict[str, Any]:
    """Legacy URL (no client_id in path). Gated by env flag."""
    if os.getenv("DELHIVERY_ALLOW_LEGACY_WEBHOOK", "").lower() not in ("1", "true", "yes"):
        return {"status": "error", "error": "Legacy webhook URL disabled. Use /event/webhook/{base64_client_id}"}
    resolved = await aresolve_client_id()
    encoded = base64.b64encode(resolved.encode("utf-8")).decode("utf-8")
    return await webhook(request, encoded)


@router.post("/event/webhook/bulk/{encoded_client_id}")
async def bulk_webhook(request: Request, encoded_client_id: str) -> Dict[str, Any]:
    """Bulk endpoint. Body: {"events": [<delhivery_payload>, ...]}."""
    trace_id = generate_trace_id()
    set_trace_id(trace_id)

    try:
        client_id = _decode_client_id(encoded_client_id)
    except ValueError as exc:
        return {"status": "error", "error": str(exc), "trace_id": trace_id}

    try:
        body = await request.json()
    except Exception as exc:
        return {"status": "error", "error": f"Invalid JSON: {exc}", "client_id": client_id, "trace_id": trace_id}

    events = (body or {}).get("events") or []
    if not isinstance(events, list):
        return {"status": "error", "error": "events must be a list", "client_id": client_id, "trace_id": trace_id}

    results = []
    for entry in events:
        normalized = normalize_delhivery_webhook(entry)
        if not normalized:
            results.append({"success": False, "error": "invalid payload"})
            continue
        try:
            results.append(await _event_processor.process_webhook_event(normalized, client_id=client_id))
        except Exception as exc:
            results.append({"success": False, "error": str(exc)})

    success_count = sum(1 for r in results if r.get("success"))
    return {
        "status": "ok",
        "client_id": client_id,
        "trace_id": trace_id,
        "total": len(results),
        "success": success_count,
        "results": results,
    }


@router.post("/event/webhook/bulk")
async def bulk_webhook_legacy(request: Request) -> Dict[str, Any]:
    if os.getenv("DELHIVERY_ALLOW_LEGACY_WEBHOOK", "").lower() not in ("1", "true", "yes"):
        return {"status": "error", "error": "Legacy bulk webhook URL disabled."}
    resolved = await aresolve_client_id()
    encoded = base64.b64encode(resolved.encode("utf-8")).decode("utf-8")
    return await bulk_webhook(request, encoded)
