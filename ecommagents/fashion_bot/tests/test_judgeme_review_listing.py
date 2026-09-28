"""Offline tests for judgeme/tools/review_listing.py -- the deterministic
review filter/sort logic behind the get_product_reviews tool. No live
Judge.me/DB calls; all mocked. Sort/filter behaviour mirrors what was
verified live against a real Judge.me shop (see review_listing.py docstring).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from fashion_bot.judgeme.tools import review_listing as rl
from fashion_bot.utils.tiered_cache import clear_tiered_cache

_MODULE = "fashion_bot.judgeme.tools.review_listing"


@pytest.fixture(autouse=True)
def _isolated_review_cache():
    """review_listing memoises its Judge.me fetch through the shared tiered
    cache, whose memory tier is process-global -- without this, every test
    below would hit the entry left by whichever test ran first (they all use
    the same client_id/external_id) and assert against a stale fetch instead
    of its own mock. Redis is forced unavailable so the tier degrades to
    memory-only and these stay offline tests."""
    clear_tiered_cache()
    with patch(
        "fashion_bot.utils.redis_client.get_shared_async_redis_client",
        AsyncMock(return_value=None),
    ):
        yield
    clear_tiered_cache()


_REVIEWS = [
    {"rating": 5, "body": "Loved it, perfect fit", "created_at": "2026-04-06T03:55:29+00:00",
     "published": True, "reviewer": {"name": "Alice", "email": "a@x.com"}, "verified": True},
    {"rating": 4, "body": "Comfortable, good quality", "created_at": "2026-08-23T09:35:49+00:00",
     "published": True, "reviewer": {"name": "Bob", "email": "b@x.com"}, "verified": False},
    {"rating": 2, "body": "Sizing ran small", "created_at": "2026-06-01T00:00:00+00:00",
     "published": True, "reviewer": {"name": "Cara", "email": "c@x.com"}, "verified": True},
]


# ── pure filter/sort logic ────────────────────────────────────────────────

def test_rating_as_int_handles_string_rating():
    """Judge.me's rating is documented/verified as an int, but a string
    value (or anything else unexpected) must degrade to 0 rather than raise
    -- a TypeError here would take down the whole request over one bad
    review instead of just treating that review as unrated."""
    assert rl._rating_as_int({"rating": "5"}) == 5
    assert rl._rating_as_int({"rating": "not-a-number"}) == 0
    assert rl._rating_as_int({"rating": None}) == 0
    assert rl._rating_as_int({}) == 0


def test_filter_and_sort_tolerates_string_rating_without_raising():
    reviews = [{"rating": "5", "created_at": "2026-01-01T00:00:00+00:00"},
               {"rating": 3, "created_at": "2026-01-02T00:00:00+00:00"}]
    result = rl._filter_and_sort(reviews, sort="top_rated", sentiment=None)
    assert [r["rating"] for r in result] == ["5", 3]  # sorted 5 before 3, original values preserved


def test_filter_and_sort_top_rated_default():
    result = rl._filter_and_sort(_REVIEWS, sort="top_rated", sentiment=None)
    assert [r["rating"] for r in result] == [5, 4, 2]


def test_filter_and_sort_recent():
    result = rl._filter_and_sort(_REVIEWS, sort="recent", sentiment=None)
    assert [r["created_at"] for r in result] == [
        "2026-08-23T09:35:49+00:00", "2026-06-01T00:00:00+00:00", "2026-04-06T03:55:29+00:00",
    ]


def test_filter_and_sort_positive_sentiment():
    result = rl._filter_and_sort(_REVIEWS, sort="top_rated", sentiment="positive")
    assert [r["rating"] for r in result] == [5, 4]


def test_filter_and_sort_negative_sentiment():
    result = rl._filter_and_sort(_REVIEWS, sort="top_rated", sentiment="negative")
    assert [r["rating"] for r in result] == [2]


def test_filter_and_sort_negative_sentiment_no_matches_returns_empty():
    """A well-reviewed product with no low ratings -- matched_count=0 is a
    valid, expected result, not an error."""
    all_positive = [r for r in _REVIEWS if r["rating"] >= 4]
    result = rl._filter_and_sort(all_positive, sort="top_rated", sentiment="negative")
    assert result == []


def test_filter_and_sort_does_not_mutate_input():
    original_order = [r["rating"] for r in _REVIEWS]
    rl._filter_and_sort(_REVIEWS, sort="top_rated", sentiment=None)
    assert [r["rating"] for r in _REVIEWS] == original_order


def test_to_public_review_strips_pii():
    """reviewer.email/phone are real fields in Judge.me's payload (verified
    live) -- they must never reach the LLM/customer."""
    public = rl._to_public_review(_REVIEWS[0])
    assert public["reviewer_name"] == "Alice"
    assert "email" not in public
    assert "reviewer" not in public
    assert set(public.keys()) == {"rating", "body", "created_at", "reviewer_name", "verified"}


# ── aget_curated_reviews orchestration ─────────────────────────────────────
# Every test below mocks the adapter directly. The fetch behind these calls
# is memoised per client_id+product id (see the tiered-cache section at the
# end of this file); _isolated_review_cache above clears that between tests
# so each one exercises its own mock.

def _mock_adapter(resolve_result=None, reviews_result=None, all_reviews_side_effect=None):
    mock_instance = AsyncMock()
    if resolve_result is not None:
        mock_instance.aresolve_product_id = AsyncMock(return_value=resolve_result)
    if reviews_result is not None:
        mock_instance.aget_reviews = AsyncMock(return_value=reviews_result)
    if all_reviews_side_effect is not None:
        mock_instance.aget_all_published_reviews = AsyncMock(side_effect=all_reviews_side_effect)
    return mock_instance


@pytest.mark.asyncio
async def test_curated_reviews_configuration_missing_propagates():
    mock_instance = _mock_adapter(resolve_result={
        "success": False, "status": "configuration_missing", "message": "Judge.me configuration missing or placeholder",
    })
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        result = await rl.aget_curated_reviews(client_id="c1", external_id="123")

    assert result["success"] is False
    assert result["status"] == "configuration_missing"


@pytest.mark.asyncio
async def test_curated_reviews_resolves_then_fetches_with_resolved_id():
    """aresolve_product_id is called with the Shopify external_id, and the
    Judge.me internal id it returns is what the reviews fetch is keyed on."""
    mock_instance = _mock_adapter(
        resolve_result={"success": True, "id": "jm-42"},
        reviews_result={"success": True, "reviews": _REVIEWS, "current_page": 1, "per_page": 100, "is_last_page": True},
    )
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        result = await rl.aget_curated_reviews(client_id="c1", external_id="123")

    mock_instance.aresolve_product_id.assert_awaited_once_with("123")
    mock_instance.aget_reviews.assert_awaited_once_with("jm-42", published_only=True, page=1, per_page=100)
    assert result["success"] is True
    assert result["total_published"] == 3


@pytest.mark.asyncio
async def test_curated_reviews_not_found_when_no_judgeme_product():
    mock_instance = _mock_adapter(resolve_result={"success": True, "id": None})
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        result = await rl.aget_curated_reviews(client_id="c1", external_id="123")

    assert result["success"] is False
    assert result["status"] == "not_found"


@pytest.mark.asyncio
async def test_curated_reviews_not_found_propagates_adapter_status():
    """A genuine Judge.me 404 -- aresolve_product_id itself already returns
    success=False/status=not_found; must be passed through, not swallowed."""
    mock_instance = _mock_adapter(resolve_result={
        "success": False, "status": "not_found", "message": "No Judge.me product for external_id=123",
    })
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        result = await rl.aget_curated_reviews(client_id="c1", external_id="123")

    assert result["success"] is False
    assert result["status"] == "not_found"


@pytest.mark.asyncio
async def test_curated_reviews_success_applies_sort_and_limit():
    mock_instance = _mock_adapter(
        resolve_result={"success": True, "id": "jm-1"},
        reviews_result={"success": True, "reviews": _REVIEWS, "current_page": 1, "per_page": 100, "is_last_page": True},
    )
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        result = await rl.aget_curated_reviews(
            client_id="c1", external_id="123", sort="top_rated", sentiment=None, limit=2,
        )

    mock_instance.aget_reviews.assert_awaited_once_with("jm-1", published_only=True, page=1, per_page=100)
    assert result["success"] is True
    assert result["total_published"] == 3
    assert result["matched_count"] == 3  # no sentiment filter -- all 3 match
    assert len(result["reviews"]) == 2  # limit=2 applied after sort
    assert [r["rating"] for r in result["reviews"]] == [5, 4]
    assert result["older_reviews_unfetched"] is False


@pytest.mark.asyncio
async def test_curated_reviews_distinguishes_no_match_from_no_reviews():
    """matched_count=0 with total_published>0 -- 'no negative reviews', not
    'this product has no reviews'."""
    all_positive = [r for r in _REVIEWS if r["rating"] >= 4]
    mock_instance = _mock_adapter(
        resolve_result={"success": True, "id": "jm-1"},
        reviews_result={"success": True, "reviews": all_positive, "current_page": 1, "per_page": 100, "is_last_page": True},
    )
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        result = await rl.aget_curated_reviews(client_id="c1", external_id="123", sentiment="negative")

    assert result["success"] is True
    assert result["total_published"] == 2
    assert result["matched_count"] == 0
    assert result["reviews"] == []


@pytest.mark.asyncio
async def test_curated_reviews_limit_capped_at_max():
    big_list = [dict(_REVIEWS[0], rating=5, created_at=f"2026-01-{i:02d}T00:00:00+00:00") for i in range(1, 30)]
    mock_instance = _mock_adapter(
        resolve_result={"success": True, "id": "jm-1"},
        reviews_result={"success": True, "reviews": big_list, "current_page": 1, "per_page": 100, "is_last_page": True},
    )
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        result = await rl.aget_curated_reviews(client_id="c1", external_id="123", limit=1000)

    assert len(result["reviews"]) == rl._MAX_LIMIT


@pytest.mark.asyncio
async def test_curated_reviews_flags_older_reviews_unfetched():
    """A product with >100 reviews -- only the first page was checked, and
    the caller must be told the result isn't exhaustive."""
    mock_instance = _mock_adapter(
        resolve_result={"success": True, "id": "jm-1"},
        reviews_result={"success": True, "reviews": _REVIEWS, "current_page": 1, "per_page": 100, "is_last_page": False},
    )
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        result = await rl.aget_curated_reviews(client_id="c1", external_id="123")

    assert result["older_reviews_unfetched"] is True


