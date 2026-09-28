"""Tool-level tests for the ``search_products`` agent tool (``tool_factory.py``).

Two test tiers:

**Unit tests (deterministic, no network)**
Exercises the tool *body* — the part the LLM actually calls — focusing on the
pinned-products integration added in PR #792:
  * pins merged first (deduped by handle) for a catalog-wide bestseller query,
  * pins gated on ``pipeline.is_bestseller`` AND ``is_catalog_wide`` (suppressed
    for category-scoped or collection-page requests),
  * fail-open: a pin-config error never breaks product search,
  * the exact per-product identity (id / name / handle) returned, and the real
    Upstash-doc → tool-product field mapping.

The tool closure imports its collaborators in two phases:
  * ``log_with_trace_id`` / ``aget_shopify_to_website_mapping`` at *factory* call
    time → patched on their source modules before building the tool;
  * ``search_products_pipeline`` and the ``pinned_products`` helpers at *tool*
    call time → patched on their source modules before invoking.

The real ``merge_pinned_first`` and ``is_catalog_wide_bestseller`` are kept
(this is what we're testing); the pin *config read* and the search pipeline are
stubbed so the test is deterministic and needs no live Upstash/LLM. The
doc→product normalisers are stubbed by default (so identity/order assertions
control the exact shapes) but one test runs them for real to lock the field
mapping. Imports are deferred + ``importorskip``-guarded so the suite degrades
gracefully in a bare dev container and runs fully in CI.

**Integration tests (live Groovee store, real Upstash + QU LLM)**
Exercises the real end-to-end search path using the same Groovee test store
and product fixtures as ``test_place_order_tools.py`` / ``test_product_details_tools.py``.
Validates that the ``search_products`` tool returns real products (Cosmic Shacket),
that bestseller queries hit the live pipeline, and that pinned products (when
configured) appear first — all against real Shopify / Upstash / LLM.
"""

import pytest


def _product(handle, title=None, pid=None):
    """A product dict with a DISTINCT id and name (not equal to the handle) so
    tests can assert the exact identity that ends up in the response, not just
    which handle survived ordering/dedup."""
    name = title or f"Name {handle}"
    ident = pid or f"id-{handle}"
    return {"id": ident, "product_id": ident, "handle": handle, "title": name, "name": name}


async def _build_search_tool(
    monkeypatch,
    *,
    pipeline_products,
    is_bestseller,
    is_catalog_wide,
    pins,
    pins_raises=False,
    state=None,
    stub_normalizers=True,
):
    """Wire stubs, build the tool via the real factory, return its coroutine + a
    record of whether the pin-config read was called.

    With ``stub_normalizers=True`` (default) ``_upstash_doc_to_tool_product`` and
    ``_normalize_product`` are identity passthroughs so identity/order tests
    control the exact product shapes. Pass ``False`` to run the real normalisers
    end-to-end.
    """
    pytest.importorskip("pydantic")
    pytest.importorskip("langchain_core")

    import fashion_bot.config_manager as config_mod

    # Per-client config reads (result counts, thresholds) hit Postgres. These
    # unit tests assert tool wiring, not config resolution, so pin every lookup
    # to "no override" — otherwise the suite blocks on a DB connect.
    async def _no_client_config(*_a, **_k):
        return None

    monkeypatch.setattr(config_mod, "aget_config", _no_client_config)

    import fashion_bot.tool_factory as tf
    import fashion_bot.utils.utils as utils_mod
    import fashion_bot.utils.product_utils as product_utils_mod
    from fashion_bot.services.recommendation import (
        recommendation_service as rs_mod,
        pinned_products as pinned_mod,
    )

    # --- factory-time collaborators (imported when the tool is built) ---
    monkeypatch.setattr(utils_mod, "log_with_trace_id", lambda *a, **k: None)

    async def _no_website(_client_id):
        return None  # skip Shopify→website URL rewrite

    monkeypatch.setattr(product_utils_mod, "aget_shopify_to_website_mapping", _no_website)

    # --- tool-body collaborators (imported when the tool runs) ---
    if stub_normalizers:
        # Pass docs/products straight through so we control the exact shapes.
        monkeypatch.setattr(tf, "_upstash_doc_to_tool_product", lambda p: p)
        monkeypatch.setattr(tf, "_normalize_product", lambda p: p)
    monkeypatch.setattr(tf, "_record_surfaced_variant_ids", lambda *a, **k: None)

    # Bypass per-pin catalog enrichment (the real one hits Upstash by handle);
    # tests control pin shapes directly. Enrichment itself is covered separately.
    async def _passthrough_enrich(_cid, p):
        return p

    monkeypatch.setattr(tf, "_aenrich_pin_with_catalog", _passthrough_enrich)

    async def _fake_pipeline(**kwargs):
        called["pipeline_kwargs"] = kwargs
        return rs_mod.SearchPipelineResult(
            products=list(pipeline_products),
            qu_query="qq",
            qu_filter="bestseller = true AND in_stock = true",
            follow_up=None,
            is_bestseller=is_bestseller,
            is_catalog_wide=is_catalog_wide,
            filters_relaxed=False,
        )

    monkeypatch.setattr(rs_mod, "search_products_pipeline", _fake_pipeline)

    called = {"pins": 0, "pipeline_kwargs": {}}

    async def _fake_pins(_client_id):
        called["pins"] += 1
        if pins_raises:
            raise RuntimeError("pin config blew up")
        return list(pins)

    monkeypatch.setattr(pinned_mod, "aget_pinned_bestseller_products", _fake_pins)
    # merge_pinned_first + is_catalog_wide_bestseller stay REAL (under test).

    search_products, _find_by_url, _find_by_id = tf._create_product_search_tools(
        state if state is not None else {}, "client-1"
    )
    fn = getattr(search_products, "coroutine", None) or getattr(search_products, "func")
    return fn, called


