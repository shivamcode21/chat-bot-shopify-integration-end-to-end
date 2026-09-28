"""At-least-once idempotency guard (§10).

Dramatiq + Shopify both deliver at-least-once, so an actor may run for the same
webhook id more than once. We dedup on the source webhook/shipment id using a
``SET NX`` on the shared Upstash client. Fail-open: if Redis is unavailable we
return "not seen" so the job still runs (a rare duplicate is preferable to
dropping a real event).
"""
from __future__ import annotations

import logging

from fashion_bot.utils.redis_client import get_shared_async_redis_client
from fashion_bot.workers.config import WEBHOOK_DEDUP_TTL_SECONDS

logger = logging.getLogger(__name__)

# Sentinels that mean "no usable id" — never dedup on these.
_UNUSABLE = {None, "", "unknown", "none", "null"}


async def already_processed(
    dedup_key: str | None,
    *,
    namespace: str = "webhook",
    ttl_seconds: int | None = None,
) -> bool:
    """Return True if this id was already handled (and mark it otherwise).

    First caller for a given id gets ``False`` and the key is recorded; later
    callers within the TTL get ``True``. Unusable ids are never deduped.

    ``ttl_seconds`` overrides the webhook dedup window for callers that need a
    different one (e.g. the escalation follow-up alert cooldown). Omitted ⇒
    ``WEBHOOK_DEDUP_TTL_SECONDS``, unchanged for every existing caller.
    """
    if dedup_key is None or str(dedup_key).strip().lower() in _UNUSABLE:
        return False
    try:
        client = await get_shared_async_redis_client()
        if client is None:
            return False  # fail-open
        key = f"{namespace}:seen:{dedup_key}"
        ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else WEBHOOK_DEDUP_TTL_SECONDS
        # SET key 1 NX EX ttl → returns truthy only when it did NOT exist.
        created = await client.set(key, "1", nx=True, ex=ttl)
        return not created
    except Exception as ex:
        logger.debug("[IDEMPOTENCY] guard failed (fail-open): %r", ex)
        return False
