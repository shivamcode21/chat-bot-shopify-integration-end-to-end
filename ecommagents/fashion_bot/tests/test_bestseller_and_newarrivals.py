"""Bestseller-refresh + new-arrivals (sort-by-newest) tests.

Two test tiers:

**Unit tests (deterministic, no network)**
Added in response to PR #792 review (conceptgroove1): cover the new services —
Upstash Search fetch/write, the bestseller reconciliation, and the
sort-by-newest recency plumbing — rather than relying on ad-hoc logic checks.

Layout:
  * Pure-logic tests (``iso_to_epoch``, the pin gate) run anywhere.
  * Tests that touch the service classes import the product-ingestion /
    recommendation packages, whose ``__init__`` pulls optional deps (pydantic,
    upstash-search). Those guard with ``pytest.importorskip`` so the suite
    degrades gracefully in a bare dev container and runs fully in CI.

``upstash-search`` has no async client, so the service wraps the sync index in
``asyncio.to_thread``; the fakes below are plain sync objects, which is exactly
what ``to_thread`` expects.

**Integration tests (live Groovee store, real Shopify + Upstash)**
Exercises the real Shopify sales-volume API, Upstash Search reads, and the
full search_products_pipeline against the same Groovee store and product
fixtures as ``test_place_order_tools.py`` / ``test_product_details_tools.py``.
"""

from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# 1. iso_to_epoch — the numeric created_at_ts that sort-by-newest filters on.
# ---------------------------------------------------------------------------
def test_iso_to_epoch_variants():
    from fashion_bot.utils.shared_utils import iso_to_epoch

    # 'Z' suffix and an explicit +00:00 offset denote the same instant.
    assert iso_to_epoch("2026-01-01T00:00:00Z") == iso_to_epoch(
        "2026-01-01T00:00:00+00:00"
    )
    # tz-aware offset honored: +05:30 is 5h30m *earlier* in UTC than the same
    # wall-clock time at Z.
    z = iso_to_epoch("2026-04-16T11:24:32Z")
    ist = iso_to_epoch("2026-04-16T11:24:32+05:30")
    assert z - ist == 5 * 3600 + 30 * 60
    # A naive timestamp is assumed UTC.
    assert iso_to_epoch("2026-01-01T00:00:00") == iso_to_epoch("2026-01-01T00:00:00Z")
    # Unparseable / empty / None → None (caller treats as "recency unknown").
    assert iso_to_epoch("not-a-date") is None
    assert iso_to_epoch("") is None
    assert iso_to_epoch(None) is None


# ---------------------------------------------------------------------------
# 2. _created_after_epoch — the in-code recency net for sort-by-newest.
#    (Sort only orders; this is what actually *bounds* age, including for docs
#    missing created_at_ts that slipped past the Upstash numeric filter.)
# ---------------------------------------------------------------------------
def test_created_after_epoch_recency_net():
    pytest.importorskip("pydantic")  # recommendation_service import chain
    from fashion_bot.services.recommendation.recommendation_service import (
        _created_after_epoch,
    )

    cutoff = 1_700_000_000  # 2023-11-14T...

    # Primary signal: created_at_ts in content.
    assert _created_after_epoch({"content": {"created_at_ts": cutoff + 10}}, cutoff)
    assert not _created_after_epoch({"content": {"created_at_ts": cutoff - 10}}, cutoff)
    # Boundary is inclusive (>=).
    assert _created_after_epoch({"content": {"created_at_ts": cutoff}}, cutoff)

    # Secondary: created_at_ts in metadata.
    assert _created_after_epoch({"metadata": {"created_at_ts": cutoff + 10}}, cutoff)

    # Fallback: ISO created_at string when the numeric field is absent
    # (un-backfilled doc) — exactly the case sort-alone would let leak through.
    assert _created_after_epoch({"content": {"created_at": "2099-01-01T00:00:00Z"}}, cutoff)
    assert not _created_after_epoch({"content": {"created_at": "2000-01-01T00:00:00Z"}}, cutoff)

    # Neither signal → not recent (excluded; recency can't be confirmed).
    assert not _created_after_epoch({"content": {}, "metadata": {}}, cutoff)
    # Garbage ts → excluded, never raises.
    assert not _created_after_epoch({"content": {"created_at_ts": "oops"}}, cutoff)


# ---------------------------------------------------------------------------
# 3. is_catalog_wide_bestseller — pin gate; LLM flag is the single source of
#    truth, with the collection-page context as the only override.
# ---------------------------------------------------------------------------
def test_is_catalog_wide_bestseller_gate():
    pytest.importorskip("pydantic")  # recommendation package __init__
    from fashion_bot.services.recommendation.pinned_products import (
        is_catalog_wide_bestseller,
    )

    # LLM flag drives the decision.
    assert is_catalog_wide_bestseller(True) is True
    assert is_catalog_wide_bestseller(False) is False
    # A collection-page context suppresses pins even when the LLM said catalog-wide
    # ("best sellers" on a collection page means within that collection).
    assert is_catalog_wide_bestseller(True, collection_context="shirts") is False
    # No collection context + flag False → still suppressed.
    assert is_catalog_wide_bestseller(False, collection_context=None) is False


# ---------------------------------------------------------------------------
# 3b. New-arrivals window — configurable per client + user-driven timeframe.
#     Precedence: explicit user timeframe > per-client config > env default,
#     all clamped to [MIN, MAX]. No hardcoded window.
# ---------------------------------------------------------------------------
def test_clamp_window_days():
    pytest.importorskip("pydantic")  # recommendation_service import chain
    from fashion_bot.services.recommendation import recommendation_service as rs

    assert rs._clamp_window_days(rs.NEW_ARRIVAL_MAX_WINDOW_DAYS + 100) == rs.NEW_ARRIVAL_MAX_WINDOW_DAYS
    assert rs._clamp_window_days(0) == rs.NEW_ARRIVAL_MIN_WINDOW_DAYS
    assert rs._clamp_window_days(-5) == rs.NEW_ARRIVAL_MIN_WINDOW_DAYS
    assert rs._clamp_window_days(30) == 30