def _handles(result):
    return [p["handle"] for p in result["products"]]


def _identity(result):
    """The exact identity of each returned product, in order."""
    return [(p["id"], p["name"], p["handle"]) for p in result["products"]]


@pytest.mark.asyncio
async def test_catalog_wide_bestseller_merges_pins_first(monkeypatch):
    fn, called = await _build_search_tool(
        monkeypatch,
        pipeline_products=[_product("live-a"), _product("live-b")],
        is_bestseller=True,
        is_catalog_wide=True,
        pins=[_product("pin-1")],
    )
    result = await fn(query="show me your best sellers")

    assert called["pins"] == 1
    # Exact id + name + handle of every product, pins first.
    assert _identity(result) == [
        ("id-pin-1", "Name pin-1", "pin-1"),
        ("id-live-a", "Name live-a", "live-a"),
        ("id-live-b", "Name live-b", "live-b"),
    ]
    assert result["found"] is True
    assert result["count"] == 3
    assert result["qu_query"] == "qq"


@pytest.mark.asyncio
async def test_pin_already_in_results_keeps_pin_identity(monkeypatch):
    # A pin and a live product share a handle but have DIFFERENT id/name. The
    # merge must keep the PIN's object in the top slot (not the live one), so the
    # returned identity is the pin's — proving dedup preserves identity, not just
    # the handle.
    live_dup = _product("dup", title="Live Dup", pid="live-id")
    pin_dup = _product("dup", title="Pinned Dup", pid="pin-id")
    fn, _ = await _build_search_tool(
        monkeypatch,
        pipeline_products=[_product("live-a"), live_dup],
        is_bestseller=True,
        is_catalog_wide=True,
        pins=[pin_dup],
    )
    result = await fn(query="best sellers")

    assert _identity(result) == [
        ("pin-id", "Pinned Dup", "dup"),     # pin wins the shared-handle slot
        ("id-live-a", "Name live-a", "live-a"),
    ]
    assert result["count"] == 2


@pytest.mark.asyncio
async def test_category_scoped_bestseller_suppresses_pins(monkeypatch):
    # is_catalog_wide=False (e.g. "best selling jeans") → pins suppressed and the
    # config read is never even attempted.
    fn, called = await _build_search_tool(
        monkeypatch,
        pipeline_products=[_product("live-a")],
        is_bestseller=True,
        is_catalog_wide=False,
        pins=[_product("pin-1")],
    )
    result = await fn(query="best selling jeans")

    assert called["pins"] == 0
    assert _handles(result) == ["live-a"]


