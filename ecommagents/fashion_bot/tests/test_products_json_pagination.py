"""
Tests for the ``ProductsJsonService.fetch_active_products`` pagination
early-stop fix.

Background
----------
The original loop ran ``while True`` and only stopped when an empty page
came back (or after 100 pages = 25,000 products). When the caller passed
``max_products=N``, the cap was applied ONLY at the end via
``_cap_products`` — so for a catalog of, say, 8,000 products with
``max_products=5000``, the service would fetch the full 8,000 (32
pages over HTTP) and then discard 3,000.

Production evidence: client ``2585e68e-5ba3-4e97-8f15-0d379b1fc4ff``
onboarding fetched at least pages 1-15 (3,750+ products) for an
``ingest_products(max_products=5000)`` call. Trace
``83866e80ac01619bcfaa9f4dc45c69e9`` on 2026-05-26.

Fix
---
Stop pagination once we have fetched at least ``max_products * 2`` items.
The ``* 2`` keeps headroom for the in-stock prioritisation that happens
inside ``_cap_products`` (which sorts by ``(not in_stock,
-total_inventory)`` before slicing). For catalogs ≤ ``max_products * 2``
this is a no-op — they get drained as before.

These tests pin the early-stop semantics down. They monkeypatch
``_normalize_product`` to return a lightweight stub so we don't have to
construct full ``NormalizedProduct`` objects with every required field,
and they stub the HTTP client to control how many pages of products
the loop sees.
"""

from types import SimpleNamespace
from typing import List

import httpx
import pytest

from fashion_bot.services.product_ingestion.products_json_service import (
    ProductsJsonService,
)


# ── Lightweight stubs ────────────────────────────────────────────────────


def _make_stub_normalized_product(raw):
    """Stand-in for the heavy NormalizedProduct dataclass."""
    return SimpleNamespace(
        id=raw.get("id"),
        in_stock=raw.get("_in_stock", True),
        total_inventory=raw.get("_total_inventory", 1),
    )


class _FakeFetcher:
    """
    ``afetch_storefront_json`` stand-in driven by a per-test list of page
    payloads.

    On the Nth call, returns ``{"products": pages[N]}`` (or an empty-products
    payload once the list runs out, simulating the end of the catalog).
    Records how many times it was called so tests can assert pagination
    stopped at the right point.
    """

    def __init__(self, pages: List[List[dict]]):
        self._pages = list(pages)
        self.calls = 0

    async def __call__(self, url, params=None, **kwargs):
        self.calls += 1
        if self._pages:
            page = self._pages.pop(0)
        else:
            page = []  # storefront-end-of-catalog
        return {"products": page}


def _service_with_pages(monkeypatch, pages):
    """Wire up a ProductsJsonService whose HTTP layer returns `pages`."""
    fake_fetcher = _FakeFetcher(pages)

    # Patch the storefront fetch helper *as imported into*
    # products_json_service, so fetch_active_products gets our fake pages
    # instead of opening a real connection. Patching the helper rather than
    # httpx keeps the retry/backoff policy out of these pagination tests —
    # it has its own coverage in test_storefront_http.py.
    monkeypatch.setattr(
        "fashion_bot.services.product_ingestion.products_json_service"
        ".afetch_storefront_json",
        fake_fetcher,
    )
    # Pagination tests assert call counts, not wall-clock politeness.
    monkeypatch.setattr(
        "fashion_bot.services.product_ingestion.products_json_service.PER_PAGE_DELAY",
        0,
    )

    svc = ProductsJsonService(website_url="https://example.test")
    # Avoid instantiating the full NormalizedProduct dataclass.
    monkeypatch.setattr(svc, "_normalize_product", _make_stub_normalized_product)
    return svc, fake_fetcher


def _page_of(n: int, in_stock: bool = True, total_inventory: int = 1, id_offset: int = 0):
    """Generate a single page of N stub raw-product dicts."""
    return [
        {"id": id_offset + i, "_in_stock": in_stock, "_total_inventory": total_inventory}
        for i in range(n)
    ]


# ── Behaviour tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_cap_drains_entire_catalog(monkeypatch):
    """
    Sanity: with max_products=0 (no cap), the loop runs until an empty
    page comes back. The early-stop must NOT trigger.
    """
    # 3 pages of 250 then end-of-catalog
    pages = [_page_of(250, id_offset=i * 250) for i in range(3)]
    svc, fake = _service_with_pages(monkeypatch, pages)

    result = await svc.fetch_active_products(max_products=0)

    # All 750 returned, 4 HTTP calls (3 with products + 1 that returns empty)
    assert len(result) == 750
    assert fake.calls == 4