@pytest.mark.asyncio
async def test_curated_reviews_fetches_single_page_only():
    """Confirms the implementation calls the single-page adapter method, not
    the full-history-loop one -- one API call, not N."""
    mock_instance = _mock_adapter(
        resolve_result={"success": True, "id": "jm-1"},
        reviews_result={"success": True, "reviews": _REVIEWS, "current_page": 1, "per_page": 100, "is_last_page": True},
        all_reviews_side_effect=AssertionError("should not loop all pages -- single page only"),
    )
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        await rl.aget_curated_reviews(client_id="c1", external_id="123")

    mock_instance.aget_reviews.assert_awaited_once()
    mock_instance.aget_all_published_reviews.assert_not_called()




# ── fetch-once caching: one Judge.me fetch reused across "show more",
# re-sorts, and later turns ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_second_call_reuses_cached_fetch_and_skips_judgeme_entirely():
    """The whole point of the feature: a repeat call for the same product
    must not touch the adapter at all, not even to resolve the product id."""
    mock_instance = _mock_adapter(
        resolve_result={"success": True, "id": "jm-1"},
        reviews_result={"success": True, "reviews": _REVIEWS, "current_page": 1, "per_page": 100, "is_last_page": True},
    )
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        first = await rl.aget_curated_reviews(client_id="c1", external_id="123")
        second = await rl.aget_curated_reviews(client_id="c1", external_id="123")

    assert first["success"] is True and second["success"] is True
    assert MockAdapter.create.await_count == 1
    mock_instance.aresolve_product_id.assert_awaited_once()
    mock_instance.aget_reviews.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeat_call_is_identical_and_costs_no_extra_judgeme_call():
    """The load-bearing property of the no-paging design.

    "Show me more" is a fresh call with the SAME arguments, and the model
    picks the next five by looking at which ones it has already printed. That
    only works if the list is byte-for-byte the same every turn -- a
    re-ordered or re-fetched list would make "the next five" name different
    reviews than the customer actually saw. It must also not cost a second
    Judge.me round trip: the model now calls on EVERY review turn, so the
    cached fetch is what keeps that free."""
    seven = [
        dict(_REVIEWS[0], rating=5, body=f"review {i}", created_at=f"2026-01-{i:02d}T00:00:00+00:00")
        for i in range(1, 8)
    ]
    mock_instance = _mock_adapter(
        resolve_result={"success": True, "id": "jm-1"},
        reviews_result={"success": True, "reviews": seven, "current_page": 1, "per_page": 100, "is_last_page": True},
    )
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        first = await rl.aget_curated_reviews(client_id="c1", external_id="123", sort="recent")
        second = await rl.aget_curated_reviews(client_id="c1", external_id="123", sort="recent")

    mock_instance.aget_reviews.assert_awaited_once()  # ONE fetch served both turns
    assert first["reviews"] == second["reviews"]
    assert first["sort_used"] == second["sort_used"]
    assert first["matched_count"] == second["matched_count"]