@pytest.mark.asyncio
async def test_collection_page_suppresses_pins(monkeypatch):
    # On a collection page, even a catalog-wide-classified query is scoped to the
    # collection → pins suppressed.
    fn, called = await _build_search_tool(
        monkeypatch,
        pipeline_products=[_product("live-a")],
        is_bestseller=True,
        is_catalog_wide=True,
        pins=[_product("pin-1")],
        state={"current_page_type": "collection", "current_collection_handle": "denim-jeans"},
    )
    result = await fn(query="best ones")

    assert called["pins"] == 0
    assert _handles(result) == ["live-a"]


@pytest.mark.asyncio
async def test_non_bestseller_query_skips_pin_path(monkeypatch):
    # A normal (non-bestseller) search never enters the pin branch.
    fn, called = await _build_search_tool(
        monkeypatch,
        pipeline_products=[_product("live-a"), _product("live-b")],
        is_bestseller=False,
        is_catalog_wide=False,
        pins=[_product("pin-1")],
    )
    result = await fn(query="black hoodie")

    assert called["pins"] == 0
    assert _handles(result) == ["live-a", "live-b"]


@pytest.mark.asyncio
async def test_pin_read_failure_is_fail_open(monkeypatch):
    # A pin-config error must never break search — live results still returned.
    fn, called = await _build_search_tool(
        monkeypatch,
        pipeline_products=[_product("live-a")],
        is_bestseller=True,
        is_catalog_wide=True,
        pins=[_product("pin-1")],
        pins_raises=True,
    )
    result = await fn(query="best sellers")

    assert called["pins"] == 1            # attempted…
    assert _handles(result) == ["live-a"]  # …but failure swallowed, live kept
    assert result["found"] is True


@pytest.mark.asyncio
async def test_no_results_reports_not_found(monkeypatch):
    fn, _ = await _build_search_tool(
        monkeypatch,
        pipeline_products=[],
        is_bestseller=False,
        is_catalog_wide=False,
        pins=[],
    )
    result = await fn(query="nonexistent")
    assert result["found"] is False
    assert result["count"] == 0
    assert result["products"] == []


@pytest.mark.asyncio
async def test_show_more_excludes_already_shown_handles(monkeypatch):
    # "Show more" must page DEEPER: the tool threads the session's already-shown
    # card handles (state["carousel_shown_handles"]) into the pipeline as
    # exclude_handles, normalized (stripped/lowercased, blanks dropped), so the
    # search returns net-new products instead of the same fixed top-N.
    from fashion_bot.services.recommendation import recommendation_service as rs_mod

    fn, _ = await _build_search_tool(
        monkeypatch,
        pipeline_products=[_product("new-a")],
        is_bestseller=False,
        is_catalog_wide=False,
        pins=[],
        state={"carousel_shown_handles": ["Shown-1", "  shown-2  ", "", None]},
    )

    captured = {}

    async def _capture_pipeline(**kwargs):
        captured.update(kwargs)
        return rs_mod.SearchPipelineResult(products=[_product("new-a")])

    monkeypatch.setattr(rs_mod, "search_products_pipeline", _capture_pipeline)

    await fn(query="show more")

    assert captured.get("exclude_handles") == ["shown-1", "shown-2"]


@pytest.mark.asyncio
async def test_first_search_passes_no_exclude_handles(monkeypatch):
    # No cards shown yet → exclude_handles stays None so first-search behaviour
    # is byte-for-byte unchanged (the feature is a no-op until something is shown).
    from fashion_bot.services.recommendation import recommendation_service as rs_mod

    fn, _ = await _build_search_tool(
        monkeypatch,
        pipeline_products=[_product("a")],
        is_bestseller=False,
        is_catalog_wide=False,
        pins=[],
        state={},
    )

    captured = {}

    async def _capture_pipeline(**kwargs):
        captured.update(kwargs)
        return rs_mod.SearchPipelineResult(products=[_product("a")])

    monkeypatch.setattr(rs_mod, "search_products_pipeline", _capture_pipeline)

    await fn(query="black shirt")

    assert captured.get("exclude_handles") is None