@pytest.mark.asyncio
async def test_new_arrival_window_precedence(monkeypatch):
    pytest.importorskip("pydantic")
    import fashion_bot.config_manager as cfg_mod
    from fashion_bot.services.recommendation import recommendation_service as rs

    configured = {"value": None}

    async def fake_aget_config(key, default=None, client_id=None):
        assert key == rs.NEW_ARRIVAL_WINDOW_CONFIG_KEY
        return configured["value"]

    monkeypatch.setattr(cfg_mod, "aget_config", fake_aget_config)

    # 1. Explicit user timeframe wins over a configured client default.
    configured["value"] = "90"
    assert await rs._aresolve_new_arrival_window_days("c", 30) == 30

    # 2. No explicit timeframe → per-client configured window.
    assert await rs._aresolve_new_arrival_window_days("c", None) == 90

    # 3. No explicit + no config → env-driven global default.
    configured["value"] = None
    assert await rs._aresolve_new_arrival_window_days("c", None) == rs.NEW_ARRIVAL_DEFAULT_WINDOW_DAYS

    # 4. No client_id → default (never a cross-tenant read).
    assert await rs._aresolve_new_arrival_window_days(None, None) == rs.NEW_ARRIVAL_DEFAULT_WINDOW_DAYS

    # 5. Non-integer config value → fail open to the default.
    configured["value"] = "not-a-number"
    assert await rs._aresolve_new_arrival_window_days("c", None) == rs.NEW_ARRIVAL_DEFAULT_WINDOW_DAYS

    # 6. Absurd explicit values are clamped to the bounds.
    assert await rs._aresolve_new_arrival_window_days("c", 10 ** 9) == rs.NEW_ARRIVAL_MAX_WINDOW_DAYS
    assert await rs._aresolve_new_arrival_window_days("c", -5) == rs.NEW_ARRIVAL_MIN_WINDOW_DAYS


@pytest.mark.asyncio
async def test_aresolve_client_int_precedence_and_clamp(monkeypatch):
    # The shared per-client int resolver behind the fetch-limit / results-count
    # configs: client_configs override > default, clamped, fail-open.
    pytest.importorskip("pydantic")
    import fashion_bot.config_manager as cfg_mod
    from fashion_bot.services.recommendation import recommendation_service as rs

    configured = {"value": None}

    async def fake_aget_config(key, default=None, client_id=None):
        return configured["value"]

    monkeypatch.setattr(cfg_mod, "aget_config", fake_aget_config)

    resolve = rs.aresolve_client_int

    # Config override is used (within bounds).
    configured["value"] = "30"
    assert await resolve("c", "k", 5, min_value=1, max_value=50) == 30
    # No config → default.
    configured["value"] = None
    assert await resolve("c", "k", 5, min_value=1, max_value=50) == 5
    # No client_id → default (never reads config).
    assert await resolve(None, "k", 5, min_value=1, max_value=50) == 5
    # Clamp high / low.
    configured["value"] = "9999"
    assert await resolve("c", "k", 5, min_value=1, max_value=50) == 50
    configured["value"] = "0"
    assert await resolve("c", "k", 5, min_value=1, max_value=50) == 1
    # Non-integer config → fail open to default.
    configured["value"] = "abc"
    assert await resolve("c", "k", 5, min_value=1, max_value=50) == 5

    # The product-discovery configs expose the documented default counts.
    assert rs.NEW_ARRIVAL_FETCH_LIMIT_DEFAULT == 100
    assert rs.BESTSELLER_FETCH_LIMIT_DEFAULT == 20
    assert rs.PRODUCT_RESULTS_COUNT_DEFAULT == 5


@pytest.mark.asyncio
async def test_understand_query_parses_recency_days(monkeypatch):
    """The QU LLM's `recency_days` is parsed/validated onto the result."""
    pytest.importorskip("pydantic")
    pytest.importorskip("langchain_core")
    import json
    import fashion_bot.services.recommendation.query_understanding as qu
    import fashion_bot.core.llm_factory as llm_mod

    async def fake_prompt(client_id, fields):
        return "SYSTEM PROMPT"

    monkeypatch.setattr(qu, "aget_qu_system_prompt", fake_prompt)

    canned = {"json": "{}"}

    class _Resp:
        def __init__(self, content):
            self.content = content

    class _LLM:
        async def ainvoke(self, messages):
            return _Resp(canned["json"])

    async def fake_aget_llm(tool_name=None, **kwargs):
        return _LLM()

    monkeypatch.setattr(llm_mod.LLMFactory, "aget_llm", staticmethod(fake_aget_llm))

    # Valid positive integer → carried through.
    canned["json"] = json.dumps(
        {"query": "products", "filter": "in_stock = true", "sort_by": "newest", "recency_days": 30}
    )
    res = await qu.understand_query("new products of last 1 month", client_id="c")
    assert res.sort_by == "newest"
    assert res.recency_days == 30

    # Invalid values (zero, negative, float, string, bool, missing) → None.
    for bad in [0, -5, 3.5, "30", True, None]:
        canned["json"] = json.dumps(
            {"query": "p", "filter": "in_stock = true", "sort_by": "newest", "recency_days": bad}
        )
        res = await qu.understand_query("new", client_id="c")
        assert res.recency_days is None, f"expected None for recency_days={bad!r}"

    # sort_by: the four valid values pass through; anything else → None (so the
    # pipeline applies no sort). "discount" is the field that lets the discount
    # keyword regex be retired.
    for good in ["newest", "price_low", "price_high", "discount"]:
        canned["json"] = json.dumps(
            {"query": "p", "filter": "in_stock = true", "sort_by": good}
        )
        res = await qu.understand_query("q", client_id="c")
        assert res.sort_by == good, f"expected sort_by={good!r}"

    for bad in ["cheapest", "lowest", "random", "", 123]:
        canned["json"] = json.dumps(
            {"query": "p", "filter": "in_stock = true", "sort_by": bad}
        )
        res = await qu.understand_query("q", client_id="c")
        assert res.sort_by is None, f"expected None for sort_by={bad!r}"


# ---------------------------------------------------------------------------
# Fakes for the Upstash Search service tests.
# ---------------------------------------------------------------------------
class _FakeDoc:
    """Mimics an upstash-search fetch result object (.id/.content/.metadata)."""

    def __init__(self, id, content=None, metadata=None):
        self.id = id
        self.content = content or {}
        self.metadata = metadata or {}


