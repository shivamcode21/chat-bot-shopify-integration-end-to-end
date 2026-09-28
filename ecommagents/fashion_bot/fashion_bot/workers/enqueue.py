"""Producer-side enqueue helper (§7).

``submit_or_inline`` is the ONLY thing webhook routes import from the workers
package. It is intentionally ``dramatiq``-free at module load: the broker/actors
are imported lazily and only when the lane is enabled, so with
``WEBHOOK_QUEUE_ENABLED`` off (default) the request path is identical to the
legacy inline behaviour and ``dramatiq`` need not even be installed.

Behaviour when a lane is enabled:
  * enqueue ok            → return a fast "queued" result;
  * broker unreachable     → **bounded** inline fallback (semaphore-capped well
    under DB_POOL_MAX so a broker outage during a storm can't reproduce the
    original pool exhaustion). If the cap is saturated we defer and let the
    source (Shopify) retry rather than pile onto the DB pool.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict

from fashion_bot.workers import config
from fashion_bot.workers.metrics import record_producer_event

logger = logging.getLogger(__name__)

# Brief window to grab an inline slot before deferring to source retry.
_INLINE_ACQUIRE_TIMEOUT = 0.1

_inline_sem: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    global _inline_sem
    if _inline_sem is None:
        _inline_sem = asyncio.Semaphore(config.WEBHOOK_INLINE_MAX_CONCURRENCY)
    return _inline_sem


async def _bounded_inline(inline: Callable[[], Awaitable[Dict[str, Any]]],
                          job_type: str) -> Dict[str, Any]:
    sem = _semaphore()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=_INLINE_ACQUIRE_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(
            "[WEBHOOK_QUEUE] inline fallback saturated for %s — deferring to "
            "source retry", job_type,
        )
        record_producer_event("deferred", job_type)
        return {"success": False, "action": "deferred", "reason": "inline_saturated"}
    try:
        return await inline()
    finally:
        sem.release()


async def _enqueue(job_type: str, payload: Dict[str, Any]) -> bool:
    """Send the job to its actor, retrying briefly. Returns True on success."""
    from fashion_bot.workers.actors import ACTOR_BY_JOB  # lazy: imports dramatiq

    actor = ACTOR_BY_JOB.get(job_type)
    if actor is None:
        logger.error("[WEBHOOK_QUEUE] no actor for job_type=%s", job_type)
        return False

    loop = asyncio.get_event_loop()
    last_err: Any = None
    for attempt in range(1, config.WEBHOOK_ENQUEUE_MAX_ATTEMPTS + 1):
        try:
            # .send() is a synchronous Redis round-trip; run off the event loop.
            await loop.run_in_executor(None, lambda: actor.send(**payload))
            return True
        except Exception as ex:  # noqa: BLE001 — resilience is the point
            last_err = ex
            if attempt < config.WEBHOOK_ENQUEUE_MAX_ATTEMPTS:
                backoff = config.WEBHOOK_ENQUEUE_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "[WEBHOOK_QUEUE] enqueue attempt %d/%d failed for %s (%r) — "
                    "retrying in %.2fs",
                    attempt, config.WEBHOOK_ENQUEUE_MAX_ATTEMPTS, job_type, ex, backoff,
                )
                await asyncio.sleep(backoff)
    logger.error(
        "[WEBHOOK_QUEUE] enqueue failed after %d attempts for %s: %r",
        config.WEBHOOK_ENQUEUE_MAX_ATTEMPTS, job_type, last_err,
    )
    return False


async def submit_or_inline(
    job_type: str,
    payload: Dict[str, Any],
    inline: Callable[[], Awaitable[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Enqueue a job, or run ``inline()`` — see module docstring.

    ``inline`` is a zero-arg callable returning the handler coroutine; it is only
    invoked when inline processing is actually needed, so no coroutine is created
    (and left un-awaited) on the queued path.
    """
    if not config.lane_enabled(job_type):
        return await inline()

    try:
        if await _enqueue(job_type, payload):
            logger.info("[WEBHOOK_QUEUE] queued %s", job_type)
            record_producer_event("queued", job_type)
            return {"success": True, "action": "queued", "queued": True}
    except Exception as ex:  # never let the queue path break the request
        logger.error("[WEBHOOK_QUEUE] unexpected enqueue error for %s: %r", job_type, ex)

    record_producer_event("failed", job_type)
    record_producer_event("fallback", job_type)
    logger.warning("[WEBHOOK_QUEUE] enqueue unavailable for %s — inline fallback", job_type)
    return await _bounded_inline(inline, job_type)