@pytest.mark.asyncio
async def test_cap_drains_when_catalog_smaller_than_2x_cap(monkeypatch):
    """
    Heuristic boundary: when the catalog is smaller than ``max_products*2``
    the early-stop should NOT fire — we drain everything as before, then
    cap. Pins down that the fix doesn't regress behaviour for small
    catalogs.
    """
    # Catalog = 600 products, max_products = 500 → 2*cap = 1000 > 600
    pages = [_page_of(250), _page_of(250), _page_of(100)]
    svc, fake = _service_with_pages(monkeypatch, pages)

    result = await svc.fetch_active_products(max_products=500)

    # All 600 fetched, final cap slices to 500.
    assert len(result) == 500
    # 3 calls: the third page returns 100 < 250, which marks the end of the
    # catalog, so no empty-page probe is needed. The loop used to pay that
    # extra round trip on every catalog; on a rate-limited storefront it
    # could fail and discard the pages already fetched.
    assert fake.calls == 3


@pytest.mark.asyncio
async def test_early_stop_triggers_at_2x_cap_for_large_catalog(monkeypatch):
    """
    The motivating production scenario. With ``max_products=5000`` and a
    catalog much larger than 10,000 (here simulated by 80 pages of 250 =
    20,000 available), pagination must stop once we have >= 10,000
    fetched — and the test verifies we did NOT walk past that point.
    """
    pages = [_page_of(250, id_offset=i * 250) for i in range(80)]
    svc, fake = _service_with_pages(monkeypatch, pages)

    result = await svc.fetch_active_products(max_products=5000)

    # Final cap slices to 5000
    assert len(result) == 5000
    # Loop did NOT consume 80 pages — it stopped once accumulated >= 10000.
    # 10000 / 250 = 40 pages exactly, so we expect <= ~41 calls (one extra
    # ok if the check happens at top of loop after the page that pushed
    # us past the boundary).
    assert fake.calls <= 41, (
        f"Expected pagination to stop ~40 pages; got {fake.calls} "
        f"(early-stop not firing?)"
    )
    # And clearly fewer than the 80 the legacy code would have made.
    assert fake.calls < 80


@pytest.mark.asyncio
async def test_early_stop_exactly_at_boundary(monkeypatch):
    """
    Catalog = exactly 2 * max_products. The early-stop check is at the
    TOP of the loop so we should fetch all 2x pages then stop on the
    next iteration — i.e. NO empty-page probe. This pins the
    "no wasted call at the boundary" property.
    """
    pages = [_page_of(250, id_offset=i * 250) for i in range(40)]  # = 10000 products
    svc, fake = _service_with_pages(monkeypatch, pages)

    result = await svc.fetch_active_products(max_products=5000)

    assert len(result) == 5000
    # We need all 40 pages to reach 10000; the 41st iteration check
    # short-circuits before any HTTP call.
    assert fake.calls == 40


# ── In-stock prioritisation invariant ────────────────────────────────────


@pytest.mark.asyncio
async def test_in_stock_prioritisation_preserved_within_early_stop_window(monkeypatch):
    """
    The whole reason for the ``* 2`` headroom is so the in-stock sort
    inside _cap_products still has something useful to work with.

    Setup: page 1 = 250 out-of-stock, page 2 = 250 in-stock. With
    max_products=200 and cap*2=400, BOTH pages get fetched (500 total
    after page 2), the sort puts in-stock first, kept slice = 200
    in-stock items.

    Without the ``* 2`` headroom (early-stop at exactly cap), the loop
    would stop after page 1 and the returned items would all be out of
    stock — the worse outcome the docstring warns about.

    Note: the cap MUST be larger than the page size (250) for the
    headroom heuristic to provide multi-page protection. A cap below
    page size gives at most one page of options regardless. See
    ``test_small_cap_falls_back_to_first_page_only`` for that edge case.
    """
    pages = [
        _page_of(250, in_stock=False, id_offset=0),     # page 1: out of stock
        _page_of(250, in_stock=True, id_offset=250),    # page 2: in stock
    ]
    svc, fake = _service_with_pages(monkeypatch, pages)

    result = await svc.fetch_active_products(max_products=200)

    assert len(result) == 200
    # Every kept item should be in-stock thanks to the sort over the
    # 2x-cap window. If the early-stop were at 1x cap, all 200 kept
    # items would be from page 1 (in_stock=False).
    assert all(p.in_stock for p in result), (
        "Early-stop window must be wide enough for in-stock prioritisation"
    )