@pytest.mark.asyncio
async def test_cached_fetch_still_derives_each_sort_and_sentiment_fresh():
    """Reuse must re-filter/re-sort from the cached LIST every time --
    serving a previously *filtered* result for a different sentiment would
    silently drop reviews that should now match."""
    mock_instance = _mock_adapter(
        resolve_result={"success": True, "id": "jm-1"},
        reviews_result={"success": True, "reviews": _REVIEWS, "current_page": 1, "per_page": 100, "is_last_page": True},
    )
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        positive = await rl.aget_curated_reviews(client_id="c1", external_id="123",
                                                 sort="top_rated", sentiment="positive")
        negative = await rl.aget_curated_reviews(client_id="c1", external_id="123",
                                                 sort="top_rated", sentiment="negative")
        recent = await rl.aget_curated_reviews(client_id="c1", external_id="123", sort="recent")

    mock_instance.aget_reviews.assert_awaited_once()
    assert [r["rating"] for r in positive["reviews"]] == [5, 4]
    assert [r["rating"] for r in negative["reviews"]] == [2]
    assert [r["created_at"] for r in recent["reviews"]][0] == "2026-08-23T09:35:49+00:00"


@pytest.mark.asyncio
async def test_cache_is_scoped_per_tenant_and_per_product():
    """Two tenants can carry the same Shopify product id, and one product's
    reviews must never be served for another -- so a differing client_id or
    external_id has to miss and fetch on its own."""
    mock_instance = _mock_adapter(
        resolve_result={"success": True, "id": "jm-1"},
        reviews_result={"success": True, "reviews": _REVIEWS, "current_page": 1, "per_page": 100, "is_last_page": True},
    )
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        await rl.aget_curated_reviews(client_id="c1", external_id="123")
        await rl.aget_curated_reviews(client_id="c2", external_id="123")  # other tenant
        await rl.aget_curated_reviews(client_id="c1", external_id="456")  # other product

    assert mock_instance.aget_reviews.await_count == 3