class _FakeIndex:
    """Records upserts and serves fetches; can be told to fail N upsert batches."""

    def __init__(self, docs_by_id=None, fail_batch_upserts=False):
        self._docs_by_id = docs_by_id or {}
        self.fetch_calls = []      # list of id-chunks requested
        self.upsert_calls = []     # list of doc-batches written
        self.range_calls = []      # list of {cursor, limit, prefix} scans
        self.fail_batch_upserts = fail_batch_upserts

    def fetch(self, ids=None):
        self.fetch_calls.append(list(ids or []))
        return [self._docs_by_id.get(i) for i in (ids or [])]

    def range(self, cursor="", limit=1, prefix=None):
        """Cursor-paginated id scan, as upstash-search's range() behaves.

        Returns an object with ``.documents`` and ``.next_cursor`` (empty string
        when the scan is exhausted), which is what ``_iter_doc_pages`` walks.
        """
        self.range_calls.append({"cursor": cursor, "limit": limit, "prefix": prefix})
        ids = sorted(
            i for i in self._docs_by_id if prefix is None or i.startswith(prefix)
        )
        start = int(cursor) if cursor else 0
        window = ids[start:start + limit]
        end = start + limit
        return SimpleNamespace(
            documents=[self._docs_by_id[i] for i in window],
            next_cursor=str(end) if end < len(ids) else "",
        )

    def upsert(self, documents=None):
        docs = list(documents or [])
        # Emulate Upstash rejecting a multi-doc batch but accepting singletons,
        # which is what triggers the service's per-doc retry path.
        if self.fail_batch_upserts and len(docs) > 1:
            raise RuntimeError("simulated batch upsert failure")
        self.upsert_calls.append(docs)
        return {"ok": True}


def _make_upstash_service(fake_index):
    """Build an UpstashSearchService without running __init__ (no env / summarizer)."""
    pytest.importorskip("pydantic")
    from fashion_bot.services.product_ingestion.upstash_search_service import (
        UpstashSearchService,
    )

    svc = object.__new__(UpstashSearchService)
    svc._client = None
    svc._url = "x"
    svc._token = "y"

    # Passthrough summarizer so aupsert_documents doesn't touch the LLM.
    class _PassthroughSummarizer:
        async def acompress_documents(self, documents):
            return documents

    svc._summarizer = _PassthroughSummarizer()
    svc._get_index = lambda client_id: fake_index
    return svc


# ---------------------------------------------------------------------------
# 4. UpstashSearchService.afetch_documents_by_ids — id-keyed reads (no scan).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_afetch_documents_by_ids_maps_and_batches():
    cid = "groovee"
    docs = {
        f"{cid}_1": _FakeDoc(f"{cid}_1", {"bestseller": True}, {"product_id": "1"}),
        f"{cid}_3": _FakeDoc(f"{cid}_3", {"bestseller": False}, {"product_id": "3"}),
    }
    fake = _FakeIndex(docs_by_id=docs)
    svc = _make_upstash_service(fake)

    # 150 ids @ batch_size 100 → two fetch chunks (100 + 50).
    product_ids = [str(n) for n in range(150)]
    out = await svc.afetch_documents_by_ids(cid, product_ids, batch_size=100)

    assert [len(c) for c in fake.fetch_calls] == [100, 50]
    # Composite ids are built as "{client_id}_{product_id}".
    assert fake.fetch_calls[0][0] == f"{cid}_0"
    # Only the present ids come back, mapped to id/content/metadata dicts; Nones dropped.
    by_id = {d["id"]: d for d in out}
    assert set(by_id) == {f"{cid}_1", f"{cid}_3"}
    assert by_id[f"{cid}_1"]["content"] == {"bestseller": True}
    assert by_id[f"{cid}_1"]["metadata"] == {"product_id": "1"}

    # Empty input short-circuits with no index hit.
    fake2 = _FakeIndex()
    svc2 = _make_upstash_service(fake2)
    assert await svc2.afetch_documents_by_ids(cid, []) == []
    assert fake2.fetch_calls == []


@pytest.mark.asyncio
async def test_afetch_documents_by_ids_raises_instead_of_returning_short():
    # A transport error must propagate: a short list would read as "these ids
    # are not indexed", turning an outage into a silent data gap.
    class _BoomIndex(_FakeIndex):
        def fetch(self, ids=None):
            raise RuntimeError("upstash unreachable")

    svc = _make_upstash_service(_BoomIndex())
    with pytest.raises(RuntimeError, match="upstash unreachable"):
        await svc.afetch_documents_by_ids("groovee", ["1", "2"])


@pytest.mark.asyncio
async def test_fetch_documents_by_content_flag_scans_every_page():
    # The flagged set must be enumerated by cursor scan across ALL pages, not by
    # a relevance search (which caps at one page of topK results).
    cid = "groovee"
    docs = {}
    for n in range(250):
        doc_id = f"{cid}_{n:03d}"
        docs[doc_id] = _FakeDoc(
            doc_id,
            {"bestseller": n % 50 == 0, "title": f"p{n}"},   # 5 flagged: 0,50,100,150,200
            {"product_id": f"{n:03d}"},
        )
    # A doc belonging to a different tenant must never be scanned.
    docs["other_999"] = _FakeDoc("other_999", {"bestseller": True}, {"product_id": "999"})

    svc = _make_upstash_service(_FakeIndex(docs_by_id=docs))
    out = await svc.afetch_documents_by_content_flag(cid, "bestseller")

    assert {d["metadata"]["product_id"] for d in out} == {"000", "050", "100", "150", "200"}
    assert all(d["content"]["bestseller"] is True for d in out)


@pytest.mark.asyncio
async def test_fetch_documents_by_content_flag_raises_on_scan_failure():
    class _BoomIndex(_FakeIndex):
        def range(self, cursor="", limit=1, prefix=None):
            raise RuntimeError("upstash unreachable")

    svc = _make_upstash_service(_BoomIndex())
    with pytest.raises(RuntimeError, match="upstash unreachable"):
        await svc.afetch_documents_by_content_flag("groovee", "bestseller")


# ---------------------------------------------------------------------------
# 5. UpstashSearchService.aupsert_documents — batched writes + per-doc retry.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_aupsert_documents_batches_and_counts():
    fake = _FakeIndex()
    svc = _make_upstash_service(fake)

    documents = [{"id": str(n), "content": {}, "metadata": {}} for n in range(120)]
    result = await svc.aupsert_documents(documents, client_id="c", batch_size=50)

    # 120 docs @ 50 → batches of 50, 50, 20.
    assert [len(b) for b in fake.upsert_calls] == [50, 50, 20]
    assert result["success_count"] == 120
    assert result["failed"] == []


