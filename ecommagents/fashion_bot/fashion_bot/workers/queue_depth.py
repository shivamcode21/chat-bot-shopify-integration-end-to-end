"""Per-lane queue-depth observer (§17.1.2).

Dramatiq's built-in metrics report in-progress / delayed-in-memory counts but
NOT pending backlog length — the single most important signal for autoscaling
and alerting. We expose it as an OpenTelemetry **observable gauge**
(``dramatiq.queue.depth``) whose callback ``LLEN``s the broker's per-queue
pending list.

Why an observable gauge (not a background thread): AGENTS.md §1 forbids
``threading.Thread`` for background work. OTel's metric reader invokes the
callback on its own reader thread — which the SDK owns, not us — and we use the
endorsed synchronous broker client there (the same pattern ``redis_client``
documents for observable-gauge callbacks). So there is no self-managed thread,
loop, or lock.

Register once in the worker process via :func:`register_queue_depth_observer`
(called from ``run.py``), so the web/producer process does not also emit it.

NOTE (§17.1.2): the pending-list key for Dramatiq's Redis broker is
``{namespace}:{queue_name}`` (namespace defaults to ``dramatiq``). Verify against
the pinned dramatiq version if it changes. We deliberately do NOT read the
``.DQ`` (delayed) / ``.acks`` keys.
"""
from __future__ import annotations

import logging

from fashion_bot.env_loader import get_env
from fashion_bot.workers import config
from fashion_bot.workers.broker import get_broker_sync_client

logger = logging.getLogger(__name__)

NAMESPACE = get_env("WEBHOOK_QUEUE_NAMESPACE", "dramatiq") or "dramatiq"

_registered = False


def queue_key(queue: str) -> str:
    return f"{NAMESPACE}:{queue}"


def _observe(options):  # OTel CallbackOptions
    """Yield one Observation per lane with its current pending depth."""
    from opentelemetry.metrics import Observation

    client = get_broker_sync_client()
    if client is None:
        return
    for q in config.ALL_QUEUES:
        try:
            depth = int(client.llen(queue_key(q)) or 0)
            yield Observation(depth, {"queue": q})
        except Exception as ex:  # best-effort; never break metric collection
            logger.debug("[QUEUE_DEPTH] llen %s failed: %r", q, ex)


def register_queue_depth_observer() -> None:
    """Register the ``dramatiq.queue.depth`` observable gauge (worker only)."""
    global _registered
    if _registered:
        return
    try:
        from opentelemetry import metrics

        meter = metrics.get_meter("fashion-bot-workers", "1.0.0")
        meter.create_observable_gauge(
            "dramatiq.queue.depth",
            callbacks=[_observe],
            description="Pending (not-yet-processed) messages per queue",
            unit="messages",
        )
        _registered = True
        logger.info(
            "[QUEUE_DEPTH] observable gauge registered (queues=%s namespace=%s)",
            config.ALL_QUEUES, NAMESPACE,
        )
    except Exception as ex:
        logger.warning("[QUEUE_DEPTH] could not register observer: %r", ex)