@pytest.mark.asyncio
async def test_returned_reviews_are_copies_so_callers_cannot_corrupt_the_cache():
    """tool_factory pops `rating` off returned reviews when the tenant's
    rating display is off. If that mutated the cached dicts, every LATER
    call for the product would come back rating-less."""
    mock_instance = _mock_adapter(
        resolve_result={"success": True, "id": "jm-1"},
        reviews_result={"success": True, "reviews": _REVIEWS, "current_page": 1, "per_page": 100, "is_last_page": True},
    )
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        first = await rl.aget_curated_reviews(client_id="c1", external_id="123")
        for review in first["reviews"]:
            review.pop("rating", None)  # what the rating-display toggle does
        second = await rl.aget_curated_reviews(client_id="c1", external_id="123")

    assert all("rating" in r for r in second["reviews"])


@pytest.mark.asyncio
async def test_failures_are_never_cached():
    """A configuration_missing/not_found/transport failure must not leave an
    entry a later call could serve -- once the tenant is configured, the very
    next call has to go live rather than replaying the failure for the TTL."""
    failing = _mock_adapter(resolve_result={
        "success": False, "status": "configuration_missing", "message": "x",
    })
    working = _mock_adapter(
        resolve_result={"success": True, "id": "jm-1"},
        reviews_result={"success": True, "reviews": _REVIEWS, "current_page": 1, "per_page": 100, "is_last_page": True},
    )
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=failing)
        failed = await rl.aget_curated_reviews(client_id="c1", external_id="123")

        MockAdapter.create = AsyncMock(return_value=working)
        recovered = await rl.aget_curated_reviews(client_id="c1", external_id="123")

    assert failed["success"] is False
    assert failed["status"] == "configuration_missing"
    assert recovered["success"] is True
    working.aget_reviews.assert_awaited_once()