@pytest.mark.asyncio
async def test_aupsert_documents_falls_back_to_per_doc_on_batch_failure():
    # Index rejects multi-doc batches → service retries each doc individually.
    fake = _FakeIndex(fail_batch_upserts=True)
    svc = _make_upstash_service(fake)

    documents = [{"id": str(n), "content": {}, "metadata": {}} for n in range(3)]
    result = await svc.aupsert_documents(documents, client_id="c", batch_size=50)

    # All three still land (one per upsert call), and none are reported failed.
    assert result["success_count"] == 3
    assert result["failed"] == []
    assert all(len(b) == 1 for b in fake.upsert_calls)


# ---------------------------------------------------------------------------
# 6. Bestseller reconciliation — ADD new sellers, REMOVE stale flags, SKIP the
#    intersection; an incomplete Shopify sweep applies adds only (never demotes).
# ---------------------------------------------------------------------------
class _FakeSalesResult:
    def __init__(self, sales, complete=True, truncated_reason=None, pages_fetched=1):
        self.sales = sales
        self.complete = complete
        self.truncated_reason = truncated_reason
        self.pages_fetched = pages_fetched


def _bestseller_doc(cid, pid, flagged):
    return {
        "id": f"{cid}_{pid}",
        "content": {"bestseller": flagged, "title": f"p{pid}"},
        "metadata": {"product_id": pid},
    }


async def _run_reconcile(
    monkeypatch, *, sales, complete, current_flagged_ids, index_ids,
    flag_scan_error=None, id_fetch_error=None,
):
    """Drive _refresh_client_bestsellers with fakes; return (result, upserted_docs).

    ``flag_scan_error`` / ``id_fetch_error`` make the corresponding index read
    raise, so tests can assert an unreadable index is never mistaken for an
    empty one.
    """
    pytest.importorskip("pydantic")
    import fashion_bot.cron_jobs.bestseller_refresh_job as job
    from fashion_bot.services.product_ingestion import (
        product_source_factory as psf_mod,
        shopify_product_service as sps_mod,
        upstash_search_service as uss_mod,
    )

    cid = "groovee"

    # --- Fake Shopify product service (instance of the real class for isinstance). ---
    svc = object.__new__(sps_mod.ShopifyProductService)

    async def _afetch_sales_volume(*a, **k):
        return _FakeSalesResult(
            sales=sales, complete=complete,
            truncated_reason=None if complete else "max_pages",
        )

    svc.afetch_sales_volume = _afetch_sales_volume

    class _FakeFactory:
        async def aget_service(self, client_id, source=None):
            return svc

    monkeypatch.setattr(psf_mod, "ProductSourceFactory", _FakeFactory)

    # --- Fake Upstash search service. ---
    captured = {"upserts": None}

    class _FakeSearch:
        async def afetch_documents_by_content_flag(self, client_id, field):
            if flag_scan_error is not None:
                raise flag_scan_error
            return [_bestseller_doc(cid, pid, True) for pid in current_flagged_ids]

        async def afetch_documents_by_ids(self, client_id, ids):
            if id_fetch_error is not None:
                raise id_fetch_error
            return [
                _bestseller_doc(cid, pid, False)
                for pid in ids
                if pid in index_ids
            ]

        async def aupsert_documents(self, docs, client_id=None):
            captured["upserts"] = docs
            return {"success_count": len(docs), "failed": []}

    monkeypatch.setattr(uss_mod, "get_upstash_search_service", lambda: _FakeSearch())

    result = await job._refresh_client_bestsellers(cid)
    return result, captured["upserts"]


@pytest.mark.asyncio
async def test_reconcile_add_remove_skip(monkeypatch):
    # Shopify top sellers: {1, 2, 3}. Currently flagged: {2, 3, 4}.
    # → ADD 1, SKIP {2,3}, REMOVE 4. Product 1 exists in the index.
    result, upserts = await _run_reconcile(
        monkeypatch,
        sales={"gid://shopify/Product/1": 50, "gid://shopify/Product/2": 30, "gid://shopify/Product/3": 10},
        complete=True,
        current_flagged_ids=["2", "3", "4"],
        index_ids={"1", "2", "3", "4"},
    )

    assert result["success"] is True
    assert result["added"] == 1
    assert result["removed"] == 1
    assert result["unchanged"] == 2

    # The single batched write sets bestseller=true on the add and false on the remove.
    flags = {d["metadata"]["product_id"]: d["content"]["bestseller"] for d in upserts}
    assert flags == {"1": True, "4": False}


@pytest.mark.asyncio
async def test_reconcile_incomplete_sweep_adds_only(monkeypatch):
    # Same sets, but the Shopify sweep was truncated → never demote (no removals),
    # only the safe-to-add product is written.
    result, upserts = await _run_reconcile(
        monkeypatch,
        sales={"gid://shopify/Product/1": 50, "gid://shopify/Product/2": 30, "gid://shopify/Product/3": 10},
        complete=False,
        current_flagged_ids=["2", "3", "4"],
        index_ids={"1", "2", "3", "4"},
    )

    assert result["added"] == 1
    assert result["removed"] == 0
    assert result["removals_skipped"] == 1
    flags = {d["metadata"]["product_id"]: d["content"]["bestseller"] for d in upserts}
    assert flags == {"1": True}


@pytest.mark.asyncio
async def test_reconcile_add_id_not_yet_ingested_is_skipped(monkeypatch):
    # New bestseller id 9 isn't in the index yet (not ingested) → counted as
    # `missing` and NOT written, while the ingested add (1) still lands.
    result, upserts = await _run_reconcile(
        monkeypatch,
        sales={"gid://shopify/Product/1": 50, "gid://shopify/Product/9": 40},
        complete=True,
        current_flagged_ids=["1"],          # 1 already flagged → SKIP
        index_ids={"1"},                     # 9 absent from the index
    )

    # 1 is already flagged (skip), 9 is missing from the index → nothing to write.
    assert result["added"] == 0
    assert result["missing"] == 1
    assert result["unchanged"] == 1
    assert upserts is None  # no upsert call when there's nothing to change