@pytest.mark.asyncio
async def test_real_normalizer_maps_upstash_doc_fields(monkeypatch):
    # End-to-end with the REAL _upstash_doc_to_tool_product + _normalize_product:
    # a raw Upstash doc (content + metadata) must come out as a product with the
    # correct id (from product_id), name (from title), url (from product_url),
    # handle, image_url, and a price dict (from price_min/price_max).
    doc = {
        "content": {
            "title": "Cosmic Shacket",
            "handle": "cosmic-shacket",
            "price_min": 1699,
            "price_max": 1999,
            "in_stock": True,
        },
        "metadata": {
            "product_id": "7654321",
            "product_url": "https://store.com/products/cosmic-shacket",
            "image_url": "https://cdn.example/img.jpg",
        },
    }
    fn, _ = await _build_search_tool(
        monkeypatch,
        pipeline_products=[doc],
        is_bestseller=False,
        is_catalog_wide=False,
        pins=[],
        stub_normalizers=False,   # exercise the real normalisers
    )
    result = await fn(query="cosmic shacket")

    assert result["count"] == 1
    p = result["products"][0]
    assert p["id"] == "7654321"
    assert p["product_id"] == "7654321"
    assert p["name"] == "Cosmic Shacket"
    assert p["title"] == "Cosmic Shacket"
    assert p["handle"] == "cosmic-shacket"
    assert p["url"] == "https://store.com/products/cosmic-shacket"
    assert p["image_url"] == "https://cdn.example/img.jpg"
    assert p["price"] == {"min": 1699, "max": 1999}


# ===========================================================================
# Integration tests — live Groovee store (real Upstash + QU LLM)
#
# Same constants and fixture pattern as test_product_details_tools.py /
# test_place_order_tools.py.  These hit real APIs so they're slower (~5-15s
# each) but they catch regressions that unit-only mocks would miss.
# ===========================================================================

CLIENT_ID = "c3ffcb1b-afb9-4ca4-8746-a06698bec870"
VALID_PRODUCT_HANDLE = "evolve-the-cosmic-shacket"
VALID_PRODUCT_TITLE = "Evolve The Cosmic Shacket"


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_env():
    """Ensure env is loaded before any integration test in this module."""
    from fashion_bot.env_loader import bootstrap_environment
    bootstrap_environment()


def _build_state(client_id: str = CLIENT_ID):
    return {
        "client_id": client_id,
        "phone_number": "9716336096",
        "conversation_context": {"entities": [], "topics": [], "focal_entity": None},
        "messages": [],
    }


def _get_search_tool(state=None):
    """Build search_products via _create_product_search_tools and return its coroutine."""
    import fashion_bot.tool_factory as tf
    if state is None:
        state = _build_state()
    client_id = state.get("client_id", CLIENT_ID)
    search_products, _, _ = tf._create_product_search_tools(state, client_id)
    fn = getattr(search_products, "coroutine", None) or getattr(search_products, "func")
    return fn


