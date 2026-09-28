"""Small producers for internal Dramatiq event streams.

These helpers keep request-path hooks lightweight. If the queue lane is enabled
the event is sent to Redis/Dramatiq; otherwise the inline fallback just logs the
payload so deployments stay backward compatible.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fashion_bot.workers import config
from fashion_bot.workers.enqueue import submit_or_inline

logger = logging.getLogger(__name__)


def fire_and_forget(coro, *, label: str) -> None:
    """Schedule a publisher without making the caller wait on queue I/O."""
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        logger.warning("[EVENT_QUEUE] no running loop for %s", label)
        return

    def _done(done_task: asyncio.Task) -> None:
        try:
            done_task.result()
        except Exception as exc:  # noqa: BLE001 - observability fallback
            logger.warning("[EVENT_QUEUE] %s publish failed: %r", label, exc)

    task.add_done_callback(_done)


async def _inline_log(event_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("[EVENT_QUEUE] inline %s payload=%s", event_name, payload)
    return {"success": True, "action": "inline", "event": event_name}


async def publish_conversation_created_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    # Disabled — only conversation_inactivity_event is enqueued for now.
    logger.debug("[EVENT_QUEUE] conversation_created_event disabled payload=%s", payload)
    return {"success": True, "action": "disabled", "event": "conversation_created_event"}


async def publish_escalation_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    return await submit_or_inline(
        config.JOB_ESCALATION_EVENT,
        payload,
        inline=lambda: _inline_log("escalation_event", payload),
    )


async def publish_escalation_notification(
    *,
    whatsapp_payload: Dict[str, Any],
    email_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Dispatch escalation WhatsApp + email, each on its own queue lane (§5.2a).

    Both channels fire concurrently. With a lane disabled (default) that channel
    is delivered **inline** — byte-for-byte the prior behaviour, and the caller
    gets back the full per-recipient result; with it enabled the job is enqueued
    and the actor delivers on the worker (the caller gets a fast queued marker).
    A failure in one channel never blocks the other. The two payloads share one
    ``notify_id`` but dedup in separate per-channel namespaces, so a redelivery of
    one lane can't re-send the other.
    """
    from fashion_bot.utils.escalation_helper import (
        asend_escalation_email,
        asend_escalation_whatsapp,
    )

    wa_result, email_result = await asyncio.gather(
        submit_or_inline(
            config.JOB_ESCALATION_WHATSAPP,
            whatsapp_payload,
            inline=lambda: asend_escalation_whatsapp(**whatsapp_payload),
        ),
        submit_or_inline(
            config.JOB_ESCALATION_EMAIL,
            email_payload,
            inline=lambda: asend_escalation_email(**email_payload),
        ),
    )

    merged: Dict[str, Any] = {}
    if isinstance(wa_result, dict):
        merged.update(wa_result)
    if isinstance(email_result, dict):
        merged.update(email_result)
    return merged


async def publish_conversation_scan_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    # Disabled — only conversation_inactivity_event is enqueued for now.
    logger.debug("[EVENT_QUEUE] conversation_scan_event disabled payload=%s", payload)
    return {"success": True, "action": "disabled", "event": "conversation_scan_event"}


async def publish_conversation_inactivity_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    return await submit_or_inline(
        config.JOB_CONVERSATION_INACTIVITY_EVENT,
        payload,
        inline=lambda: _inline_log("conversation_inactivity_event", payload),
    )
