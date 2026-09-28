"""Shared async Redis client.

Single process-wide ``redis.asyncio`` client. Safe because the FastAPI
app and APScheduler crons share one event loop (``AsyncIOScheduler``
binds to the running loop), so the client's transports have a stable
owner.

Consolidates the previously duplicated ``_get_async_redis_client``
implementations that lived in ``config_manager.py``,
``gupshup_webhook.py`` and ``utils/utils.py`` — each of which
independently constructed an ``redis.asyncio.Redis`` client with the
same URL and (slightly drifted) SSL config.
"""
from typing import Any, Optional
import logging
import os

logger = logging.getLogger(__name__)

try:
    import redis.asyncio as _redis_async  # type: ignore
except ImportError:
    _redis_async = None

try:
    import redis as _redis_sync  # type: ignore
except ImportError:
    _redis_sync = None

try:
    import certifi
except ImportError:
    certifi = None

from fashion_bot.env_loader import get_bool, get_env

REDIS_URL = (
    get_env("REDIS_URL")
    or get_env("REDIS_CONNECTION_STRING")
    or "redis://localhost:6379/0"
)

_shared_async_redis_client: Any = None
_shared_sync_redis_client: Any = None


def _build_client_kwargs() -> dict:
    client_kwargs: dict = {"decode_responses": True}
    if str(REDIS_URL).lower().startswith("rediss://"):
        if certifi is not None:
            client_kwargs["ssl_ca_certs"] = certifi.where()
        if get_bool("REDIS_SSL_INSECURE", False):
            client_kwargs["ssl_cert_reqs"] = None
    return client_kwargs


def _sanitize_redis_url(url: str) -> str:
    if "://" in url:
        return f"{url.split('://', 1)[0]}://***"
    return url


async def get_shared_async_redis_client() -> Optional[Any]:
    """Return the shared async Redis client, or None if unavailable.

    None signals one of: redis package not installed, initial PING
    failed, or the URL is misconfigured. Callers must handle the None
    case gracefully (fail-open per AGENTS.md §3).
    """
    global _shared_async_redis_client
    if _shared_async_redis_client is not None:
        return _shared_async_redis_client
    if _redis_async is None:
        return None
    try:
        client = _redis_async.Redis.from_url(REDIS_URL, **_build_client_kwargs())
        await client.ping()
        _shared_async_redis_client = client
        logger.info(
            "Connected to shared async Redis at %s", _sanitize_redis_url(REDIS_URL)
        )
        return client
    except Exception as ex:
        logger.error(
            "Failed to connect to shared async Redis at %s: %s",
            _sanitize_redis_url(REDIS_URL),
            ex,
        )
        return None


def get_shared_sync_redis_client() -> Optional[Any]:
    """Return a process-wide *synchronous* Redis client, or None if unavailable.

    Mirror of :func:`get_shared_async_redis_client` for the few call sites that
    run outside the event loop and cannot ``await``:

    * OTel observable-gauge callbacks, invoked from the metric-reader thread.
    * Sync cron bodies executed in APScheduler's thread-pool executor.

    Uses the same URL and SSL config as the async client. Fail-open: returns
    None on any error so callers (telemetry only) never raise.
    """
    global _shared_sync_redis_client
    if _shared_sync_redis_client is not None:
        return _shared_sync_redis_client
    if _redis_sync is None:
        return None
    try:
        client = _redis_sync.Redis.from_url(REDIS_URL, **_build_client_kwargs())
        client.ping()
        _shared_sync_redis_client = client
        logger.info(
            "Connected to shared sync Redis at %s", _sanitize_redis_url(REDIS_URL)
        )
        return client
    except Exception as ex:
        logger.error(
            "Failed to connect to shared sync Redis at %s: %s",
            _sanitize_redis_url(REDIS_URL),
            ex,
        )
        return None


async def close_shared_async_redis_client() -> None:
    global _shared_async_redis_client
    if _shared_async_redis_client is None:
        return
    client = _shared_async_redis_client
    _shared_async_redis_client = None
    try:
        await client.aclose()
    except Exception:
        pass
