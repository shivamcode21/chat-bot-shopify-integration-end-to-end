"""Shared Shopify Admin API throttle / backoff helpers.

Centralizes the rate-limit handling that every Shopify GraphQL call needs so
jobs don't each reimplement retry math (AGENTS.md: "Shared Utilities Over
Duplication"; ``tenacity`` is the project's standard retry library).

Three layers, all used across the product-ingestion + bestseller pipelines:

1. Pure helpers (:func:`is_throttled`, :func:`retry_after_seconds`,
   :func:`throttle_backoff_seconds`) — for callers that drive their own
   pagination loop and need fine-grained control over *why* they stopped
   (e.g. the orders sales-volume sweep, which reports partial-completeness).
2. :class:`ShopifyThrottle` + :func:`retry_shopify` — the Shopify analogue of
   the DB layer's ``@awith_retry`` decorator (``database_manager.py``). Decorate
   any async function that performs a Shopify Admin API call and you write only
   the "happy path"; the dedicated throttle class transparently retries the
   whole call on transient failures with capped exponential backoff (honouring
   Retry-After). Translate a raw response into the right error class with
   :func:`raise_for_shopify_status` / :func:`raise_for_shopify_errors`.
3. :func:`shopify_graphql_post` — a convenience single POST (driven by the same
   :class:`ShopifyThrottle`) that any "fetch one page / one object" Shopify
   GraphQL call can use to get bounded retry on HTTP 429 / 5xx / network errors
   / GraphQL ``THROTTLED`` for free, while non-retryable errors (auth/permission
   4xx, non-throttle GraphQL errors) propagate immediately.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Shared bounds for every Shopify retry: 6 retries (7 attempts) with backoff
# capped at 30s, matching the orders sweep's prior MAX_PAGE_RETRIES behaviour.
SHOPIFY_MAX_ATTEMPTS = 7
SHOPIFY_BACKOFF_CAP_SECONDS = 30.0


class ShopifyRetryableError(Exception):
    """Transient Shopify failure (429 / 5xx / THROTTLED) worth retrying."""

    def __init__(self, message: str, *, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class ShopifyGraphQLError(Exception):
    """Non-retryable GraphQL-level error returned inside a 200 response body."""


def is_throttled(errors: Any) -> bool:
    """True when a Shopify GraphQL ``errors`` array signals cost throttling."""
    try:
        for err in errors or []:
            code = ((err.get("extensions") or {}).get("code") or "").upper()
            msg = (err.get("message") or "").upper()
            if code == "THROTTLED" or "THROTTLED" in msg:
                return True
    except Exception:
        pass
    return False


def retry_after_seconds(resp: httpx.Response) -> Optional[float]:
    """Parse a Retry-After header (seconds), capped at the backoff cap."""
    try:
        ra = resp.headers.get("Retry-After")
        if ra:
            return min(float(ra), SHOPIFY_BACKOFF_CAP_SECONDS)
    except (TypeError, ValueError):
        pass
    return None


def throttle_backoff_seconds(data: Optional[Dict[str, Any]], attempt: int) -> float:
    """Backoff for a throttled page: honour Shopify's cost ``throttleStatus``
    when present (wait until enough cost points restore), else exponential."""
    try:
        if data:
            cost = (data.get("extensions") or {}).get("cost") or {}
            ts = cost.get("throttleStatus") or {}
            available = ts.get("currentlyAvailable")
            restore = ts.get("restoreRate")
            requested = cost.get("requestedQueryCost") or 0
            if restore and available is not None and requested and requested > available:
                return min((requested - available) / restore + 0.5, SHOPIFY_BACKOFF_CAP_SECONDS)
    except Exception:
        pass
    return min(2.0 ** attempt, SHOPIFY_BACKOFF_CAP_SECONDS)


def raise_for_shopify_status(resp: httpx.Response) -> None:
    """Translate an HTTP response into the right retry/terminal error.

    Call this from inside a :func:`retry_shopify`-decorated body (or any custom
    Shopify call) right after the POST so the throttle layer can react:

    * HTTP 429 → :class:`ShopifyRetryableError` (carries Retry-After) → retried.
    * HTTP 5xx → :class:`ShopifyRetryableError` → retried.
    * Other 4xx → ``httpx.HTTPStatusError`` via ``raise_for_status`` → propagates
      immediately (deterministic auth/permission/client error).
    * 2xx/3xx → returns, leaving the response for the caller to parse.
    """
    if resp.status_code == 429:
        raise ShopifyRetryableError(
            "Shopify HTTP 429 (rate limited)",
            retry_after=retry_after_seconds(resp),
        )
    if resp.status_code >= 500:
        raise ShopifyRetryableError(f"Shopify HTTP {resp.status_code}")
    # Non-429 4xx → deterministic client/auth error; surface immediately.
    resp.raise_for_status()


def raise_for_shopify_errors(data: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a GraphQL ``errors`` array (inside a 200 body) into errors.

    Throttle errors become a retryable :class:`ShopifyRetryableError`; any other
    GraphQL error becomes a terminal :class:`ShopifyGraphQLError`. Returns the
    body unchanged when there are no errors, so it composes inline::

        return raise_for_shopify_errors(resp.json())
    """
    errors = data.get("errors")
    if errors:
        if is_throttled(errors):
            raise ShopifyRetryableError(f"Shopify GraphQL THROTTLED: {errors}")
        raise ShopifyGraphQLError(f"Shopify GraphQL errors: {errors}")
    return data