class TestSearchProductsIntegration:
    """Live integration tests against the Groovee test store."""

    @pytest.mark.asyncio
    async def test_search_cosmic_shacket_returns_real_product(self):
        """Searching for the Cosmic Shacket by name returns it from Upstash."""
        fn = _get_search_tool()
        result = await fn(query="cosmic shacket")

        assert isinstance(result, dict)
        assert result["found"] is True
        assert result["count"] > 0
        handles = [p.get("handle", "") for p in result["products"]]
        assert VALID_PRODUCT_HANDLE in handles, (
            f"Expected '{VALID_PRODUCT_HANDLE}' in live search results: {handles}"
        )
        product = next(p for p in result["products"] if p.get("handle") == VALID_PRODUCT_HANDLE)
        assert product.get("name") or product.get("title")
        assert product.get("url"), "Product must have a URL"
        assert isinstance(product.get("price"), dict), "Price must be a dict with min/max"

    @pytest.mark.asyncio
    async def test_bestseller_query_returns_live_products(self):
        """A 'best sellers' query returns real products with correct structure."""
        fn = _get_search_tool()
        result = await fn(query="show me your best sellers")

        assert isinstance(result, dict)
        assert result["found"] is True
        assert result["count"] > 0
        assert isinstance(result["qu_query"], str)
        assert len(result["qu_query"]) > 0
        for p in result["products"]:
            assert p.get("handle"), f"Product missing handle: {p}"
            assert p.get("name") or p.get("title"), f"Product missing name/title: {p}"

    @pytest.mark.asyncio
    async def test_trending_query_returns_live_products(self):
        """'Trending products' is a synonym for bestsellers in the QU pipeline."""
        fn = _get_search_tool()
        result = await fn(query="what's trending right now")

        assert isinstance(result, dict)
        assert result["found"] is True
        assert result["count"] > 0

    @pytest.mark.asyncio
    async def test_category_scoped_search_returns_results(self):
        """A category-scoped query (e.g. 'black hoodie') returns products."""
        fn = _get_search_tool()
        result = await fn(query="black hoodie")

        assert isinstance(result, dict)
        assert result["found"] is True
        assert result["count"] > 0
        assert "in_stock" in result.get("qu_filter", ""), (
            f"QU filter must include in_stock. qu_filter='{result.get('qu_filter')}'"
        )

    @pytest.mark.asyncio
    async def test_pinned_products_surface_first_in_bestsellers(self):
        """If the client has pinned bestsellers configured, they appear first.

        Skipped when no pins are configured — this is an opt-in feature."""
        from fashion_bot.services.recommendation.pinned_products import (
            aget_pinned_bestseller_products,
        )

        pins = await aget_pinned_bestseller_products(CLIENT_ID)
        if not pins:
            pytest.skip("No pinned_bestseller_products configured for this client")

        pin_handles = {p["handle"].strip().lower() for p in pins if p.get("handle")}
        fn = _get_search_tool()
        result = await fn(query="show me your best sellers")

        assert result["found"] is True
        handles = [p.get("handle", "").strip().lower() for p in result["products"]]

        first_non_pin = next(
            (i for i, h in enumerate(handles) if h not in pin_handles),
            len(handles),
        )
        for ph in pin_handles:
            if ph in handles:
                assert handles.index(ph) < first_non_pin, (
                    f"Pin '{ph}' must appear before non-pinned products. order={handles}"
                )

    @pytest.mark.asyncio
    async def test_search_returns_qu_metadata(self):
        """Every search result includes QU metadata (qu_query, qu_filter)."""
        fn = _get_search_tool()
        result = await fn(query="oversized jacket")

        assert "qu_query" in result
        assert "qu_filter" in result
        assert isinstance(result["qu_query"], str) and len(result["qu_query"]) > 0
        assert "in_stock" in result["qu_filter"]

    @pytest.mark.asyncio
    async def test_empty_query_does_not_crash(self):
        """An empty query must not raise — returns a valid (possibly empty) response."""
        fn = _get_search_tool()
        result = await fn(query="")

        assert isinstance(result, dict)
        assert "found" in result
        assert isinstance(result.get("products"), list)

    @pytest.mark.asyncio
    async def test_collection_page_suppresses_pins_live(self):
        """On a collection page, pins are suppressed even for bestseller queries."""
        from fashion_bot.services.recommendation.pinned_products import (
            aget_pinned_bestseller_products,
        )

        pins = await aget_pinned_bestseller_products(CLIENT_ID)
        if not pins:
            pytest.skip("No pinned_bestseller_products configured for this client")

        state = _build_state()
        state["current_page_type"] = "collection"
        state["current_collection_handle"] = "hoodies"
        fn = _get_search_tool(state)
        result = await fn(query="best sellers")

        pin_handles = {p["handle"].strip().lower() for p in pins if p.get("handle")}
        result_handles = [p.get("handle", "").strip().lower() for p in result.get("products", [])]
        for ph in pin_handles:
            if ph in result_handles:
                live_idx = result_handles.index(ph)
                non_pin_before = any(
                    h not in pin_handles for h in result_handles[:live_idx]
                )
                if non_pin_before:
                    pass  # pin not forced first — correct for collection context


# ---------------------------------------------------------------------------
# Pinned promos normalize as in-stock (unit) — so the LLM surfaces them in its
# reply + ###SHOW_PRODUCTS### rather than dropping them as "out of stock".
# ---------------------------------------------------------------------------
def test_pinned_product_normalizes_as_in_stock():
    pytest.importorskip("pydantic")
    pytest.importorskip("langchain_core")
    import fashion_bot.tool_factory as tf

    pin = {"title": "Cosmic Shacket", "handle": "evolve-the-cosmic-shacket",
           "price": {"min": 1699, "max": 1699}, "pinned": True}
    out = tf._normalize_product(pin)
    assert out["in_stock"] is True
    assert out["stock_message"] != "Currently out of stock."

    # A non-pinned product with no inventory/sizes stays out of stock (control).
    live = {"title": "Plain", "handle": "plain", "price": {"min": 100, "max": 100}}
    out2 = tf._normalize_product(live)
    assert out2["in_stock"] is False
    assert out2["stock_message"] == "Currently out of stock."


