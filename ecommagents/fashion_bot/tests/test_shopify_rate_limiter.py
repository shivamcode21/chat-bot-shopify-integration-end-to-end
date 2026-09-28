"""Unit tests for the proactive Shopify rate limiter (utils/shopify_rate_limiter).

These exercise the process-local fallback path (Redis unavailable → per-process
token bucket), which is deterministic and needs no Redis, plus the Redis branch
of ``_reserve_wait`` via a fake client, and the fail-open behaviour when the
Redis ``eval`` errors.
"""

import asyncio
import time

import pytest

from fashion_bot.utils import shopify_rate_limiter as rl


def _limiter(**kw):
    kw.setdefault("rate_per_second", 5.0)
    kw.setdefault("burst", 2)
    kw.setdefault("max_wait_seconds", 30.0)
    return rl.ShopifyRateLimiter(**kw)


@pytest.mark.asyncio
async def test_burst_then_paced(monkeypatch):
    # Force the process-local bucket path (no Redis).
    monkeypatch.setattr(rl, "get_shared_async_redis_client", lambda: _async_none())
    lim = _limiter(rate_per_second=10.0, burst=3)

    t0 = time.monotonic()
    stamps = []
    for _ in range(6):
        await lim.acquire("shopA")
        stamps.append(time.monotonic() - t0)

    # First `burst` (3) go straight through; the rest are paced at ~1/rate = 0.1s.
    assert stamps[2] < 0.02, stamps
    # 6th call is 3 tokens past the burst → ~0.3s in.
    assert stamps[5] >= 0.25, stamps


@pytest.mark.asyncio
async def test_per_shop_buckets_are_independent(monkeypatch):
    monkeypatch.setattr(rl, "get_shared_async_redis_client", lambda: _async_none())
    lim = _limiter(rate_per_second=1.0, burst=1)

    await lim.acquire("shopA")  # drains shopA's single token
    # A different shop has its own full bucket → immediate.
    t = time.monotonic()
    await lim.acquire("shopB")
    assert time.monotonic() - t < 0.02


@pytest.mark.asyncio
async def test_disabled_never_paces(monkeypatch):
    monkeypatch.setattr(rl, "get_shared_async_redis_client", lambda: _async_none())
    lim = _limiter(rate_per_second=1.0, burst=1, enabled=False)
    t = time.monotonic()
    for _ in range(10):
        assert await lim.acquire("z") == 0.0
    assert time.monotonic() - t < 0.05


@pytest.mark.asyncio
async def test_max_wait_caps_sleep_and_no_over_depletion(monkeypatch):
    monkeypatch.setattr(rl, "get_shared_async_redis_client", lambda: _async_none())
    lim = _limiter(rate_per_second=1.0, burst=1, max_wait_seconds=0.4)

    await lim.acquire("B")  # consume the only token

    # Next call needs ~1s but is capped at max_wait (0.4s) and proceeds.
    t = time.monotonic()
    waited = await lim.acquire("B")
    dt = time.monotonic() - t
    assert abs(waited - 0.4) < 0.05, waited
    assert 0.35 <= dt <= 0.6, dt

    # The capped call reserved exactly one token (no phantom second reservation),
    # so the following acquire is again only ~max_wait, not pushed further out.
    t = time.monotonic()
    await lim.acquire("B")
    assert time.monotonic() - t <= 0.6


@pytest.mark.asyncio
async def test_run_and_decorator(monkeypatch):
    monkeypatch.setattr(rl, "get_shared_async_redis_client", lambda: _async_none())
    lim = _limiter()

    async def work(x):
        return x * 2

    assert await lim.run("shopA", work, 21) == 42

    # Module-level convenience wrapper uses the shared singleton.
    assert await rl.shopify_rate_limited_call("shopA", work, 5) == 10

    class Svc:
        shop_domain = "shopC"

        @rl.shopify_rate_limited(lambda self, *a, **k: self.shop_domain)
        async def fetch(self, v):
            return v + 1

    assert await Svc().fetch(9) == 10


@pytest.mark.asyncio
async def test_redis_branch_uses_returned_wait(monkeypatch):
    # Fake Redis whose eval returns wait in milliseconds; acquire should sleep it.
    class _FakeRedis:
        def __init__(self, wait_ms):
            self._wait_ms = wait_ms
            self.eval_calls = 0

        async def eval(self, *args):
            self.eval_calls += 1
            return self._wait_ms

    fake = _FakeRedis(200)
    monkeypatch.setattr(rl, "get_shared_async_redis_client", lambda: _async_value(fake))
    lim = _limiter(max_wait_seconds=30.0)

    t = time.monotonic()
    waited = await lim.acquire("shopA")
    dt = time.monotonic() - t
    assert fake.eval_calls == 1
    assert abs(waited - 0.2) < 0.05, waited
    assert 0.18 <= dt <= 0.35, dt

    # eval returning 0 → no sleep.
    fake._wait_ms = 0
    assert await lim.acquire("shopA") == 0.0


@pytest.mark.asyncio
async def test_redis_eval_error_fails_open_to_local(monkeypatch):
    class _BrokenRedis:
        async def eval(self, *args):
            raise RuntimeError("redis blew up")

    monkeypatch.setattr(
        rl, "get_shared_async_redis_client", lambda: _async_value(_BrokenRedis())
    )
    lim = _limiter(rate_per_second=1.0, burst=1)

    # Should not raise; falls back to the per-process bucket. First call (full
    # local bucket) is immediate; the limiter degraded gracefully.
    t = time.monotonic()
    await lim.acquire("shopA")
    assert time.monotonic() - t < 0.05


# --- small async helpers (get_shared_async_redis_client is itself a coroutine) -
async def _async_none():
    return None


def _async_value(v):
    async def _coro():
        return v
    return _coro()