@pytest.mark.asyncio
async def test_cached_payload_carries_no_reviewer_pii():
    """Reviews are trimmed BEFORE they are cached, so reviewer email/phone
    never reach Redis -- not merely never reach the LLM."""
    mock_instance = _mock_adapter(
        resolve_result={"success": True, "id": "jm-1"},
        reviews_result={"success": True, "reviews": _REVIEWS, "current_page": 1, "per_page": 100, "is_last_page": True},
    )
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        payload = await rl._aget_reviews_once("c1", "123")

    assert payload["reviews"]
    for cached_review in payload["reviews"]:
        assert set(cached_review.keys()) == {"rating", "body", "created_at", "reviewer_name", "verified"}


# ── the shacket distribution ───────────────────────────────────────────────

def _synthetic_reviews(count, rating, first_day=1):
    """`count` published reviews all at `rating`, on distinct descending dates
    so top_rated's (rating, created_at) tie-break is deterministic."""
    return [
        {
            "rating": rating,
            "body": f"review {rating}star #{i}",
            "created_at": f"2025-{(first_day + i) // 28 + 1:02d}-{(first_day + i) % 28 + 1:02d}T00:00:00+00:00",
            "published": True,
            "reviewer": {"name": f"R{i}", "email": "r@x.com"},
            "verified": True,
        }
        for i in range(count)
    ]


# The real Evolve: The Cosmic Shacket distribution (Judge.me product
# 1752556458 -> external_id 9934369816898): 18 published reviews, of which
# exactly one is 3-star -- "I ordered medium size but it still looks like L
# size". Under top_rated that review sorts DEAD LAST (position 18), which is
# what makes it the right regression fixture: any slice smaller than the full
# set drops the only review that answers a sizing question.
_SHACKET_SIZING_COMPLAINT = {
    "rating": 3,
    "body": "I ordered medium size but it still looks like L size",
    "created_at": "2025-05-21T15:53:47+00:00",
    "published": True,
    "reviewer": {"name": "Anon", "email": "a@x.com"},
    "verified": True,
}
_SHACKET_PUBLISHED = _synthetic_reviews(14, 5) + _synthetic_reviews(3, 4) + [_SHACKET_SIZING_COMPLAINT]


def _shacket_adapter():
    return _mock_adapter(
        resolve_result={"success": True, "id": "jm-shacket"},
        reviews_result={
            "success": True,
            "reviews": _SHACKET_PUBLISHED,
            "current_page": 1,
            "per_page": 100,
            "is_last_page": True,
        },
    )


async def _curated(**kwargs):
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=_shacket_adapter())
        return await rl.aget_curated_reviews(client_id="c1", external_id="9934369816898", **kwargs)


@pytest.mark.asyncio
async def test_list_mode_top_rated_hides_the_lowest_rated_review():
    """A 5-review slice of the real shacket data cannot contain the only
    review that mentions sizing, because top_rated sorts it last -- so a
    listing is a sample, and an answer drawn from it must not be presented as
    what customers generally think."""
    result = await _curated(sort="top_rated", limit=5)

    assert len(result["reviews"]) == 5
    assert all(r["rating"] == 5 for r in result["reviews"])
    bodies = [r["body"] for r in result["reviews"]]
    assert _SHACKET_SIZING_COMPLAINT["body"] not in bodies










@pytest.mark.asyncio
async def test_three_star_review_is_in_neither_sentiment_bucket():
    """A gap worth knowing about before routing an attribute question through
    a sentiment filter: `positive` is rating >= 4 and `negative` is rating
    <= 2, so a 3-star review matches NEITHER. On the real shacket data the
    only review that answers "how's the sizing" is that 3-star one -- so
    `sentiment="negative"` returns nothing at all, and a caller reading
    matched_count == 0 as "no complaints" would be wrong.

    An unfiltered listing is what actually surfaces it, which is why an
    attribute question must not be routed through a sentiment filter."""
    negative = await _curated(sentiment="negative", limit=20)
    assert negative["reviews"] == []
    assert negative["matched_count"] == 0
    assert negative["total_published"] == 18

    unfiltered = await _curated(limit=20)
    assert _SHACKET_SIZING_COMPLAINT["body"] in [r["body"] for r in unfiltered["reviews"]]


