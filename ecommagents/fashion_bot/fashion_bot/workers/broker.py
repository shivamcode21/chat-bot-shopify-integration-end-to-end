"""Dramatiq broker configuration (shared by producer and worker).

Importing this module sets the process-wide Dramatiq broker:

* ``DRAMATIQ_BROKER_URL`` set → :class:`RedisBroker` against the dedicated Render
  Key Value instance (NOT the Upstash cache). Middleware: the broker defaults
  (Retries, TimeLimit, AgeLimit, ShutdownNotifications, Callbacks, Pipelines,
  CurrentMessage) **minus** the built-in Prometheus exposition (we use OTLP),
  **plus** ``AsyncIO`` (our handlers are ``async def``) and our
  ``OpenTelemetryMiddleware``. Queue depth is exported separately via an OTel
  observable gauge (``queue_depth.py``), not a middleware/thread.
* no URL → :class:`StubBroker`, so the module still imports in tests / local dev
  without a broker. ``.send()`` is never reached in that case because the
  producer only enqueues when ``WEBHOOK_QUEUE_ENABLED`` is set.

This module does NOT initialize OpenTelemetry providers — the web app already
does that at startup, and the worker entrypoint (``workers/run.py``) does it for
worker processes. Initializing here would double-configure providers in the web
process.
"""
from __future__ import annotations

import logging

import dramatiq
from dramatiq.middleware.asyncio import AsyncIO

from fashion_bot.env_loader import get_bool
from fashion_bot.workers import config
from fashion_bot.workers.otel_middleware import OpenTelemetryMiddleware

logger = logging.getLogger(__name__)

_broker = None
_broker_sync_client = None


def get_broker_sync_client():
    """Process-wide SYNCHRONOUS Redis client to the BROKER (one per resource).

    The broker is a distinct Redis resource from the Upstash cache (which has its
    own shared clients in ``utils/redis_client.py``), so it gets its own single
    client here rather than a per-call-site one. Used only from OTel
    observable-gauge callbacks (the queue-depth observer) and the ``dlq`` ops
    CLI — both run outside the event loop, mirroring the endorsed sync-client
    use in ``redis_client.get_shared_sync_redis_client``. Returns ``None`` if the
    broker URL is unset.
    """
    global _broker_sync_client
    if _broker_sync_client is not None:
        return _broker_sync_client
    if not config.DRAMATIQ_BROKER_URL:
        return None
    try:
        import redis as _redis
        kwargs: dict = {"decode_responses": True}
        if str(config.DRAMATIQ_BROKER_URL).lower().startswith("rediss://"):
            try:
                import certifi
                kwargs["ssl_ca_certs"] = certifi.where()
            except Exception:
                pass
            if get_bool("REDIS_SSL_INSECURE", False):
                kwargs["ssl_cert_reqs"] = None
        _broker_sync_client = _redis.Redis.from_url(config.DRAMATIQ_BROKER_URL, **kwargs)
        return _broker_sync_client
    except Exception as ex:
        logger.warning("[BROKER] sync client unavailable: %r", ex)
        return None


def _drop_prometheus(broker) -> None:
    """Remove Dramatiq's default Prometheus middleware — we export via OTLP."""
    try:
        broker.middleware = [
            m for m in broker.middleware
            if type(m).__name__ != "Prometheus"
        ]
    except Exception:
        pass


def _build_broker():
    if not config.DRAMATIQ_BROKER_URL:
        from dramatiq.brokers.stub import StubBroker
        logger.warning(
            "[BROKER] DRAMATIQ_BROKER_URL not set — using StubBroker. The queue "
            "is non-functional; producers will fall back to inline processing."
        )
        broker = StubBroker()
    else:
        from dramatiq.brokers.redis import RedisBroker
        broker = RedisBroker(url=config.DRAMATIQ_BROKER_URL)

    _drop_prometheus(broker)
    broker.add_middleware(AsyncIO())
    broker.add_middleware(OpenTelemetryMiddleware())
    return broker


def get_broker():
    """Lazily build, register, and return the process-wide broker."""
    global _broker
    if _broker is None:
        _broker = _build_broker()
        dramatiq.set_broker(_broker)
        logger.info("[BROKER] dramatiq broker configured (%s)", type(_broker).__name__)
    return _broker


# Configure on import so ``@dramatiq.actor`` in actors.py has a broker.
get_broker()
