"""Proactive, distributed rate limiter for outbound Shopify Admin API calls.

The :mod:`fashion_bot.utils.shopify_throttle` helpers are **reactive**: they
back off *after* Shopify answers with HTTP 429 / GraphQL ``THROTTLED``. That
protects a single call once it has already tripped the limit, but it does
nothing to stop many callers (the product full-sync, the delta-sync webhook,
the monthly bestseller refresh, ad-hoc tools) — possibly spread across multiple
Render pods/services — from *collectively* hammering Shopify in the first place.

This module is the **proactive** layer. It enforces a steady ceiling on how
fast we are *allowed* to call Shopify, paced *before* the request leaves the
process, so the reactive backoff is only ever a safety net rather than the
primary control. The ceiling is shared:

* **Per shop** — Shopify rate limits are per store, so the bucket key includes
  the shop domain; one busy store never starves another.
* **Across pods/services** — the bucket lives in Redis (the same shared client
  the cron locks use), so every replica of every service draws tokens from one
  cluster-wide allowance. In a multi-server deployment the *sum* of all callers
  stays under the ceiling, not each pod independently.

Algorithm: a **token bucket** maintained atomically in Redis via a Lua script
(refill on read, reserve-on-consume). Reserving (letting the token count go
negative) queues concurrent callers in an orderly line and computes how long
each must wait, so a burst of callers paces out instead of stampeding.

Fail-open (AGENTS.md §3): if Redis is unavailable we transparently fall back to
a **per-process** asyncio token bucket — a single pod still paces itself, the
reactive backoff still guards the rest, and no Shopify call is ever blocked
outright by limiter failure.

Two ways to use it, matching the two shapes callers want:

1. Pace a call you already make yourself — ``await limiter.acquire(shop)`` (or
   ``async with limiter.limit(shop):``) right before the request.
2. Hand the limiter a function to run *under* the limit —
   ``await limiter.run(shop, fn, *args, **kwargs)`` / the module-level
   :func:`shopify_rate_limited_call`, or the :func:`shopify_rate_limited`
   decorator.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

from fashion_bot.env_loader import get_bool, get_float, get_int
from fashion_bot.utils.redis_client import get_shared_async_redis_client

logger = logging.getLogger(__name__)

T = TypeVar("T")

# --- Tunables (env-overridable, read once at construction) -------------------
# Steady-state ceiling, in requests/second, that ALL Shopify callers for one
# shop may collectively sustain. Shopify's Admin GraphQL limit is cost-based
# (default 100 points/s restore, 1000 bucket); 2 req/s is a conservative
# request-rate proxy that leaves ample headroom for interactive tools while a
# bulk sweep runs. Tune per deployment via SHOPIFY_RATE_LIMIT_RPS.
_DEFAULT_RPS = 2.0
# Burst capacity (bucket size): how many requests may fire back-to-back after
# an idle period before pacing kicks in. Keeps short bursts snappy.
_DEFAULT_BURST = 4
# Hard cap on how long a single acquire() will wait before giving up and
# proceeding anyway (fail-open): the reactive backoff then handles any real
# throttle. Prevents a pathological backlog from stalling a request forever.
_DEFAULT_MAX_WAIT_SECONDS = 30.0

# Token-bucket Lua: atomically refill by elapsed time, reserve the requested
# tokens (allowing a negative balance so concurrent callers queue), persist the
# new balance, and return how many MILLISECONDS the caller must wait (0 = go
# now). Reserve-on-consume — not allow/deny — so a burst paces out in order
# instead of every waiter being told the same wait and then stampeding.
#   KEYS[1]            bucket hash key
#   ARGV[1] rate       tokens per second
#   ARGV[2] burst      bucket capacity
#   ARGV[3] now_ms     caller clock (ms)
#   ARGV[4] requested  tokens for this call (normally 1)
_TOKEN_BUCKET_LUA = """
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local data = redis.call("HMGET", KEYS[1], "tokens", "ts")
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil or ts == nil then
  tokens = burst
  ts = now