@pytest.mark.asyncio
async def test_reconcile_zero_id_overlap_refuses_to_clear_every_flag(monkeypatch):
    # Shopify top sellers {7,8} share NOTHING with the flagged set {1,2}. That is
    # a structural mismatch (wrong id space / wrong store), not a real turnover,
    # so the removals must be blocked rather than clearing every flag. Adds are
    # still safe and must still land.
    result, upserts = await _run_reconcile(
        monkeypatch,
        sales={"gid://shopify/Product/7": 50, "gid://shopify/Product/8": 40},
        complete=True,
        current_flagged_ids=["1", "2"],
        index_ids={"1", "2", "7", "8"},
    )

    assert result["unchanged"] == 0
    assert result["removal_guard"] is True
    assert result["removed"] == 0            # 1 and 2 keep their flags
    assert result["removals_skipped"] == 2
    assert result["added"] == 2              # 7 and 8 still promoted

    # Nothing written with bestseller=False — no product was demoted.
    written = {d["id"]: d["content"]["bestseller"] for d in upserts}
    assert written == {"groovee_7": True, "groovee_8": True}


@pytest.mark.asyncio
async def test_reconcile_partial_overlap_still_removes(monkeypatch):
    # One shared id is enough to prove the id spaces line up, so genuine
    # turnover must still demote — the guard above must not over-trigger.
    result, upserts = await _run_reconcile(
        monkeypatch,
        sales={"gid://shopify/Product/1": 50, "gid://shopify/Product/7": 40},
        complete=True,
        current_flagged_ids=["1", "2"],
        index_ids={"1", "2", "7"},
    )

    assert result["unchanged"] == 1
    assert result.get("removal_guard") is False
    assert result["removed"] == 1            # 2 is no longer a bestseller


@pytest.mark.asyncio
async def test_reconcile_flag_scan_failure_is_not_reported_as_no_bestsellers(monkeypatch):
    # An unreadable index must abort the client, not silently look like
    # "nothing is flagged" (which would re-add the whole top-N every run).
    result, upserts = await _run_reconcile(
        monkeypatch,
        sales={"gid://shopify/Product/1": 50},
        complete=True,
        current_flagged_ids=["1"],
        index_ids={"1"},
        flag_scan_error=RuntimeError("upstash unreachable"),
    )

    assert result["skipped"] is True
    assert result["reason"] == "index_read_error"
    assert upserts is None


@pytest.mark.asyncio
async def test_reconcile_id_fetch_failure_is_not_reported_as_missing(monkeypatch):
    # Same for the ADD-path lookup: a transport error must not be recorded as
    # "these products are not in the index", and must not apply the pending
    # removals on its way out.
    result, upserts = await _run_reconcile(
        monkeypatch,
        sales={"gid://shopify/Product/1": 50, "gid://shopify/Product/2": 40},
        complete=True,
        current_flagged_ids=["2", "3"],
        index_ids={"1", "2", "3"},
        id_fetch_error=RuntimeError("upstash unreachable"),
    )

    assert result["skipped"] is True
    assert result["reason"] == "index_read_error"
    assert result.get("missing") is None     # not misreported as a data gap
    assert upserts is None                   # removal of 3 was NOT applied


@pytest.mark.asyncio
async def test_reconcile_no_sales_skips_without_demoting(monkeypatch):
    # An empty Shopify sales window must never wipe existing bestseller flags.
    result, upserts = await _run_reconcile(
        monkeypatch,
        sales={},
        complete=True,
        current_flagged_ids=["2", "3"],
        index_ids={"2", "3"},
    )

    assert result.get("skipped") is True
    assert result.get("reason") == "no_sales"
    assert upserts is None  # no write at all


async def _run_capture_lookback(monkeypatch, *, config_value):
    """Drive _refresh_client_bestsellers, stub aget_config, capture the `days`
    kwarg passed to afetch_sales_volume. Returns the captured days value."""
    pytest.importorskip("pydantic")
    import fashion_bot.cron_jobs.bestseller_refresh_job as job
    from fashion_bot import config_manager as cfg_mod
    from fashion_bot.services.product_ingestion import (
        product_source_factory as psf_mod,
        shopify_product_service as sps_mod,
        upstash_search_service as uss_mod,
    )

    cid = "groovee"
    captured = {"days": None}

    svc = object.__new__(sps_mod.ShopifyProductService)

    async def _afetch_sales_volume(*a, **k):
        captured["days"] = k.get("days")
        return _FakeSalesResult(
            sales={"gid://shopify/Product/1": 10}, complete=True, truncated_reason=None
        )

    svc.afetch_sales_volume = _afetch_sales_volume

    class _FakeFactory:
        async def aget_service(self, client_id, source=None):
            return svc

    monkeypatch.setattr(psf_mod, "ProductSourceFactory", _FakeFactory)

    class _FakeSearch:
        async def afetch_documents_by_content_flag(self, client_id, field):
            return []

        async def afetch_documents_by_ids(self, client_id, ids):
            return []

        async def aupsert_documents(self, docs, client_id=None):
            return {"success_count": len(docs), "failed": []}

    monkeypatch.setattr(uss_mod, "get_upstash_search_service", lambda: _FakeSearch())

    # Stub config lookup: return the per-client override only for the lookback key.
    async def _fake_aget_config(config_key, default=None, client_id=None):
        if config_key == "bestseller_lookback_days":
            return config_value
        return None

    monkeypatch.setattr(cfg_mod, "aget_config", _fake_aget_config)

    await job._refresh_client_bestsellers(cid)
    return captured["days"]


@pytest.mark.asyncio
async def test_lookback_per_client_override_drives_fetch(monkeypatch):
    # A per-client bestseller_lookback_days config overrides the env default and
    # is passed to afetch_sales_volume.
    days = await _run_capture_lookback(monkeypatch, config_value="30")
    assert days == 30


@pytest.mark.asyncio
async def test_lookback_default_when_no_client_override(monkeypatch):
    # No per-client config → fall back to the module-level env default.
    import fashion_bot.cron_jobs.bestseller_refresh_job as job
    days = await _run_capture_lookback(monkeypatch, config_value=None)
    assert days == job.BESTSELLER_LOOKBACK_DAYS


# ---------------------------------------------------------------------------
# 6b. afetch_sales_volume — ShopifyQL path: one query, returns-adjusted,
#     GID-keyed, always complete; with env-flag + parse-error fallback to the
#     legacy Orders sweep.
# ---------------------------------------------------------------------------
def _make_shopify_service():
    """A ShopifyProductService with just the attrs the sales fetch needs (no __init__)."""
    pytest.importorskip("pydantic")
    from fashion_bot.services.product_ingestion import shopify_product_service as sps_mod

    svc = object.__new__(sps_mod.ShopifyProductService)
    svc.access_token = "shpat_test"
    svc.graphql_url = "https://x.myshopify.com/admin/api/2025-10/graphql.json"
    svc._rate_limit_key = "x.myshopify.com"
    return svc


