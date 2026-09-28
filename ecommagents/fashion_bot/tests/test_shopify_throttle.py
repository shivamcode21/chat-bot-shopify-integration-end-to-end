"""Unit tests for the reactive Shopify throttle (utils/shopify_throttle).

Covers the dedicated :class:`ShopifyThrottle` retry policy, the ``@retry_shopify``
decorator (the Shopify analogue of the DB layer's ``@awith_retry``), the
response-translation helpers, and the refactored :func:`shopify_graphql_post`.

``tenacity``/``httpx`` are core deps, so these run wherever the suite does; a
zero backoff cap keeps them fast (no real sleeps).
"""

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("tenacity")

from fashion_bot.utils import shopify_throttle as st


def _fast(**kw):
    kw.setdefault("max_attempts", 4)
    kw.setdefault("backoff_cap_seconds", 0)  # no real sleeping
    return st.ShopifyThrottle(**kw)


@pytest.mark.asyncio
async def test_retries_transient_then_succeeds():
    calls = {"n": 0}

    @st.retry_shopify(throttle=_fast())
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise st.ShopifyRetryableError("429", retry_after=0)
        return "ok"

    assert await flaky() == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_transport_error_is_retried():
    calls = {"n": 0}

    @st.retry_shopify(throttle=_fast())
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("network down")
        return "ok"

    assert await flaky() == "ok"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_terminal_error_not_retried():
    calls = {"n": 0}

    @st.retry_shopify(throttle=_fast())
    async def boom():
        calls["n"] += 1
        raise st.ShopifyGraphQLError("bad field")

    with pytest.raises(st.ShopifyGraphQLError):
        await boom()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_attempts_exhausted_reraises_last():
    calls = {"n": 0}

    @st.retry_shopify(throttle=_fast(max_attempts=3))
    async def always_429():
        calls["n"] += 1
        raise st.ShopifyRetryableError("429", retry_after=0)

    with pytest.raises(st.ShopifyRetryableError):
        await always_429()
    assert calls["n"] == 3  # exactly max_attempts, then reraise


@pytest.mark.asyncio
async def test_bare_decorator_uses_default_throttle():
    @st.retry_shopify
    async def ok():
        return 42

    assert await ok() == 42
    assert st.get_shopify_throttle().max_attempts == st.SHOPIFY_MAX_ATTEMPTS


def test_wait_honours_retry_after_capped():
    t = st.ShopifyThrottle(backoff_cap_seconds=5)

    class _State:
        class outcome:
            @staticmethod
            def exception():
                return st.ShopifyRetryableError("x", retry_after=100)
        attempt_number = 1

    # retry_after (100) clamped to the cap (5)
    assert t._wait(_State) == 5
    # without retry_after → exponential 2**attempt, also capped
    _State.outcome.exception = staticmethod(lambda: st.ShopifyRetryableError("x"))
    assert t._wait(_State) == 2


def test_raise_for_shopify_status():
    class Resp:
        def __init__(self, status, headers=None):
            self.status_code = status
            self.headers = headers or {}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("err", request=None, response=None)

    # 429 → retryable carrying Retry-After
    with pytest.raises(st.ShopifyRetryableError) as ei:
        st.raise_for_shopify_status(Resp(429, {"Retry-After": "3"}))
    assert ei.value.retry_after == 3.0

    # 5xx → retryable
    with pytest.raises(st.ShopifyRetryableError):
        st.raise_for_shopify_status(Resp(503))

    # other 4xx → terminal HTTPStatusError
    with pytest.raises(httpx.HTTPStatusError):
        st.raise_for_shopify_status(Resp(404))

    # 2xx → returns
    assert st.raise_for_shopify_status(Resp(200)) is None


def test_raise_for_shopify_errors():
    assert st.raise_for_shopify_errors({"data": 1}) == {"data": 1}
    with pytest.raises(st.ShopifyRetryableError):
        st.raise_for_shopify_errors({"errors": [{"extensions": {"code": "THROTTLED"}}]})
    with pytest.raises(st.ShopifyGraphQLError):
        st.raise_for_shopify_errors({"errors": [{"message": "bad"}]})


@pytest.mark.asyncio
async def test_shopify_graphql_post_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(st, "_default_throttle", _fast(max_attempts=5))

    class Resp:
        def __init__(self, status, body=None, headers=None):
            self.status_code = status
            self._body = body or {}
            self.headers = headers or {}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("err", request=None, response=None)
        def json(self):
            return self._body

    class FakeClient:
        def __init__(self, seq):
            self.seq = list(seq)
            self.i = 0
        async def post(self, *a, **k):
            r = self.seq[self.i]
            self.i += 1
            if isinstance(r, Exception):
                raise r
            return r

    client = FakeClient([
        Resp(429, headers={"Retry-After": "0"}),
        Resp(503),
        Resp(200, {"errors": [{"extensions": {"code": "THROTTLED"}}]}),
        httpx.ConnectError("flaky network"),
        Resp(200, {"data": {"ok": True}}),
    ])
    out = await st.shopify_graphql_post(client, "url", {}, {})
    assert out == {"data": {"ok": True}}
    assert client.i == 5

    # non-throttle GraphQL error is terminal (no retry)
    client2 = FakeClient([Resp(200, {"errors": [{"message": "bad"}]})])
    with pytest.raises(st.ShopifyGraphQLError):
        await st.shopify_graphql_post(client2, "url", {}, {})
    assert client2.i == 1
