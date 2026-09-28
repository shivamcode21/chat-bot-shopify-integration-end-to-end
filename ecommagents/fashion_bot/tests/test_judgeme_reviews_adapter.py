"""Offline tests for judgeme/tools/reviews_adapter.py -- the Judge.me HTTP
client itself (retry/backoff, status mapping, config validation, the capped
page loop). Complements test_judgeme_review_listing.py, which only exercises
this adapter through a mock -- these tests exercise the adapter's own logic
directly, with a mocked HTTP client.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from fashion_bot.judgeme.tools.reviews_adapter import JudgeMeReviewsAdapter, is_judgeme_configured

_MODULE = "fashion_bot.judgeme.tools.reviews_adapter"

_VALID_CONFIG = {
    "shop_domain": "example.myshopify.com",
    "api_token": "tok123",
    "public_token": "pub123",
    "api_base": "https://judge.me/api/v1",
    "webhook_secret": "whsec",
}


class _FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _adapter_with_config(config=None):
    adapter = JudgeMeReviewsAdapter(client_id="c1")
    adapter._config = config if config is not None else dict(_VALID_CONFIG)
    return adapter


# ── is_judgeme_configured: the SAME check _validate_config uses, exposed
# so a caller deciding whether to even register the tool doesn't need a
# separate, weaker truthiness check ────────────────────────────────────────

def test_is_judgeme_configured_true_for_real_credentials():
    assert is_judgeme_configured(dict(_VALID_CONFIG)) is True


def test_is_judgeme_configured_false_for_empty_config():
    assert is_judgeme_configured({}) is False
    assert is_judgeme_configured(None) is False


def test_is_judgeme_configured_false_for_placeholder_values():
    """Regression test: a plain truthiness check on shop_domain/api_token
    would wrongly say this IS configured -- both fields are non-empty
    strings. Only the placeholder-aware check catches it."""
    assert is_judgeme_configured({"shop_domain": "dummy", "api_token": "placeholder"}) is False
    assert is_judgeme_configured({"shop_domain": "your_shop_domain", "api_token": "your_api_token"}) is False


def test_is_judgeme_configured_false_when_only_one_field_present():
    assert is_judgeme_configured({"shop_domain": "example.myshopify.com"}) is False
    assert is_judgeme_configured({"api_token": "tok123"}) is False


# ── config validation short-circuits before any network call ──────────────

@pytest.mark.asyncio
async def test_configuration_missing_short_circuits_no_network_call():
    adapter = _adapter_with_config({})
    with patch(f"{_MODULE}.get_shared_async_http_client", new_callable=AsyncMock) as mock_get_client:
        result = await adapter.aresolve_product_id("123")

    assert result == {
        "success": False,
        "status": "configuration_missing",
        "message": "Judge.me configuration missing or placeholder",
    }
    mock_get_client.assert_not_called()


@pytest.mark.asyncio
async def test_dummy_placeholder_value_treated_as_missing():
    adapter = _adapter_with_config({"shop_domain": "dummy", "api_token": "placeholder"})
    with patch(f"{_MODULE}.get_shared_async_http_client", new_callable=AsyncMock) as mock_get_client:
        result = await adapter.aresolve_product_id("123")

    assert result["status"] == "configuration_missing"
    mock_get_client.assert_not_called()


# ── aresolve_product_id status mapping ─────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_product_id_success():
    adapter = _adapter_with_config()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_FakeResponse(200, {"product": {"id": 42}}))
    with patch(f"{_MODULE}.get_shared_async_http_client", new_callable=AsyncMock, return_value=mock_client):
        result = await adapter.aresolve_product_id("shopify-123")

    assert result == {"success": True, "id": 42, "product": {"id": 42}}


@pytest.mark.asyncio
async def test_resolve_product_id_404_maps_to_not_found():
    adapter = _adapter_with_config()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_FakeResponse(404, {"error": "not found"}))
    with patch(f"{_MODULE}.get_shared_async_http_client", new_callable=AsyncMock, return_value=mock_client):
        result = await adapter.aresolve_product_id("shopify-999")

    assert result["success"] is False
    assert result["status"] == "not_found"


@pytest.mark.asyncio
async def test_resolve_product_id_401_maps_to_generic_error_not_raw_body():
    """A non-404 4xx (auth/permission) must still carry a `status` key --
    the tool docstring promises failures always do, and only 404 is
    explicitly mapped. Regression test for the raw {status_code, body}
    leak a reviewer caught."""
    adapter = _adapter_with_config()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_FakeResponse(401, {"error": "unauthorized"}))
    with patch(f"{_MODULE}.get_shared_async_http_client", new_callable=AsyncMock, return_value=mock_client):
        result = await adapter.aresolve_product_id("shopify-123")

    assert result["success"] is False
    assert result["status"] == "error"


# ── retry/backoff: 429/5xx retried, other 4xx not ──────────────────────────

@pytest.mark.asyncio
async def test_429_is_retried_then_succeeds():
    adapter = _adapter_with_config()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(
        side_effect=[_FakeResponse(429, {}), _FakeResponse(200, {"product": {"id": 7}})]
    )
    with patch(f"{_MODULE}.get_shared_async_http_client", new_callable=AsyncMock, return_value=mock_client), \
         patch(f"{_MODULE}._BACKOFF_CAP_SECONDS", 0.01):
        result = await adapter.aresolve_product_id("shopify-1")

    assert result["success"] is True
    assert mock_client.get.await_count == 2


@pytest.mark.asyncio
async def test_404_is_not_retried():
    """404 is a deterministic 'no such product' -- retrying it would just
    waste the retry budget on every genuinely-missing product."""
    adapter = _adapter_with_config()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_FakeResponse(404, {}))
    with patch(f"{_MODULE}.get_shared_async_http_client", new_callable=AsyncMock, return_value=mock_client):
        await adapter.aresolve_product_id("shopify-1")

    assert mock_client.get.await_count == 1


# ── aget_all_published_reviews: capped page loop ───────────────────────────

@pytest.mark.asyncio
async def test_aget_all_published_reviews_stops_at_max_pages():
    """Regression test for the uncapped-loop risk a reviewer flagged --
    a product with more pages than max_pages must stop there, not loop
    forever, and must say so via more_pages_exist."""
    adapter = _adapter_with_config()

    async def _fake_aget_reviews(product_id, published_only, page, per_page):
        # Every page looks "full" (100 rows) and non-last, so an uncapped
        # loop would never stop on its own.
        return {
            "success": True,
            "reviews": [{"rating": 5, "published": True}] * 100,
            "is_last_page": False,
        }

    with patch.object(adapter, "aget_reviews", side_effect=_fake_aget_reviews) as mock_aget:
        result = await adapter.aget_all_published_reviews("jm-1", max_pages=3)

    assert result["success"] is True
    assert result["more_pages_exist"] is True
    assert result["count"] == 300
    assert mock_aget.await_count == 3


@pytest.mark.asyncio
async def test_aget_all_published_reviews_stops_at_real_last_page_under_cap():
    adapter = _adapter_with_config()

    async def _fake_aget_reviews(product_id, published_only, page, per_page):
        return {
            "success": True,
            "reviews": [{"rating": 5, "published": True}] * 10,
            "is_last_page": True,
        }

    with patch.object(adapter, "aget_reviews", side_effect=_fake_aget_reviews) as mock_aget:
        result = await adapter.aget_all_published_reviews("jm-1", max_pages=20)

    assert result["more_pages_exist"] is False
    assert result["count"] == 10
    assert mock_aget.await_count == 1


# ── deadline coherence: the adapter's own retry budget vs. the outer
# asyncio.wait_for in tool_factory.py's _create_product_reviews_tool.
# aget_curated_reviews makes up to 2 sequential adapter calls, so the outer
# deadline must exceed 2x the adapter's own worst case, or the retry budget
# never actually gets a chance to run before the outer wait_for wins the
# race on every attempt after the first (this was a real bug found on
# review: the two used to be numerically incoherent). ─────────────────────

def test_adapter_retry_budget_fits_under_outer_deadline_twice():
    import fashion_bot.judgeme.tools.reviews_adapter as adapter_module
    from fashion_bot.tool_factory import _PRODUCT_REVIEWS_DEADLINE_S

    per_call_worst_case = (
        adapter_module._MAX_ATTEMPTS * adapter_module._REQUEST_TIMEOUT_SECONDS
        + adapter_module._BACKOFF_CAP_SECONDS * (adapter_module._MAX_ATTEMPTS - 1)
    )
    two_sequential_calls_worst_case = per_call_worst_case * 2

    assert two_sequential_calls_worst_case < _PRODUCT_REVIEWS_DEADLINE_S, (
        f"aget_curated_reviews's two-sequential-call worst case "
        f"({two_sequential_calls_worst_case}s) must fit under the outer "
        f"wait_for deadline ({_PRODUCT_REVIEWS_DEADLINE_S}s), or the outer "
        f"timeout always fires before the adapter's own retries get a "
        f"chance to run."
    )