def _shopifyql_response(rows):
    """A shopifyqlQuery GraphQL envelope with product_id / net_items_sold columns."""
    return {
        "data": {
            "shopifyqlQuery": {
                "parseErrors": [],
                "tableData": {
                    "columns": [{"name": "product_id"}, {"name": "net_items_sold"}],
                    "rows": rows,
                },
            }
        }
    }


@pytest.mark.asyncio
async def test_sales_volume_shopifyql_builds_gid_keyed_map(monkeypatch):
    from fashion_bot.services.product_ingestion import shopify_product_service as sps_mod

    captured = {}

    async def fake_post(client, url, headers, payload, rate_limit_key=None):
        captured["payload"] = payload
        return _shopifyql_response([
            {"product_id": "9934369816898", "net_items_sold": "98"},
            {"product_id": "10069521695042", "net_items_sold": "95"},
        ])

    monkeypatch.setenv("BESTSELLER_USE_SHOPIFYQL", "true")
    monkeypatch.setattr(sps_mod, "shopify_graphql_post", fake_post)

    svc = _make_shopify_service()
    result = await svc.afetch_sales_volume(days=90, max_pages=200)

    # Single ShopifyQL call → GID-keyed map, marked complete (no truncation).
    assert result.complete is True
    assert result.pages_fetched == 1
    assert result.sales == {
        "gid://shopify/Product/9934369816898": 98,
        "gid://shopify/Product/10069521695042": 95,
    }
    # Query targeted the 90-day window and grouped by product_id (not title).
    ql = captured["payload"]["variables"]["q"]
    assert "SINCE -90d" in ql and "GROUP BY product_id" in ql


@pytest.mark.asyncio
async def test_sales_volume_shopifyql_skips_nonpositive_and_blank(monkeypatch):
    from fashion_bot.services.product_ingestion import shopify_product_service as sps_mod

    async def fake_post(client, url, headers, payload, rate_limit_key=None):
        # net ≤ 0 (returns ≥ sales) and a blank id must all be dropped.
        return _shopifyql_response([
            {"product_id": "111", "net_items_sold": "5"},
            {"product_id": "222", "net_items_sold": "0"},
            {"product_id": "333", "net_items_sold": "-3"},
            {"product_id": "", "net_items_sold": "9"},
        ])

    monkeypatch.setattr(sps_mod, "shopify_graphql_post", fake_post)

    svc = _make_shopify_service()
    result = await svc.afetch_sales_volume(days=30)
    assert result.sales == {"gid://shopify/Product/111": 5}


@pytest.mark.asyncio
async def test_sales_volume_shopifyql_parse_error_falls_back_to_orders(monkeypatch):
    from fashion_bot.services.product_ingestion import shopify_product_service as sps_mod

    async def fake_post(client, url, headers, payload, rate_limit_key=None):
        return {"data": {"shopifyqlQuery": {"parseErrors": ["bad query"], "tableData": None}}}

    monkeypatch.setattr(sps_mod, "shopify_graphql_post", fake_post)

    called = {"orders": False}

    async def fake_orders(self, days=10, max_pages=20):
        called["orders"] = True
        return sps_mod.SalesVolumeResult(
            sales={"gid://shopify/Product/7": 3}, complete=True, pages_fetched=2
        )

    monkeypatch.setattr(
        sps_mod.ShopifyProductService, "_afetch_sales_volume_via_orders", fake_orders
    )

    svc = _make_shopify_service()
    result = await svc.afetch_sales_volume(days=30, max_pages=5)
    assert called["orders"] is True  # parse error → degrade to the Orders sweep
    assert result.sales == {"gid://shopify/Product/7": 3}


@pytest.mark.asyncio
async def test_sales_volume_flag_disables_shopifyql(monkeypatch):
    from fashion_bot.services.product_ingestion import shopify_product_service as sps_mod

    async def fake_post(*a, **k):
        raise AssertionError("ShopifyQL must not be called when the flag is off")

    monkeypatch.setattr(sps_mod, "shopify_graphql_post", fake_post)
    monkeypatch.setenv("BESTSELLER_USE_SHOPIFYQL", "false")

    async def fake_orders(self, days=10, max_pages=20):
        return sps_mod.SalesVolumeResult(
            sales={"gid://shopify/Product/9": 1}, complete=True, pages_fetched=1
        )

    monkeypatch.setattr(
        sps_mod.ShopifyProductService, "_afetch_sales_volume_via_orders", fake_orders
    )

    svc = _make_shopify_service()
    result = await svc.afetch_sales_volume(days=30)
    assert result.sales == {"gid://shopify/Product/9": 1}


# ---------------------------------------------------------------------------
# 6c. afetch_product_engagement — ShopifyQL `sessions` dataset → GID-keyed
#     unique-sessions map (dict rows, per Shopify's documented tableData format).
# ---------------------------------------------------------------------------
def _sessions_response(rows):
    """A shopifyqlQuery envelope for the sessions dataset (product_id/sessions)."""
    return {
        "data": {
            "shopifyqlQuery": {
                "parseErrors": [],
                "tableData": {
                    "columns": [{"name": "product_id"}, {"name": "sessions"}],
                    "rows": rows,
                },
            }
        }
    }


@pytest.mark.asyncio
async def test_product_engagement_builds_gid_keyed_sessions(monkeypatch):
    from fashion_bot.services.product_ingestion import shopify_product_service as sps_mod

    captured = {}

    async def fake_post(client, url, headers, payload, rate_limit_key=None):
        captured["payload"] = payload
        # 0-session and blank-id rows must be dropped; ids normalised to GID.
        return _sessions_response([
            {"product_id": "111", "sessions": "500"},
            {"product_id": "222", "sessions": "0"},
            {"product_id": "", "sessions": "9"},
        ])

    monkeypatch.setattr(sps_mod, "shopify_graphql_post", fake_post)

    svc = _make_shopify_service()
    result = await svc.afetch_product_engagement(days=90)

    assert result.grouped_by == "product_id"
    assert result.complete is True
    assert result.sessions == {"gid://shopify/Product/111": 500}
    ql = captured["payload"]["variables"]["q"]
    assert "FROM sessions" in ql and "GROUP BY product_id" in ql and "SINCE -90d" in ql