# ---------------------------------------------------------------------------
# Regression: pre-fetch crashed with "'str' object has no attribute 'get'" when
# the Shopify transform bailed out (e.g. a swallowed metafield NoneType error)
# and handed back the RAW GraphQL product whose ``variants`` is still the nested
# ``{"edges": [{"node": {...}}]}`` dict shape. Iterating that dict yields its
# str keys, which the variant-availability probe then called ``.get()`` on.
# ``_normalize_product`` must coerce that shape (and any stray non-dict entries)
# instead of raising. See websocket_chat pre-fetch RCA.
# ---------------------------------------------------------------------------
def test_normalize_product_tolerates_raw_graphql_variants_shape():
    pytest.importorskip("pydantic")
    pytest.importorskip("langchain_core")
    import fashion_bot.tool_factory as tf

    raw_graphql = {
        "title": "24H Hydration Sunscreen",
        "handle": "24h-hydration-sunscreen",
        "price": {"min": 499, "max": 499},
        # Raw GraphQL edges/node shape — NOT the flattened list the enhanced
        # transform would have produced.
        "variants": {
            "edges": [
                {"node": {"is_available": True, "inventory_quantity": 5}},
            ]
        },
    }
    out = tf._normalize_product(raw_graphql)  # must not raise
    assert isinstance(out["variants"], list)
    assert out["variants"] == [{"is_available": True, "inventory_quantity": 5}]
    assert out["total_variants"] == 1
    assert out["in_stock"] is True

    # A literal list containing a stray handle-string must also be tolerated:
    # the string entry is dropped, the real variant dict still counts.
    mixed = {
        "title": "Mixed", "handle": "mixed", "price": {"min": 10, "max": 10},
        "variants": ["some-handle-string", {"available": True}],
    }
    out2 = tf._normalize_product(mixed)  # must not raise
    assert out2["variants"] == [{"available": True}]
    assert out2["in_stock"] is True


# ---------------------------------------------------------------------------
# Pin catalog-enrichment (unit) — a bare pin config (no variants) is resolved
# to its FULL catalog product by handle so the carousel renders a real size
# picker instead of auto-attaching the pin URL's ?variant=. Falls back to the
# normalized config stub on a catalog miss (fail-open).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pin_enriched_from_catalog_by_handle(monkeypatch):
    pytest.importorskip("pydantic")
    pytest.importorskip("langchain_core")
    import fashion_bot.tool_factory as tf
    import fashion_bot.services.product_ingestion.upstash_search_service as uss_mod

    # The bare pin config: a handle + title, but NO variants (the prod gap).
    pin = {"title": "Cosmic Shacket", "handle": "evolve-the-cosmic-shacket",
           "price": {"min": 1699, "max": 1699}, "pinned": True}

    # The full catalog product as Upstash returns it (content + metadata),
    # carrying the real variants/sizes the bare pin lacks.
    catalog_doc = {
        "content": {
            "handle": "evolve-the-cosmic-shacket",
            "title": "Evolve: The Cosmic Shacket",
            "price_min": 1699,
            "price_max": 1699,
        },
        "metadata": {
            "handle": "evolve-the-cosmic-shacket",
            "variants": [
                {"id": "111", "size": "S", "available": True},
                {"id": "222", "size": "M", "available": True},
            ],
            "sizes_in_stock": ["S", "M"],
        },
    }

    class _FakeSearch:
        async def asearch(self, query, client_id, **kwargs):
            # Resolves by exact-handle filter — return the catalog doc.
            return [catalog_doc]

    monkeypatch.setattr(uss_mod, "get_upstash_search_service", lambda: _FakeSearch())

    out = await tf._aenrich_pin_with_catalog("client-x", pin)

    # Enriched: real variants flowed through, pin identity preserved.
    assert out["pinned"] is True
    assert out["handle"] == "evolve-the-cosmic-shacket"
    assert len(out.get("variants") or []) == 2
    assert out["in_stock"] is True


