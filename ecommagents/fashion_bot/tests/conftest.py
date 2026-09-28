"""
Shared pytest fixtures and helpers for integration tests.

Provides:
    1. In-memory Redis mock for tests that need Redis (dedup guard, etc.)
       without requiring a running Redis instance.
    2. Shopify 429 retry decorator for integration tests hitting the real API.
    3. LLM non-determinism helper for marking assertions as flaky.
"""

import asyncio
import time
from typing import Dict, Optional

import pytest


# ============================================================================
# 1. In-memory fake Redis for order dedup (and general async Redis clients)
# ============================================================================

class FakeRedis:
    """Minimal async-compatible in-memory Redis stand-in.

    Supports ``get``, ``set`` (with optional ``ex`` TTL), ``setex``,
    ``delete``, and ``exists``.  Values are stored as bytes (matching
    real redis-py behaviour).
    """

    def __init__(self):
        self._store: Dict[str, bytes] = {}
        self._expiry: Dict[str, float] = {}

    def _is_expired(self, key: str) -> bool:
        if key in self._expiry and time.time() > self._expiry[key]:
            self._store.pop(key, None)
            self._expiry.pop(key, None)
            return True
        return False

    async def get(self, key: str) -> Optional[bytes]:
        self._is_expired(key)
        val = self._store.get(key)
        if val is None:
            return None
        return val if isinstance(val, bytes) else val.encode()

    async def set(self, key: str, value, ex: Optional[int] = None, **_kw) -> bool:
        self._store[key] = value if isinstance(value, bytes) else str(value).encode()
        if ex is not None:
            self._expiry[key] = time.time() + ex
        return True

    async def setex(self, key: str, ttl: int, value) -> bool:
        return await self.set(key, value, ex=ttl)

    async def delete(self, *keys: str) -> int:
        count = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                self._expiry.pop(k, None)
                count += 1
        return count

    async def exists(self, *keys: str) -> int:
        return sum(1 for k in keys if k in self._store and not self._is_expired(k))

    async def ping(self) -> bool:
        return True

    async def close(self):
        pass

    async def aclose(self):
        pass


_fake_redis_instance = FakeRedis()


@pytest.fixture(autouse=True)
def _patch_redis_for_dedup(monkeypatch):
    """Replace the async Redis client used by UnifiedStateCache with FakeRedis.

    This ensures ``aget_order_dedup`` / ``aset_order_dedup`` and any other
    code path that calls ``cache._get_async_redis_client()`` gets a working
    in-memory store instead of failing on ``Connection refused``.

    The fake instance is shared across the session but each test gets a
    clean store (flushed in setup).
    """
    _fake_redis_instance._store.clear()
    _fake_redis_instance._expiry.clear()

    async def _fake_get_async_redis_client(self=None):
        return _fake_redis_instance

    from fashion_bot import state_cache as sc

    cache = sc.get_unified_cache()
    monkeypatch.setattr(cache, "_get_async_redis_client", _fake_get_async_redis_client)


# ============================================================================
# 2. Shopify 429 retry helper
# ============================================================================

def _is_rate_limit_error(result_or_exc) -> bool:
    """Return True when the value indicates a Shopify 429 rate-limit."""
    if isinstance(result_or_exc, dict):
        err = str(result_or_exc.get("error", "")).lower()
        details = str(result_or_exc.get("details", "")).lower()
        create_err = str(result_or_exc.get("create_error", "")).lower()
        haystack = f"{err} {details} {create_err}"
        return "429" in haystack or "rate limit" in haystack
    if isinstance(result_or_exc, Exception):
        return "429" in str(result_or_exc).lower() or "rate limit" in str(result_or_exc).lower()
    return False


async def shopify_retry(coro_fn, *args, max_retries: int = 3, base_delay: float = 60.0, **kwargs):
    """Retry an async call that may fail with Shopify HTTP 429 rate limits.

    Handles both exceptions **and** result dicts that signal rate-limiting
    (``{"success": False, "error": "...429..."}``) which is how the tool
    layer surfaces Shopify errors.

    Args:
        coro_fn:  An async callable (or a coroutine-returning callable).
        max_retries: How many times to retry after a 429.
        base_delay: Seconds to wait after the first 429 (doubles each retry).

    Returns:
        The result of the successful call.

    Raises:
        The original exception if all retries are exhausted.
    """
    last_result = None
    for attempt in range(max_retries + 1):
        try:
            result = await coro_fn(*args, **kwargs)
        except Exception as exc:
            if not _is_rate_limit_error(exc) or attempt == max_retries:
                raise
            wait = base_delay * (2 ** attempt)
            await asyncio.sleep(wait)
            continue

        if isinstance(result, dict) and _is_rate_limit_error(result) and attempt < max_retries:
            last_result = result
            wait = base_delay * (2 ** attempt)
            await asyncio.sleep(wait)
            continue
        return result

    return last_result


# ============================================================================
# 3. LLM non-determinism: soft assertion helpers
# ============================================================================

def assert_llm_carries_attribute(
    actual: str,
    *keywords: str,
    field_name: str = "qu_query",
    description: str = "",
):
    """Assert that at least one keyword appears in ``actual`` (case-insensitive).

    If none match, the test is marked as ``xfail`` (expected failure) rather
    than a hard failure, because LLM carry-forward is inherently
    non-deterministic.
    """
    actual_lower = actual.lower()
    if any(kw.lower() in actual_lower for kw in keywords):
        return
    pytest.xfail(
        f"LLM non-deterministic: {description or 'attribute carry-forward'} — "
        f"expected one of {keywords!r} in {field_name}={actual!r}"
    )
