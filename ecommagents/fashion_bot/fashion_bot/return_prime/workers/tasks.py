"""Dramatiq workers for Return Prime webhook processing."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import dramatiq

from fashion_bot.return_prime.workers.broker import RETURN_PRIME_WEBHOOK_QUEUE, broker  # noqa: F401

logger = logging.getLogger(__name__)


async def _process_return_prime_webhook_event_async(
    *,
    client_id: str,
    event_id: int,
    payload: dict,
    extracted: dict,
) -> dict:
    from fashion_bot.return_prime.webhook.service import return_prime_webhook_service

    return await return_prime_webhook_service.process_stored_event(
        client_id,
        event_id,
        payload,
        extracted,
    )


@dramatiq.actor(
    broker=broker,
    queue_name=RETURN_PRIME_WEBHOOK_QUEUE,
    max_retries=3,
    min_backoff=1000,
    max_backoff=60000,
)
def process_return_prime_webhook_event(
    client_id: str,
    event_id: int,
    payload: dict,
    extracted: dict,
) -> None:
    """Worker entrypoint: send Gupshup templates for a stored webhook event."""
    logger.info(
        "Processing Return Prime webhook event client_id=%s event_id=%s",
        client_id,
        event_id,
    )
    asyncio.run(
        _process_return_prime_webhook_event_async(
            client_id=client_id,
            event_id=event_id,
            payload=payload,
            extracted=extracted,
        )
    )


def enqueue_return_prime_webhook_event(
    *,
    client_id: str,
    event_id: int,
    payload: dict,
    extracted: dict,
) -> str:
    """Enqueue a stored Return Prime webhook event for worker processing."""
    message = process_return_prime_webhook_event.send(
        client_id,
        event_id,
        payload,
        extracted,
    )
    message_id = getattr(message, "message_id", None) or str(message)
    logger.info(
        "Enqueued Return Prime webhook event client_id=%s event_id=%s message_id=%s",
        client_id,
        event_id,
        message_id,
    )
    return message_id
