"""Curated Judge.me review listing -- resolves a Shopify product to its
Judge.me reviews, then deterministically filters/sorts them.

Design note (why filtering/sorting live here, not in the prompt): the LLM
picks *which* sort/sentiment to request based on how the customer
phrased it (e.g. "top 5" -> sort="top_rated", "negative reviews" ->
sentiment="negative") -- that's a small, low-risk intent classification.
The actual filtering and ranking happens here, in plain code, so the model
never reorders, drops, or invents a review itself. Verified live against a
real Judge.me shop (Concept Groove) before this module was written -- see
the review fields consumed below (``rating``, ``body``, ``created_at``,
``published``, ``reviewer``).

No paging, by design. One call returns the whole batch the customer can be
shown for a given sort/sentiment (``_DEFAULT_LIMIT``, currently 15) and the
caller hands it out in smaller chunks itself. An earlier version paged
server-side with a cursor the model had to copy between turns; the model
composed cursors instead of copying them, which restarted lists and re-served
reviews customers had already read, and every guard against that added more
machinery than the feature was worth. Displaying 15 already-fetched reviews
five at a time needs no cursor, no position, and no state -- so a "show me
more" is answered from the batch in hand, and the only thing that triggers a
new call is a NEW question (different product, sort, or sentiment).

``matched_count`` can therefore exceed ``len(reviews)``: a product with 40
positive reviews returns the top 15 and reports 40. Callers must not present
15 as the complete set -- past the batch, the product page is where the rest
live.

Fetch-once caching: the two Judge.me calls behind a listing (resolve the
product, then fetch its reviews) run through the shared tiered cache
(memory -> Redis), keyed by client_id + product id. A re-sort, a sentiment
switch, or a different customer asking about the same product within the TTL
all re-derive their slice from that ONE cached fetch rather than calling
Judge.me again. Filtering/sorting/slicing always happen fresh on top of the
cached list, never on an already-filtered result, so every sort/sentiment
combination stays correct.

The cache deliberately lives here rather than in conversation state:
LangGraph carries only declared channels across a turn boundary, so a dict
written into ``state`` by a tool is gone by the next turn. Conversation state
is also re-serialised on every turn, including turns with nothing to do with
reviews. Keying on client_id + product id instead makes the reuse survive
turns for free and shares it across conversations.

Only TRIMMED reviews (:func:`_to_public_review`) are ever cached -- the raw
Judge.me payload carries reviewer email/phone, which must not be written to
Redis.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Literal, Optional

from fashion_bot.env_loader import get_int
from fashion_bot.judgeme.tools.reviews_adapter import JudgeMeReviewsAdapter
from fashion_bot.utils.tiered_cache import aget_with_tiered_cache

logger = logging.getLogger(__name__)

SortMode = Literal["top_rated", "recent"]
Sentiment = Literal["positive", "negative"]

_POSITIVE_MIN_RATING = 4
_NEGATIVE_MAX_RATING = 2
# One batch, sized for the caller to hand out in chunks of five rather than
# to come back for more. There is no second call for the same sort/sentiment
# -- see the module docstring -- so this number is the whole listing the
# customer can be shown, and 15 is three chunks of five: enough that "show me
# more" has somewhere to go twice, small enough that the payload stays inside
# the tool-trace budget and the model is not tempted to summarise from it.
_DEFAULT_LIMIT = 15
# Equal to the default, deliberately: the caller does not choose a size, so
# this is a floor-and-ceiling rather than a cap on something variable. A
# direct caller asking for more still gets 15.
_MAX_LIMIT = 15

# How long one Judge.me fetch is reused for a product. A day: reviews
# accumulate over months, not minutes -- a busy product here gathers a few
# dozen across a year -- so the set a customer is shown is the same set
# whether it was fetched this minute or this morning. A short window pays a
# resolve+fetch on every conversation to protect against a change that has
# almost certainly not happened.
#
# Worth knowing rather than worth worrying about: nothing invalidates this
# cache -- no webhook, no write path -- so the TTL is the only thing that
# refreshes it. If a store ever needs same-day visibility of a new review,
# either shorten this or bust the key on the review webhook.
#
# Memory is bounded, not unbounded: the memory tier's per-TTL bucket holds
# at most IN_MEMORY_CACHE_MAX_ENTRIES (default 2000) and evicts LRU beyond
# that, so the ceiling is ~2000 products x one trimmed page each -- reached
# only by a process that touched 2000 distinct products in a day.
#
# MUST NOT equal CONFIG_MEMORY_TTL (config_manager.py, default 600). The
# tiered cache's memory tier partitions entries into per-TTL buckets of
# IN_MEMORY_CACHE_MAX_ENTRIES each (utils/tiered_cache.py:InMemoryTTLCache),
# keyed by the ttl_seconds value passed in -- so a DIFFERENT number is the
# only thing keeping review payloads (up to 100 reviews each) out of the
# bucket the config reads share with ~6 other call sites. Make the two equal
# and a burst of review traffic starts evicting config entries, which shows
# up as extra DB reads on a completely unrelated path.
_REVIEWS_CACHE_TTL_SECONDS = get_int("JUDGEME_REVIEWS_CACHE_TTL_SECONDS", 86_400)


class _ReviewFetchFailed(Exception):
    """Carries a failure result out of the cached loader.

    ``aget_with_tiered_cache`` caches whatever its loader returns, and a
    ``configuration_missing`` / ``not_found`` / transport failure must never
    be cached -- nor flattened into a bare miss, since callers distinguish
    those statuses. Raising past the cache keeps the specific result intact
    and leaves nothing behind in either tier.
    """

    def __init__(self, result: Dict[str, Any]):
        super().__init__(str(result.get("status") or "error"))
        self.result = result


def _rating_as_int(review: Dict[str, Any]) -> int:
    """Coerce a review's ``rating`` to int, defensively -- Judge.me's field
    is documented/verified as an int, but a string value (or anything else
    unexpected) would otherwise raise TypeError when compared/sorted against
    an int, taking down the whole request rather than just treating that one
    review as unrated."""
    try:
        return int(review.get("rating") or 0)
    except (TypeError, ValueError):
        return 0


def _filter_and_sort(
    reviews: List[Dict[str, Any]],
    sort: SortMode,
    sentiment: Optional[Sentiment],
) -> List[Dict[str, Any]]:
    """Pure function: apply sentiment filter, then sort. No I/O, no mutation
    of the input list (returns a new list)."""
    filtered = list(reviews)

    if sentiment == "positive":
        filtered = [r for r in filtered if _rating_as_int(r) >= _POSITIVE_MIN_RATING]
    elif sentiment == "negative":
        filtered = [r for r in filtered if _rating_as_int(r) <= _NEGATIVE_MAX_RATING]

    if sort == "recent":
        filtered.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    else:  # top_rated (default)
        filtered.sort(key=lambda r: (_rating_as_int(r), r.get("created_at") or ""), reverse=True)

    return filtered


def _to_public_review(review: Dict[str, Any]) -> Dict[str, Any]:
    """Trim a raw Judge.me review down to what's safe/useful to hand the LLM.

    Deliberately drops the ``reviewer`` object's email/phone/tags -- that's
    PII (verified present in the raw payload: reviewer.email, reviewer.phone)
    with no reason to ever reach a chat response. Only the display name
    survives.

    Applied at FETCH time, before anything is cached, so the PII never
    reaches Redis either. Not idempotent -- a second pass over an already
    trimmed review would null its ``reviewer_name`` -- so call it exactly
    once, in :func:`_aload_reviews_from_judgeme`.
    """
    reviewer = review.get("reviewer") or {}
    return {
        "rating": review.get("rating"),
        "body": review.get("body"),
        "created_at": review.get("created_at"),
        "reviewer_name": reviewer.get("name"),
        "verified": review.get("verified"),
    }


async def _aload_reviews_from_judgeme(client_id: str, external_id: str) -> Dict[str, Any]:
    """The two live Judge.me calls behind a listing, as one cacheable unit.

    Fetches only the FIRST page (up to 100 reviews, Judge.me's own per-page
    cap) -- deliberately not looping every page. For the "top 5" / "recent" /
    "positive" / "negative" use cases this tool exists for, the reviews on
    that page are enough; scanning a product's full history for a chat answer
    isn't worth an unbounded number of live calls. ``is_last_page`` records
    whether that page captured everything, so callers can present counts
    honestly rather than implying they are exhaustive.

    Returns the TRIMMED published reviews. Raises :class:`_ReviewFetchFailed`
    on any failure, so nothing failed or partial ever reaches the cache.
    """
    adapter = await JudgeMeReviewsAdapter.create(client_id=client_id)

    resolved = await adapter.aresolve_product_id(external_id)
    if not resolved.get("success"):
        raise _ReviewFetchFailed(resolved)  # configuration_missing / not_found, propagated as-is

    judgeme_product_id = resolved.get("id")
    if not judgeme_product_id:
        raise _ReviewFetchFailed(
            {"success": False, "status": "not_found", "message": "No Judge.me product resolved"}
        )

    result = await adapter.aget_reviews(judgeme_product_id, published_only=True, page=1, per_page=100)
    if not result.get("success"):
        raise _ReviewFetchFailed(result)

    # The resolve response already carries the product's title -- it was being
    # discarded. Keeping it lets a result be labelled by COPYING a name rather
    # than by the caller remembering which product it asked about; see the
    # product_title note in aget_curated_reviews.
    return {
        "reviews": [_to_public_review(r) for r in result["reviews"]],
        "is_last_page": result.get("is_last_page", True),
        "product_title": (resolved.get("product") or {}).get("title"),
    }


# Bumped whenever the CACHED PAYLOAD SHAPE changes, so entries written by an
# older deploy are ignored rather than read back missing their new fields.
#
# v2 added product_title. Without a bump, every product cached before the
# deploy would keep returning a payload with no title for the rest of the
# 24-hour TTL -- and the tool description tells the model to name no product
# when the title is absent, so review replies would quietly stop naming
# products for a day after release. A one-character version is cheaper than
# reasoning about that window.
_REVIEWS_CACHE_VERSION = "v2"


def _reviews_cache_key(client_id: str, external_id: str) -> str:
    """Tenant-scoped per-product key (AGENTS.md: every cache entry is scoped
    by client_id -- two tenants can carry the same Shopify product id)."""
    return f"judgeme_reviews:{_REVIEWS_CACHE_VERSION}:{client_id}:{external_id}"


async def _aget_reviews_once(client_id: str, external_id: str) -> Dict[str, Any]:
    """:func:`_aload_reviews_from_judgeme`, memoised through memory -> Redis."""
    cache_key = _reviews_cache_key(client_id, external_id)

    async def _get_from_redis() -> Optional[Dict[str, Any]]:
        from fashion_bot.utils.redis_client import get_shared_async_redis_client
        redis = await get_shared_async_redis_client()
        if redis is None:
            return None
        raw = await redis.get(cache_key)
        return json.loads(raw) if raw else None

    async def _set_to_redis(value: Dict[str, Any]) -> None:
        from fashion_bot.utils.redis_client import get_shared_async_redis_client
        redis = await get_shared_async_redis_client()
        if redis is not None:
            await redis.set(cache_key, json.dumps(value, default=str), ex=_REVIEWS_CACHE_TTL_SECONDS)

    # Redis get/set failures are caught and logged by aget_with_tiered_cache
    # itself, which then falls through to the live load -- no need to swallow
    # them a second time here.
    payload, _source = await aget_with_tiered_cache(
        cache_key=cache_key,
        ttl_seconds=_REVIEWS_CACHE_TTL_SECONDS,
        load_from_source_fn=lambda: _aload_reviews_from_judgeme(client_id, external_id),
        get_from_redis_fn=_get_from_redis,
        set_to_redis_fn=_set_to_redis,
    )
    return payload or {"reviews": [], "is_last_page": True}


async def aget_curated_reviews(
    client_id: str,
    external_id: str,
    # Matches config_manager._DEFAULT_REVIEW_SORT. In production the
    # orchestrator always resolves the tenant's ordering and passes it
    # explicitly, so this default only covers a direct caller -- but a
    # "top_rated" default here would contradict the product policy and read
    # as the real one to anyone grepping for it.
    sort: SortMode = "recent",
    sentiment: Optional[Sentiment] = None,
    limit: int = _DEFAULT_LIMIT,
) -> Dict[str, Any]:
    """Resolve ``external_id`` (Shopify product id) to Judge.me's product,
    fetch its published reviews, and return the first ``limit`` of them
    after a deterministic filter and sort.

    The fetch itself happens at most once per product per
    ``_REVIEWS_CACHE_TTL_SECONDS`` (see the module docstring) -- a re-sort or
    a different sentiment re-derives its slice from that cached fetch
    without calling Judge.me again.

    There is no position argument and no cursor. Every call answers one
    question -- "which reviews match THIS sort and sentiment" -- from the
    top of the list, and the caller displays the batch it gets however it
    likes. See the module docstring for why paging was removed.

    Returns one of:
    - ``{"success": False, "status": "configuration_missing", ...}`` -- no
      Judge.me API credentials configured for this tenant.
    - ``{"success": False, "status": "not_found", ...}`` -- Judge.me has no
      product record for this external_id (not yet synced, or genuinely
      not on Judge.me).
    - ``{"success": True, "reviews": [...], "sort_used": "top_rated"|"recent",
      "matched_count": N,
      "total_published": M, "older_reviews_unfetched": bool}`` -- ``reviews``
      is capped at ``limit`` (max ``_MAX_LIMIT`` regardless of what's
      requested). ``matched_count`` is how many published reviews (within
      the fetched page) matched the sentiment filter (0 is a valid,
      expected result -- e.g. "negative reviews" on a well-reviewed
      product), and may exceed the number returned. ``total_published`` is
      the published-review count within the fetched page, independent of any
      filter -- use it to distinguish "no reviews matched this filter" from
      "no reviews exist at all". ``older_reviews_unfetched`` is True when
      the product has more than 100 reviews total, i.e. the single fetched
      page is not the complete set. ``sort_used`` is the ordering this
      result was built with, echoed back so a caller that omitted ``sort``
      can see which tenant default was applied.
    """
    capped_limit = max(1, min(limit, _MAX_LIMIT))

    try:
        payload = await _aget_reviews_once(client_id, external_id)
    except _ReviewFetchFailed as failure:
        return failure.result

    published_reviews = payload["reviews"]
    total_published = len(published_reviews)

    filtered = _filter_and_sort(published_reviews, sort=sort, sentiment=sentiment)
    matched_count = len(filtered)

    return {
        "success": True,
        # Which product these reviews belong to, echoed back so a result is
        # self-identifying.
        #
        # A turn can hold more than one of these at once -- "compare the
        # reviews of these two" is an ordinary request, and the model resolves
        # such a phrase against everything in context. Observed live: it
        # fetched two products, neither of them the one the customer meant,
        # and presented the reviews under a third product's name. Real
        # customers' words on a product they never bought is worse than an
        # invented review, and nothing in the result shape let the model --
        # or a log reader -- catch it, because the reviews arrived anonymous.
        #
        # Echoing the id costs nothing and makes the mix-up detectable rather
        # than silent: every batch now carries the product it came from.
        "product_id": str(external_id).strip(),
        # The product's name, for the caller to LABEL this batch with.
        #
        # product_id above turned out not to be enough. It asks the model to
        # compare two long numbers, which is exactly the check it does not
        # perform: asked for one product's reviews it fetched another's, was
        # handed the correct id in this field, and still wrote the first
        # product's name above the second product's reviews.
        #
        # A title is different in kind -- it is text to copy, and copying text
        # out of a tool result is the one thing this caller does reliably
        # (every reviewer name and body it has ever been given came back
        # verbatim). Labelling from here cannot silently drift from the
        # reviews underneath it, because both come from the same result.
        #
        # Absent rather than None when unknown: a cache entry written before
        # this field existed has no title, and a key that is simply missing
        # is easier to reason about than one holding None.
        **({"product_title": payload["product_title"]}
           if payload.get("product_title") else {}),
        # Copies, not the cached dicts themselves: a caller mutating a
        # returned review (tool_factory strips `rating` when the tenant's
        # rating display is off) would otherwise corrupt the shared
        # memory-tier entry for every later call.
        "reviews": [dict(r) for r in filtered[:capped_limit]],
        # Can be larger than len(reviews) -- it counts what MATCHED, not what
        # was returned. That gap is the honest one: with 40 matching reviews
        # and a cap of 15, the caller is holding 15 of 40 and should not
        # imply it has them all.
        "matched_count": matched_count,
        "total_published": total_published,
        # The sort actually applied. Omitting `sort` resolves to the tenant's
        # configured ordering (aget_judgeme_default_review_sort), so echoing
        # it back is the only way a caller can tell which one it got.
        "sort_used": sort,
        # Named for what it measures -- whether the single fetched page left
        # older reviews behind -- rather than "are there more reviews".
        "older_reviews_unfetched": not payload.get("is_last_page", True),
    }