end
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(burst, tokens + (elapsed / 1000.0) * rate)
tokens = tokens - requested
local wait_ms = 0
if tokens < 0 then
  wait_ms = math.ceil((-tokens / rate) * 1000.0)
end
redis.call("HSET", KEYS[1], "tokens", tokens, "ts", now)
-- Keep the key alive long enough to drain the worst-case reservation backlog,
-- then let it lapse so idle shops don't linger forever.
local ttl = math.ceil(burst / rate) + math.ceil(wait_ms / 1000.0) + 30
redis.call("EXPIRE", KEYS[1], ttl)
return wait_ms
"""


class _ProcessTokenBucket:
    """Per-process async token bucket used when Redis is unavailable.

    Mirrors the Lua reserve-on-consume math under an ``asyncio.Lock`` so a
    single pod still paces itself (and concurrent coroutines on that pod queue
    in order). No cross-pod coordination — that is what Redis provides — but it
    guarantees the limiter degrades to *something* useful rather than nothing.
    """

    def __init__(self, rate: float, burst: float):
        self._rate = rate
        self._burst = burst
        self._tokens = burst
        self._ts = time.monotonic()
        self._lock = asyncio.Lock()

    async def reserve(self, requested: float = 1.0) -> float:
        """Reserve ``requested`` tokens; return seconds the caller must wait."""
        async with self._lock:
            now = time.monotonic()
            elapsed = max(0.0, now - self._ts)
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._tokens -= requested
            self._ts = now
            if self._tokens < 0:
                return (-self._tokens) / self._rate
            return 0.0


class ShopifyRateLimiter:
    """Distributed (Redis-backed) token-bucket pacer for Shopify API calls.

    One instance is shared process-wide via :func:`get_shopify_rate_limiter`.
    All methods are keyed by ``shop`` (the shop domain) so each store gets its
    own independent allowance.
    """

    def __init__(
        self,
        *,
        rate_per_second: Optional[float] = None,
        burst: Optional[float] = None,
        max_wait_seconds: Optional[float] = None,
        enabled: Optional[bool] = None,
    ):
        self.rate = max(0.01, rate_per_second if rate_per_second is not None
                        else get_float("SHOPIFY_RATE_LIMIT_RPS", _DEFAULT_RPS))
        self.burst = max(1.0, float(burst if burst is not None
                         else get_int("SHOPIFY_RATE_LIMIT_BURST", _DEFAULT_BURST)))
        self.max_wait = max(0.0, max_wait_seconds if max_wait_seconds is not None
                            else get_float("SHOPIFY_RATE_LIMIT_MAX_WAIT", _DEFAULT_MAX_WAIT_SECONDS))
        self.enabled = (enabled if enabled is not None
                        else get_bool("SHOPIFY_RATE_LIMIT_ENABLED", True))
        # Per-shop process-local fallbacks, created lazily when Redis is down.
        self._local_buckets: Dict[str, _ProcessTokenBucket] = {}

    # -- internals ------------------------------------------------------------
    @staticmethod
    def _bucket_key(shop: str) -> str:
        return f"shopify_rl:{(shop or 'global').strip().lower()}"

    def _local_bucket(self, shop: str) -> _ProcessTokenBucket:
        bucket = self._local_buckets.get(shop)
        if bucket is None:
            bucket = _ProcessTokenBucket(self.rate, self.burst)
            self._local_buckets[shop] = bucket
        return bucket

    async def _reserve_wait(self, shop: str) -> float:
        """Reserve one token for ``shop``; return seconds to wait (Redis or local)."""
        redis_client = await get_shared_async_redis_client()
        if redis_client is not None:
            try:
                wait_ms = await redis_client.eval(
                    _TOKEN_BUCKET_LUA,
                    1,
                    self._bucket_key(shop),
                    self.rate,
                    self.burst,
                    int(time.time() * 1000),
                    1,
                )
                return max(0.0, float(wait_ms) / 1000.0)
            except Exception as e:
                # Fail-open: degrade to the per-process bucket for this call.
                logger.warning(
                    "[SHOPIFY_RL] Redis token-bucket eval failed for shop=%s (%s); "
                    "falling back to process-local pacing",
                    shop, e,
                )
        return await self._local_bucket(shop).reserve(1.0)

    # -- public API -----------------------------------------------------------
    async def acquire(self, shop: str) -> float:
        """Block until this caller is allowed to make one Shopify request.

        Reserves a token from the shared bucket and sleeps for the computed
        wait. The sleep is capped at ``max_wait`` (fail-open): on a very deep
        backlog the call proceeds anyway and the reactive backoff guards it.

        Returns the number of seconds spent waiting (0.0 if it went straight
        through) — useful for metrics/logging.
        """
        if not self.enabled:
            return 0.0

        # Reserve exactly ONE token, then sleep the wait it computed. We must not
        # re-reserve in a loop: each _reserve_wait call consumes another token, so
        # looping would over-deplete the shared bucket whenever a wait is clamped.
        wait_seconds = await self._reserve_wait(shop)
        if wait_seconds <= 0.0:
            return 0.0

        sleep_for = min(wait_seconds, self.max_wait)
        if sleep_for > 1.0:
            logger.debug("[SHOPIFY_RL] shop=%s pacing: sleeping %.2fs", shop, sleep_for)
        await asyncio.sleep(sleep_for)

        if wait_seconds > self.max_wait:
            logger.warning(
                "[SHOPIFY_RL] shop=%s computed wait %.1fs exceeded max_wait=%.1fs; "
                "proceeding (reactive backoff will guard). Consider raising "
                "SHOPIFY_RATE_LIMIT_RPS or lowering call volume.",
                shop, wait_seconds, self.max_wait,
            )
        return sleep_for

    def limit(self, shop: str) -> "_AcquireContext":
        """Async context manager form: ``async with limiter.limit(shop): ...``."""
        return _AcquireContext(self, shop)

    async def run(
        self,
        shop: str,
        fn: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Run an async ``fn`` under the rate limit for ``shop``.

        This is the "give the limiter a function to call" shape: it paces, then
        awaits ``fn(*args, **kwargs)`` and returns its result.
        """
        await self.acquire(shop)
        return await fn(*args, **kwargs)