@pytest.mark.asyncio
async def test_product_engagement_parse_error_raises(monkeypatch):
    from fashion_bot.services.product_ingestion import shopify_product_service as sps_mod

    async def fake_post(client, url, headers, payload, rate_limit_key=None):
        return {"data": {"shopifyqlQuery": {"parseErrors": ["bad"], "tableData": None}}}

    monkeypatch.setattr(sps_mod, "shopify_graphql_post", fake_post)

    svc = _make_shopify_service()
    with pytest.raises(RuntimeError):
        await svc.afetch_product_engagement(days=30)


# ---------------------------------------------------------------------------
# 6d. Engagement reconcile inside _refresh_client_bestsellers — conversion_rate
#     is computed and written, and overlaps with the bestseller write preserve
#     the bestseller flag (single write per doc, no clobber).
# ---------------------------------------------------------------------------
async def _run_reconcile_with_sessions(
    monkeypatch, *, sales, sessions, current_flagged_ids, index_ids
):
    """Drive _refresh_client_bestsellers with sales + sessions fakes; return
    {pid: merged upserted doc} across ALL upsert calls (bestseller + engagement)."""
    pytest.importorskip("pydantic")
    import fashion_bot.cron_jobs.bestseller_refresh_job as job
    from fashion_bot.services.product_ingestion import (
        product_source_factory as psf_mod,
        shopify_product_service as sps_mod,
        upstash_search_service as uss_mod,
    )

    cid = "groovee"
    svc = object.__new__(sps_mod.ShopifyProductService)

    async def _afetch_sales_volume(*a, **k):
        return _FakeSalesResult(sales=sales, complete=True, truncated_reason=None)

    async def _afetch_product_engagement(*a, **k):
        return sps_mod.EngagementResult(sessions=sessions, grouped_by="product_id")

    svc.afetch_sales_volume = _afetch_sales_volume
    svc.afetch_product_engagement = _afetch_product_engagement

    class _FakeFactory:
        async def aget_service(self, client_id, source=None):
            return svc

    monkeypatch.setattr(psf_mod, "ProductSourceFactory", _FakeFactory)

    upserts_by_pid = {}

    class _FakeSearch:
        async def afetch_documents_by_content_flag(self, client_id, field):
            return [_bestseller_doc(cid, pid, True) for pid in current_flagged_ids]

        async def afetch_documents_by_ids(self, client_id, ids):
            # Model the index faithfully: a product's stored bestseller flag is
            # True iff it is currently flagged.
            return [
                _bestseller_doc(cid, pid, pid in current_flagged_ids)
                for pid in ids
                if pid in index_ids
            ]

        async def aupsert_documents(self, docs, client_id=None):
            for d in docs:
                upserts_by_pid[d["metadata"]["product_id"]] = d
            return {"success_count": len(docs), "failed": []}

    monkeypatch.setattr(uss_mod, "get_upstash_search_service", lambda: _FakeSearch())

    result = await job._refresh_client_bestsellers(cid)
    return result, upserts_by_pid


@pytest.mark.asyncio
async def test_engagement_writes_conversion_and_preserves_bestseller(monkeypatch):
    # sales: 1→50, 2→30 (units). Currently flagged: {1}. Sessions: 1→500, 2→300,
    # 3→100 (3 has traffic but no sales). Bestseller reconcile: ADD 2, unchanged 1.
    result, upserts = await _run_reconcile_with_sessions(
        monkeypatch,
        sales={"gid://shopify/Product/1": 50, "gid://shopify/Product/2": 30},
        sessions={
            "gid://shopify/Product/1": 500,
            "gid://shopify/Product/2": 300,
            "gid://shopify/Product/3": 100,
        },
        current_flagged_ids=["1"],
        index_ids={"1", "2", "3"},
    )

    # conversion_rate = units_sold / unique_sessions.
    assert upserts["1"]["content"]["conversion_rate"] == 0.1   # 50/500
    assert upserts["1"]["content"]["unique_sessions"] == 500
    assert upserts["1"]["content"]["units_sold"] == 50
    assert upserts["2"]["content"]["conversion_rate"] == 0.1   # 30/300
    # Product 3: traffic but no sales → conversion 0, units 0.
    assert upserts["3"]["content"]["conversion_rate"] == 0.0
    assert upserts["3"]["content"]["units_sold"] == 0

    # Overlap safety: product 2 (a bestseller ADD) keeps bestseller=True AND gets
    # analytics; product 1 (unchanged bestseller) keeps its True flag through the
    # engagement write; product 3 stays non-bestseller.
    assert upserts["2"]["content"]["bestseller"] is True
    assert upserts["1"]["content"]["bestseller"] is True
    assert upserts["3"]["content"]["bestseller"] is False
    assert result["engagement"]["products_with_sessions"] == 3


@pytest.mark.asyncio
async def test_engagement_skipped_when_no_sessions(monkeypatch):
    # Empty sessions → no analytics written; bestseller reconcile still runs.
    result, upserts = await _run_reconcile_with_sessions(
        monkeypatch,
        sales={"gid://shopify/Product/1": 50},
        sessions={},
        current_flagged_ids=[],
        index_ids={"1"},
    )
    # Product 1 is a bestseller ADD (flagged), but carries NO analytics fields.
    assert "conversion_rate" not in upserts["1"]["content"]
    assert result["engagement"].get("reason") == "no_sessions"


# ---------------------------------------------------------------------------
# 6e. business_rules_reranker — sort_by_conversion ranks by conversion_rate.
# ---------------------------------------------------------------------------
def test_rerank_sort_by_conversion_orders_by_rate():
    from fashion_bot.services.recommendation.business_rules_reranker import rerank

    results = [
        {"content": {"title": "low", "conversion_rate": 0.01, "variant_availability_pct": 100}, "score": 0.9},
        {"content": {"title": "high", "conversion_rate": 0.20, "variant_availability_pct": 100}, "score": 0.1},
        {"content": {"title": "none", "variant_availability_pct": 100}, "score": 0.95},  # no rate → last
    ]
    out = rerank(results, max_results=10, sort_by_conversion=True)
    assert [r["content"]["title"] for r in out] == ["high", "low", "none"]


# ===========================================================================
# Integration tests — live Groovee store (real Shopify + Upstash)
#
# Same constants and fixture pattern as test_product_details_tools.py /
# test_place_order_tools.py.  These hit real APIs so they're slower.
# ===========================================================================

CLIENT_ID = "c3ffcb1b-afb9-4ca4-8746-a06698bec870"
VALID_PRODUCT_HANDLE = "evolve-the-cosmic-shacket"


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_env():
    from fashion_bot.env_loader import bootstrap_environment
    bootstrap_environment()