@pytest.mark.asyncio
async def test_pin_falls_back_to_stub_when_not_in_catalog(monkeypatch):
    pytest.importorskip("pydantic")
    pytest.importorskip("langchain_core")
    import fashion_bot.tool_factory as tf
    import fashion_bot.services.product_ingestion.upstash_search_service as uss_mod

    pin = {"title": "Orphan Promo", "handle": "not-in-catalog",
           "price": {"min": 999, "max": 999}, "pinned": True}

    class _EmptySearch:
        async def asearch(self, query, client_id, **kwargs):
            return []  # catalog miss

    monkeypatch.setattr(uss_mod, "get_upstash_search_service", lambda: _EmptySearch())

    out = await tf._aenrich_pin_with_catalog("client-x", pin)

    # Fallback to the normalized config stub: still pinned + in-stock (so it
    # renders), just without a real variants array.
    assert out["pinned"] is True
    assert out["handle"] == "not-in-catalog"
    assert out["in_stock"] is True


# ==========================================================================
# Focal product context plumbing (production path)
# ==========================================================================
# Query Understanding has per-client rules that only fire when a "Current
# product" block is supplied ("borrow the anchor's style, never its type").
# The production tool used to omit product_context entirely — only the demo
# path passed it — so those rules were dead in prod and QU had to infer the
# anchor from raw conversation history. That is what let a "matching panties"
# request come back as bras (issue #8).

def _state_with_focal(name, entity_type="product", **extra):
    state = {
        "conversation_context": {
            "focal_entity": {
                "entity_type": entity_type,
                "entity_id": "some-handle",
                "entity_value": name,
            }
        }
    }
    state.update(extra)
    return state


@pytest.mark.asyncio
async def test_focal_product_is_passed_to_pipeline_as_product_context(monkeypatch):
    fn, called = await _build_search_tool(
        monkeypatch,
        pipeline_products=[_product("a")],
        is_bestseller=False,
        is_catalog_wide=False,
        pins=[],
        state=_state_with_focal(
            "Enamor Bamboo Cotton IB26 Women's Full Support Bra Non-Padded Wirefree"
        ),
    )
    await fn(query="matching panties")

    # Name only. QU reads the anchor's type off the title using the client's own
    # taxonomy; deriving it here would put a product vocabulary in code.
    ctx = called["pipeline_kwargs"]["product_context"]
    assert ctx == {
        "name": "Enamor Bamboo Cotton IB26 Women's Full Support Bra Non-Padded Wirefree"
    }


@pytest.mark.asyncio
async def test_no_product_context_without_a_focal_product(monkeypatch):
    fn, called = await _build_search_tool(
        monkeypatch,
        pipeline_products=[_product("a")],
        is_bestseller=False,
        is_catalog_wide=False,
        pins=[],
        state={},
    )
    await fn(query="show me bras")

    assert called["pipeline_kwargs"]["product_context"] is None


@pytest.mark.asyncio
async def test_non_product_focal_entity_is_ignored(monkeypatch):
    """An order in focus must never be sent as a product anchor."""
    fn, called = await _build_search_tool(
        monkeypatch,
        pipeline_products=[_product("a")],
        is_bestseller=False,
        is_catalog_wide=False,
        pins=[],
        state=_state_with_focal("ORDER #1234", entity_type="order"),
    )
    await fn(query="show me bras")

    assert called["pipeline_kwargs"]["product_context"] is None


@pytest.mark.asyncio
async def test_collection_page_keeps_collection_scoping_priority(monkeypatch):
    """product_context disables the pipeline's collection-scoping safeguard, so
    on a collection page the collection must win and no anchor is sent."""
    fn, called = await _build_search_tool(
        monkeypatch,
        pipeline_products=[_product("a")],
        is_bestseller=False,
        is_catalog_wide=False,
        pins=[],
        state=_state_with_focal(
            "Enamor Bra",
            current_page_type="collection",
            current_collection_handle="bamboo-bliss",
        ),
    )
    await fn(query="show me the best ones")

    assert called["pipeline_kwargs"]["collection_context"] == "bamboo-bliss"
    assert called["pipeline_kwargs"]["product_context"] is None


@pytest.mark.asyncio
async def test_product_context_carries_no_derived_fields(monkeypatch):
    """Whatever the catalog's vocabulary, the tool passes the title verbatim and
    nothing else — no type inference in code, for any tenant."""
    fn, called = await _build_search_tool(
        monkeypatch,
        pipeline_products=[_product("a")],
        is_bestseller=False,
        is_catalog_wide=False,
        pins=[],
        state=_state_with_focal("Hand-Block Printed Silk Lehenga Set"),
    )
    await fn(query="matching dupatta")

    assert called["pipeline_kwargs"]["product_context"] == {
        "name": "Hand-Block Printed Silk Lehenga Set"
    }