@pytest.mark.asyncio
async def test_small_cap_falls_back_to_first_page_only(monkeypatch):
    """
    Edge case the docstring acknowledges: if ``max_products * 2`` is
    smaller than the storefront's per-page size (250), the early-stop
    fires after the first page regardless of stock status. Documenting
    this here so a future reader (and a future heuristic-tweak PR)
    knows it's intentional, not a regression.

    With max_products=50 and cap*2=100 < page_size=250, the first page
    alone satisfies the early-stop. If that first page happens to be
    all out-of-stock, the in-stock items on page 2 are never seen.

    The pragmatic justification: callers asking for ≤ ~125 products are
    usually doing a quick sample (prompt generation, smoke tests, etc.)
    where the latency and bandwidth saved by stopping early outweighs
    the worse stock prioritisation.
    """
    pages = [
        _page_of(250, in_stock=False, id_offset=0),     # page 1: all OOS
        _page_of(250, in_stock=True, id_offset=250),    # page 2 never reached
    ]
    svc, fake = _service_with_pages(monkeypatch, pages)

    result = await svc.fetch_active_products(max_products=50)

    assert len(result) == 50
    assert fake.calls == 1  # stopped after page 1 alone
    # All from page 1, which was 100% out-of-stock — heuristic limitation
    assert all(not p.in_stock for p in result)


@pytest.mark.asyncio
async def test_empty_first_page_returns_empty_without_loop(monkeypatch):
    """
    Defensive: an empty first response (e.g. storefront returns no
    products) must return empty cleanly — no early-stop confusion.
    """
    pages = []  # FakeAsyncClient will return empty on first call
    svc, fake = _service_with_pages(monkeypatch, pages)

    result = await svc.fetch_active_products(max_products=5000)

    assert result == []
    assert fake.calls == 1


# ── Failure / termination behaviour ──────────────────────────────────────


@pytest.mark.asyncio
async def test_short_page_ends_pagination_without_an_extra_request(monkeypatch):
    """
    A page shorter than the 250-item page size is the last page. Probing one
    page past it wastes a request against a storefront that may rate-limit
    it — which is exactly how a fully-fetched Clarks catalog was thrown away.
    """
    svc, fake = _service_with_pages(monkeypatch, [_page_of(147)])

    result = await svc.fetch_active_products(max_products=5000)

    assert len(result) == 147
    assert fake.calls == 1  # no page-2 confirmation request


@pytest.mark.asyncio
async def test_full_page_still_probes_the_next_page(monkeypatch):
    """A full page is ambiguous, so the next page must still be requested."""
    svc, fake = _service_with_pages(monkeypatch, [_page_of(250), _page_of(10, id_offset=250)])

    result = await svc.fetch_active_products(max_products=5000)

    assert len(result) == 260
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_later_page_failure_keeps_already_fetched_products(monkeypatch):
    """
    Losing page N must not discard pages 1..N-1. Under force_refresh a raise
    here meant the client's index was cleared with nothing to replace it.
    """
    calls = {"n": 0}

    async def _flaky(url, params=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"products": _page_of(250)}
        raise httpx.HTTPStatusError(
            "429", request=httpx.Request("GET", url), response=httpx.Response(429)
        )

    monkeypatch.setattr(
        "fashion_bot.services.product_ingestion.products_json_service"
        ".afetch_storefront_json",
        _flaky,
    )
    monkeypatch.setattr(
        "fashion_bot.services.product_ingestion.products_json_service.PER_PAGE_DELAY", 0
    )
    svc = ProductsJsonService(website_url="https://example.test")
    monkeypatch.setattr(svc, "_normalize_product", _make_stub_normalized_product)

    result = await svc.fetch_active_products(max_products=5000)

    assert len(result) == 250


@pytest.mark.asyncio
async def test_first_page_failure_still_raises(monkeypatch):
    """With nothing fetched there is no partial catalog to salvage."""

    async def _always_fails(url, params=None, **kwargs):
        raise httpx.HTTPStatusError(
            "429", request=httpx.Request("GET", url), response=httpx.Response(429)
        )

    monkeypatch.setattr(
        "fashion_bot.services.product_ingestion.products_json_service"
        ".afetch_storefront_json",
        _always_fails,
    )
    svc = ProductsJsonService(website_url="https://example.test")

    with pytest.raises(httpx.HTTPStatusError):
        await svc.fetch_active_products(max_products=5000)
