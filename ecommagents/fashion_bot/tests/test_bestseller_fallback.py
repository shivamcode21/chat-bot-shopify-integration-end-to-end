"""
Tests for the merged bestseller fallback in search_products_pipeline.

Background
----------
``get_top_selling_products_tool`` was merged into ``search_products``. The
unique feature it provided — a live Shopify Orders scan when no products
are tagged as bestsellers in the vector index — is now integrated into
``search_products_pipeline`` as a last-resort fallback when
``has_bestseller_filter`` is True and all other relaxation steps still
yield 0 results.

These tests verify:
1. The fallback triggers only when bestseller filter is active and results are 0.
2. The fallback does NOT trigger when results already exist.
3. The fallback does NOT trigger for non-bestseller queries.
4. Fallback failures are caught gracefully (no crash, returns empty).
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_MODULE = "fashion_bot.services.recommendation.recommendation_service"


@dataclass
class _FakeQUResult:
    query: str = "trending shirts"
    filter: str = "bestseller = true AND in_stock = true"
    follow_up: Optional[str] = None
    sort_by: str = ""
    is_catalog_wide: bool = True


@dataclass
class _FakeQUResultNoBestseller:
    query: str = "black shirt"
    filter: str = "category = 'Shirts' AND in_stock = true"
    follow_up: Optional[str] = None
    sort_by: str = ""
    is_catalog_wide: bool = False


def _make_product(title: str, price: float = 999.0) -> Dict[str, Any]:
    return {
        "title": title,
        "price_min": price,
        "handle": title.lower().replace(" ", "-"),
        "product_url": f"https://shop.test/products/{title.lower().replace(' ', '-')}",
        "in_stock": True,
        "sizes": ["M", "L"],
        "colors": ["Black"],
    }


def _make_search_result(product: Dict[str, Any]) -> Dict[str, Any]:
    return {"content": product, "metadata": product, "score": 0.9}


def _patch_pipeline_deps(
    qu_result,
    search_results,
    aget_top_selling_return=None,
    aget_top_selling_side_effect=None,
):
    """Return a context manager that patches all dependencies of the pipeline."""
    import contextlib

    @contextlib.asynccontextmanager
    async def _ctx():
        mock_svc = MagicMock()
        mock_svc.asearch = AsyncMock(return_value=search_results)

        patches = [
            patch(f"{_MODULE}.understand_query", new_callable=AsyncMock, return_value=qu_result),
            patch(f"{_MODULE}.get_upstash_search_service", return_value=mock_svc),
            patch(f"{_MODULE}.aresolve_client_int", new_callable=AsyncMock, return_value=5),
            patch(f"{_MODULE}.rerank", side_effect=lambda results, **kw: results[:kw.get("max_results", 5)]),
            patch(
                "fashion_bot.utils.product_utils.aget_shopify_to_website_mapping",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ]

        aget_top_kwargs: Dict[str, Any] = {}
        if aget_top_selling_side_effect:
            aget_top_kwargs["side_effect"] = aget_top_selling_side_effect
        elif aget_top_selling_return is not None:
            aget_top_kwargs["return_value"] = aget_top_selling_return
        else:
            aget_top_kwargs["return_value"] = {"products": [], "found": False}

        patches.append(
            patch(
                "fashion_bot.core.orchestrator.ProductOrchestrator.aget_top_selling",
                new_callable=AsyncMock,
                **aget_top_kwargs,
            )
        )

        with contextlib.ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in patches]
            mock_aget_top = mocks[-1]
            yield mock_svc, mock_aget_top

    return _ctx()


@pytest.mark.asyncio
async def test_bestseller_fallback_triggers_on_empty_results():
    """When bestseller filter is active and search returns 0, the Shopify
    Orders fallback should fire and return products."""
    fallback_products = [_make_product("Top Seller Shirt")]

    async with _patch_pipeline_deps(
        qu_result=_FakeQUResult(),
        search_results=[],
        aget_top_selling_return={"products": fallback_products, "found": True},
    ) as (mock_svc, mock_aget_top):
        from fashion_bot.services.recommendation.recommendation_service import (
            search_products_pipeline,
        )

        result = await search_products_pipeline(
            query="show me trending products",
            client_id="test-client-123",
            max_results=5,
        )

        mock_aget_top.assert_called_once()
        assert len(result.products) > 0
        assert result.filters_relaxed is True
        assert result.is_bestseller is True


@pytest.mark.asyncio
async def test_bestseller_fallback_skipped_when_results_exist():
    """When search already has results, the Shopify Orders fallback
    should NOT be called."""
    existing_results = [_make_search_result(_make_product("Found Product"))]

    async with _patch_pipeline_deps(
        qu_result=_FakeQUResult(),
        search_results=existing_results,
    ) as (mock_svc, mock_aget_top):
        from fashion_bot.services.recommendation.recommendation_service import (
            search_products_pipeline,
        )

        result = await search_products_pipeline(
            query="show me trending products",
            client_id="test-client-123",
            max_results=5,
        )

        mock_aget_top.assert_not_called()
        assert len(result.products) > 0


@pytest.mark.asyncio
async def test_bestseller_fallback_skipped_for_non_bestseller_query():
    """Non-bestseller queries should never trigger the Shopify Orders fallback,
    even with 0 results."""
    async with _patch_pipeline_deps(
        qu_result=_FakeQUResultNoBestseller(),
        search_results=[],
    ) as (mock_svc, mock_aget_top):
        from fashion_bot.services.recommendation.recommendation_service import (
            search_products_pipeline,
        )

        result = await search_products_pipeline(
            query="black shirt",
            client_id="test-client-123",
            max_results=5,
        )

        mock_aget_top.assert_not_called()
        assert result.is_bestseller is False


@pytest.mark.asyncio
async def test_bestseller_fallback_graceful_on_exception():
    """If the Shopify Orders fallback raises, it should be caught and
    the pipeline should return empty results without crashing."""
    async with _patch_pipeline_deps(
        qu_result=_FakeQUResult(),
        search_results=[],
        aget_top_selling_side_effect=RuntimeError("Shopify API timeout"),
    ) as (mock_svc, mock_aget_top):
        from fashion_bot.services.recommendation.recommendation_service import (
            search_products_pipeline,
        )

        result = await search_products_pipeline(
            query="show me trending products",
            client_id="test-client-123",
            max_results=5,
        )

        mock_aget_top.assert_called_once()
        assert result.products == []
        assert result.filters_relaxed is False