# ── sort_used echo ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sort_used_echoes_the_ordering_actually_applied():
    """A caller repeating a call needs to COMPARE, not remember: "the next
    five" is meaningless if the ordering changed underneath it, so the result
    states which ordering produced it."""
    top = await _curated(sort="top_rated")
    recent = await _curated(sort="recent")

    assert top["sort_used"] == "top_rated"
    assert recent["sort_used"] == "recent"



@pytest.mark.asyncio
async def test_sort_used_reflects_the_default_when_sort_is_omitted():
    """The whole point: a caller that omitted `sort` can still see WHICH
    ordering it got. In production the orchestrator resolves the tenant's
    configured ordering before this layer; here the module default applies."""
    result = await _curated(limit=5)

    assert result["sort_used"] == "recent" == rl.aget_curated_reviews.__defaults__[0]


@pytest.mark.asyncio
async def test_sort_used_absent_on_failure_results():
    """Failures return early with the adapter's status shape -- no ordering
    was applied, so claiming one would be a lie."""
    mock_instance = _mock_adapter(
        resolve_result={"success": False, "status": "not_found", "message": "no product"}
    )
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        result = await rl.aget_curated_reviews(client_id="c1", external_id="nope")

    assert result["success"] is False
    assert "sort_used" not in result


# ── one batch, no paging ──────────────────────────────────────────────────
# The tool hands over its whole batch in one call and the caller displays it
# five at a time. These pin the parts of that contract a caller depends on:
# the size of the batch, and the fact that matched_count can exceed it.

def _adapter_for(reviews):
    return _mock_adapter(
        resolve_result={"success": True, "id": "jm-1"},
        reviews_result={"success": True, "reviews": reviews, "current_page": 1,
                        "per_page": 100, "is_last_page": True},
    )


def _many(count, rating=5):
    """`count` published reviews, distinct bodies, newest last."""
    return [
        {"rating": rating, "body": f"review {i:03d}",
         "created_at": f"2026-01-01T00:00:{i:02d}+00:00", "published": True,
         "reviewer": {"name": f"R{i}", "email": f"{i}@x.com"}, "verified": True}
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_one_call_returns_the_whole_batch_of_fifteen():
    """15, not 5: the caller shows five at a time out of ONE result, so a
    single call has to carry all three chunks."""
    mock_instance = _adapter_for(_many(40))
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        result = await rl.aget_curated_reviews(client_id="c1", external_id="123")

    assert len(result["reviews"]) == 15
    assert rl._DEFAULT_LIMIT == 15


@pytest.mark.asyncio
async def test_matched_count_reports_the_real_total_not_the_batch_size():
    """40 matched, 15 returned. The caller must be able to tell it is holding
    a subset -- reporting 15 here would let it tell the customer that is every
    review the product has."""
    mock_instance = _adapter_for(_many(40))
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        result = await rl.aget_curated_reviews(client_id="c1", external_id="123")

    assert result["matched_count"] == 40
    assert result["total_published"] == 40
    assert len(result["reviews"]) == 15


@pytest.mark.asyncio
async def test_a_short_list_returns_everything_it_has():
    mock_instance = _adapter_for(_many(4))
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        result = await rl.aget_curated_reviews(client_id="c1", external_id="123")

    assert len(result["reviews"]) == 4
    assert result["matched_count"] == 4


@pytest.mark.asyncio
async def test_no_paging_fields_are_returned():
    """Nothing in the result may look like a cursor. A leftover has_more or
    next_page_token would put the model back to composing positions, which is
    the entire failure this design removed."""
    mock_instance = _adapter_for(_many(40))
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        result = await rl.aget_curated_reviews(client_id="c1", external_id="123")

    assert "has_more" not in result
    assert "next_page_token" not in result
    assert "offset_used" not in result


@pytest.mark.asyncio
async def test_a_new_sentiment_refilters_the_full_fetch_not_the_last_batch():
    """A switched filter is a new call, and it must be answered from all 100
    fetched reviews. Filtering the previous batch of 15 instead would hide
    every matching review that sat below it -- here, all 6 negatives."""
    mixed = _many(30, rating=5) + _many(6, rating=1)
    mock_instance = _adapter_for(mixed)
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        unfiltered = await rl.aget_curated_reviews(client_id="c1", external_id="123")
        negative = await rl.aget_curated_reviews(
            client_id="c1", external_id="123", sentiment="negative"
        )

    # None of the 1-star reviews made the unfiltered top-15 ...
    assert all(r["rating"] == 5 for r in unfiltered["reviews"])
    # ... yet the filtered call still finds every one of them.
    assert negative["matched_count"] == 6
    assert len(negative["reviews"]) == 6
    assert all(r["rating"] == 1 for r in negative["reviews"])


# ── a result says which product it is for ─────────────────────────────────
# One turn can hold results for more than one product ("compare the reviews of
# these two"). The reviews themselves carry no product, so without this field
# the only thing keeping the batches apart is the model's memory of the order
# it called in -- which was observed live putting one product's reviews under
# another's name.

@pytest.mark.asyncio
async def test_result_carries_the_product_it_is_for():
    mock_instance = _adapter_for(_many(4))
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        result = await rl.aget_curated_reviews(client_id="c1", external_id="123")

    assert result["product_id"] == "123"


@pytest.mark.asyncio
async def test_each_product_result_names_its_own_product():
    """Two products fetched in one turn stay distinguishable by the data
    alone, not by the order the caller happens to remember."""
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=_adapter_for(_many(3)))
        first = await rl.aget_curated_reviews(client_id="c1", external_id="111")
    clear_tiered_cache()
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=_adapter_for(_many(2)))
        second = await rl.aget_curated_reviews(client_id="c1", external_id="222")

    assert first["product_id"] == "111"
    assert second["product_id"] == "222"
    assert first["product_id"] != second["product_id"]