# ---------------------------------------------------------------------------
# 7. Shopify sales-volume fetch — live API
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_shopify_sales_volume_returns_structured_result():
    """afetch_sales_volume returns a SalesVolumeResult with real GIDs and counts."""
    pytest.importorskip("pydantic")
    from fashion_bot.services.product_ingestion.product_source_factory import (
        ProductSourceFactory,
    )
    from fashion_bot.services.product_ingestion.shopify_product_service import (
        ShopifyProductService,
    )

    product_service = await ProductSourceFactory().aget_service(CLIENT_ID, source="shopify")
    assert isinstance(product_service, ShopifyProductService), (
        "Groovee client must resolve to a ShopifyProductService"
    )

    result = await product_service.afetch_sales_volume(days=30, max_pages=3)

    assert hasattr(result, "sales"), "Result must have a 'sales' attribute"
    assert hasattr(result, "complete"), "Result must have a 'complete' attribute"
    assert hasattr(result, "pages_fetched"), "Result must have 'pages_fetched'"
    assert isinstance(result.sales, dict)
    assert result.pages_fetched >= 1

    if result.sales:
        first_gid = next(iter(result.sales))
        assert "gid://shopify/Product/" in first_gid, (
            f"Sales keys must be Shopify GIDs, got: {first_gid}"
        )
        first_count = result.sales[first_gid]
        assert isinstance(first_count, (int, float)) and first_count > 0


# ---------------------------------------------------------------------------
# 8. Upstash Search — live reads for the Groovee client
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_upstash_search_bestseller_filter_returns_docs():
    """Searching Upstash with bestseller=true returns real flagged products."""
    pytest.importorskip("pydantic")
    from fashion_bot.services.product_ingestion.upstash_search_service import (
        get_upstash_search_service,
    )

    svc = get_upstash_search_service()
    docs = await svc.asearch(
        query="bestseller",
        client_id=CLIENT_ID,
        filter_str="bestseller = true",
        limit=10,
        semantic_weight=0.1,
        reranking=False,
    )

    assert isinstance(docs, list)
    if docs:
        doc = docs[0]
        assert "content" in doc, "Upstash doc must have 'content'"
        assert "metadata" in doc, "Upstash doc must have 'metadata'"
        assert doc["content"].get("bestseller") is True, (
            "Filtered doc must have bestseller=true in content"
        )
        assert doc["metadata"].get("product_id"), (
            "Upstash doc must have product_id in metadata"
        )


@pytest.mark.asyncio
async def test_upstash_fetch_by_ids_returns_known_product():
    """afetch_documents_by_ids can retrieve a known product (Cosmic Shacket)."""
    pytest.importorskip("pydantic")
    from fashion_bot.services.product_ingestion.upstash_search_service import (
        get_upstash_search_service,
    )

    svc = get_upstash_search_service()
    docs = await svc.asearch(
        query="cosmic shacket",
        client_id=CLIENT_ID,
        filter_str="in_stock = true",
        limit=3,
    )
    if not docs:
        pytest.skip("Cosmic Shacket not found in Upstash — catalog may have drifted")

    target = next(
        (d for d in docs if d.get("content", {}).get("handle") == VALID_PRODUCT_HANDLE),
        None,
    )
    if not target:
        pytest.skip("Cosmic Shacket handle not in search results")

    pid = target["metadata"].get("product_id")
    assert pid, "Product must have a product_id in metadata"

    fetched = await svc.afetch_documents_by_ids(CLIENT_ID, [str(pid)])
    assert len(fetched) == 1
    assert fetched[0]["metadata"]["product_id"] == str(pid)
    assert fetched[0]["content"].get("handle") == VALID_PRODUCT_HANDLE


# ---------------------------------------------------------------------------
# 9. Full search pipeline — live end-to-end
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_search_pipeline_bestseller_returns_products():
    """The real search_products_pipeline returns products for a bestseller query."""
    pytest.importorskip("pydantic")
    from fashion_bot.services.recommendation.recommendation_service import (
        search_products_pipeline,
        SearchPipelineResult,
    )

    result = await search_products_pipeline(
        query="show me your best sellers",
        client_id=CLIENT_ID,
    )

    assert isinstance(result, SearchPipelineResult)
    assert len(result.products) > 0, "Bestseller query should return products"
    assert isinstance(result.qu_query, str) and len(result.qu_query) > 0
    assert result.is_bestseller is True, (
        "QU should classify 'best sellers' as is_bestseller=True"
    )


@pytest.mark.asyncio
async def test_search_pipeline_new_arrivals():
    """The real pipeline handles a 'new arrivals' / sort-by-newest query.

    The test store may have no recently-created products, so we validate
    structure rather than requiring non-empty results."""
    pytest.importorskip("pydantic")
    from fashion_bot.services.recommendation.recommendation_service import (
        search_products_pipeline,
        SearchPipelineResult,
    )

    result = await search_products_pipeline(
        query="show me new arrivals",
        client_id=CLIENT_ID,
    )

    assert isinstance(result, SearchPipelineResult)
    assert isinstance(result.products, list)
    assert isinstance(result.qu_query, str) and len(result.qu_query) > 0
    assert "in_stock" in result.qu_filter


def _extract_handle(product: dict) -> str:
    """Extract the handle from a pipeline product dict.

    The pipeline returns Upstash docs in ``{"content": {...}, "metadata": {...}}``
    form. ``content`` may be a dict or a string depending on the SDK version,
    so we try multiple locations."""
    c = product.get("content")
    if isinstance(c, dict) and c.get("handle"):
        return c["handle"]
    m = product.get("metadata")
    if isinstance(m, dict) and m.get("handle"):
        return m["handle"]
    return product.get("handle", "")


@pytest.mark.asyncio
async def test_search_pipeline_known_product():
    """Searching for the Cosmic Shacket through the full pipeline finds it."""
    pytest.importorskip("pydantic")
    from fashion_bot.services.recommendation.recommendation_service import (
        search_products_pipeline,
    )

    result = await search_products_pipeline(
        query="cosmic shacket",
        client_id=CLIENT_ID,
    )

    assert len(result.products) > 0
    handles = [_extract_handle(p) for p in result.products]
    assert VALID_PRODUCT_HANDLE in handles, (
        f"Expected '{VALID_PRODUCT_HANDLE}' in pipeline results: {handles}"
    )