class ShopifyThrottle:
    """Dedicated Shopify Admin API throttle/backoff policy.

    The single home for *when* a Shopify call should be retried and *how long*
    to wait between attempts. Both :func:`shopify_graphql_post` and the
    :func:`retry_shopify` decorator drive their retries through one instance so
    the backoff math lives in exactly one place (AGENTS.md: shared utilities;
    ``tenacity`` is the standard retry library).

    A transient failure is anything in :attr:`RETRYABLE_EXC` —
    :class:`ShopifyRetryableError` (429 / 5xx / GraphQL ``THROTTLED``) or an
    ``httpx.TransportError`` (network/timeout). Everything else propagates on
    the first occurrence.
    """

    #: Exception types that mark a transient failure worth retrying.
    RETRYABLE_EXC = (ShopifyRetryableError, httpx.TransportError)

    def __init__(
        self,
        *,
        max_attempts: int = SHOPIFY_MAX_ATTEMPTS,
        backoff_cap_seconds: float = SHOPIFY_BACKOFF_CAP_SECONDS,
    ):
        self.max_attempts = max_attempts
        self.backoff_cap_seconds = backoff_cap_seconds

    def _wait(self, retry_state) -> float:
        """tenacity wait fn: honour Retry-After on the error, else exp backoff."""
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, ShopifyRetryableError) and exc.retry_after:
            return min(exc.retry_after, self.backoff_cap_seconds)
        return min(2.0 ** retry_state.attempt_number, self.backoff_cap_seconds)

    def attempts(self) -> AsyncRetrying:
        """An ``AsyncRetrying`` configured with this throttle's policy.

        For callers that want the explicit ``async for attempt in ...:`` loop
        (e.g. to pace *inside* each attempt). Use :meth:`run` for the common
        "just retry this coroutine" case.
        """
        return AsyncRetrying(
            retry=retry_if_exception_type(self.RETRYABLE_EXC),
            stop=stop_after_attempt(self.max_attempts),
            wait=self._wait,
            reraise=True,
        )

    async def run(
        self,
        fn: Callable[..., Awaitable[T]],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Invoke ``fn(*args, **kwargs)``, retrying transient failures.

        Re-raises the last exception once attempts are exhausted (tenacity
        ``reraise=True``).
        """
        async for attempt in self.attempts():
            with attempt:
                return await fn(*args, **kwargs)
        # AsyncRetrying(reraise=True) re-raises on exhaustion; unreachable.
        raise ShopifyRetryableError("Shopify request failed after retries")


#: Process-wide default throttle, sharing the standard bounds.
_default_throttle = ShopifyThrottle()


def get_shopify_throttle() -> ShopifyThrottle:
    """Return the shared default :class:`ShopifyThrottle`."""
    return _default_throttle


def retry_shopify(
    fn: Optional[Callable[..., Awaitable[T]]] = None,
    *,
    throttle: Optional[ShopifyThrottle] = None,
) -> Callable[..., Awaitable[T]]:
    """Decorator: auto-retry a Shopify Admin API call on transient failures.

    The Shopify analogue of the DB layer's ``@awith_retry`` — you write the
    "happy path" of the call and get bounded exponential backoff (honouring
    Retry-After) on HTTP 429 / 5xx / network errors / GraphQL ``THROTTLED`` for
    free, while deterministic 4xx and non-throttle GraphQL errors propagate
    immediately. The retry policy is delegated to the shared
    :class:`ShopifyThrottle` (override per-call with ``throttle=``).

    The decorated body must *raise* on transient failures for a retry to fire:
    use :func:`raise_for_shopify_status` (HTTP) and :func:`raise_for_shopify_errors`
    (GraphQL body), or let an ``httpx.TransportError`` propagate. Anything that
    paces (e.g. the proactive per-shop limiter) should live *inside* the body so
    it re-runs on every attempt.

    Usage::

        @retry_shopify
        async def fetch_product_by_id(self, product_id):
            async with httpx.AsyncClient(timeout=30.0) as client:
                await get_shopify_rate_limiter().acquire(self._rate_limit_key)
                resp = await client.post(self.graphql_url, headers=h, json=body)
                raise_for_shopify_status(resp)
                return raise_for_shopify_errors(resp.json())
    """
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            policy = throttle or get_shopify_throttle()
            return await policy.run(func, *args, **kwargs)
        return wrapper

    # Support both bare ``@retry_shopify`` and ``@retry_shopify(throttle=...)``.
    return decorator(fn) if fn is not None else decorator


async def shopify_graphql_post(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    *,
    max_attempts: int = SHOPIFY_MAX_ATTEMPTS,
    rate_limit_key: Optional[str] = None,
) -> Dict[str, Any]:
    """POST a Shopify GraphQL request with proactive pacing + bounded retry.

    Two complementary rate-limit layers:

    * **Proactive** — when ``rate_limit_key`` (the shop domain) is given, each
      attempt first draws a token from the shared, cross-pod
      :class:`~fashion_bot.utils.shopify_rate_limiter.ShopifyRateLimiter`, so we
      pace *before* sending and rarely trip Shopify's limit at all.
    * **Reactive** — the shared :class:`ShopifyThrottle` retries with capped
      exponential backoff (honouring Retry-After) on HTTP 429, HTTP 5xx,
      network/timeout errors, and GraphQL ``THROTTLED`` as a safety net.

    Raises immediately for non-retryable failures: non-429 4xx via
    ``httpx.HTTPStatusError`` and non-throttle GraphQL errors via
    :class:`ShopifyGraphQLError`. After the final attempt the last
    :class:`ShopifyRetryableError` (or ``httpx.TransportError``) propagates.

    Returns the parsed JSON response body on success.
    """
    limiter = None
    if rate_limit_key:
        # Imported lazily to avoid a hard dependency / import cycle for callers
        # that don't pace (the limiter pulls in the shared Redis client).
        from fashion_bot.utils.shopify_rate_limiter import get_shopify_rate_limiter
        limiter = get_shopify_rate_limiter()

    throttle = (
        get_shopify_throttle()
        if max_attempts == SHOPIFY_MAX_ATTEMPTS
        else ShopifyThrottle(max_attempts=max_attempts)
    )

    async def _attempt() -> Dict[str, Any]:
        # Proactive pacing: wait for our turn in the shared per-shop budget
        # before the request leaves the process (covers retries too).
        if limiter is not None:
            await limiter.acquire(rate_limit_key)
        resp = await client.post(url, headers=headers, json=payload)
        raise_for_shopify_status(resp)
        return raise_for_shopify_errors(resp.json())

    return await throttle.run(_attempt)