class _AcquireContext:
    """Async context manager returned by :meth:`ShopifyRateLimiter.limit`."""

    def __init__(self, limiter: ShopifyRateLimiter, shop: str):
        self._limiter = limiter
        self._shop = shop

    async def __aenter__(self) -> float:
        return await self._limiter.acquire(self._shop)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


# --- Process-wide singleton + module-level conveniences ----------------------
_shared_limiter: Optional[ShopifyRateLimiter] = None


def get_shopify_rate_limiter() -> ShopifyRateLimiter:
    """Return the process-wide shared Shopify rate limiter."""
    global _shared_limiter
    if _shared_limiter is None:
        _shared_limiter = ShopifyRateLimiter()
    return _shared_limiter


async def shopify_rate_limited_call(
    shop: str,
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Pace ``fn`` through the shared limiter for ``shop``, then await it.

    Convenience wrapper around :meth:`ShopifyRateLimiter.run` for callers that
    just want "call this Shopify function, but throttled".
    """
    return await get_shopify_rate_limiter().run(shop, fn, *args, **kwargs)


def shopify_rate_limited(
    shop_getter: Callable[..., str],
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator: pace an async method/function through the shared limiter.

    ``shop_getter`` receives the wrapped call's ``*args, **kwargs`` and returns
    the shop key. For a bound method whose ``self`` carries ``shop_domain``::

        @shopify_rate_limited(lambda self, *a, **k: self.shop_domain)
        async def fetch_page(self, ...): ...
    """
    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            shop = shop_getter(*args, **kwargs)
            await get_shopify_rate_limiter().acquire(shop)
            return await fn(*args, **kwargs)
        wrapper.__name__ = getattr(fn, "__name__", "shopify_rate_limited")
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return decorator