@pytest.mark.asyncio
async def test_product_id_is_normalised_the_way_the_lookup_was():
    """Whitespace in, clean id out -- so a caller comparing the echoed id to
    the one it sent does not see a spurious mismatch."""
    mock_instance = _adapter_for(_many(2))
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=mock_instance)
        result = await rl.aget_curated_reviews(client_id="c1", external_id="  123  ")

    assert result["product_id"] == "123"


# ── a result carries the product's NAME, not just its id ─────────────────
# product_id alone was not enough: it asks the caller to compare two long
# numbers, which is the check that was observed failing -- one product's
# reviews were shown under another product's name with the correct id sitting
# in the result. A title is text to copy instead of a number to compare.

def _adapter_with_title(reviews, title):
    mock_instance = _mock_adapter(
        resolve_result={"success": True, "id": "jm-1", "product": {"id": "jm-1", "title": title}},
        reviews_result={"success": True, "reviews": reviews, "current_page": 1,
                        "per_page": 100, "is_last_page": True},
    )
    return mock_instance


@pytest.mark.asyncio
async def test_result_carries_the_product_title():
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=_adapter_with_title(_many(3), "Oversized Denim Jacket"))
        result = await rl.aget_curated_reviews(client_id="c1", external_id="123")

    assert result["product_title"] == "Oversized Denim Jacket"


@pytest.mark.asyncio
async def test_title_is_absent_rather_than_none_when_judgeme_has_none():
    """A missing key is easier to reason about than one holding None, and the
    tool description tells the model to name no product when it is absent."""
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=_adapter_with_title(_many(3), None))
        result = await rl.aget_curated_reviews(client_id="c1", external_id="123")

    assert "product_title" not in result
    assert result["success"] is True  # a missing title is not a failure


@pytest.mark.asyncio
async def test_title_and_reviews_always_come_from_the_same_fetch():
    """The property that makes labelling from the title safe: a result cannot
    carry one product's name over another product's reviews."""
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=_adapter_with_title(_many(2), "Product A"))
        a = await rl.aget_curated_reviews(client_id="c1", external_id="aaa")
    clear_tiered_cache()
    with patch(f"{_MODULE}.JudgeMeReviewsAdapter") as MockAdapter:
        MockAdapter.create = AsyncMock(return_value=_adapter_with_title(_many(5), "Product B"))
        b = await rl.aget_curated_reviews(client_id="c1", external_id="bbb")

    assert (a["product_title"], a["product_id"], len(a["reviews"])) == ("Product A", "aaa", 2)
    assert (b["product_title"], b["product_id"], len(b["reviews"])) == ("Product B", "bbb", 5)


def test_cache_key_is_versioned_so_an_older_payload_shape_is_not_read_back():
    """product_title was added to the cached payload. Without a version bump,
    entries from the previous deploy would return titleless for the whole
    24-hour TTL and review replies would stop naming products."""
    key = rl._reviews_cache_key("c1", "123")
    assert rl._REVIEWS_CACHE_VERSION in key
    assert key.endswith("c1:123")
