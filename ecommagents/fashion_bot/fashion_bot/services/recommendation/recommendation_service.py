"""
Recommendation Service - main orchestrator for the recommendation engine.

Coordinates:
1. Query Understanding (LLM #1) - search query + filter
2. Upstash Search retrieval
3. Business Rules reranking
4. Response Generation (LLM #2)

The shared ``search_products_pipeline`` function exposes steps 1-3 as a
standalone coroutine so that callers that already have their own LLM
(tool_factory, tool_registry, demo_chat_backend) don't need to go through
the full RecommendationService which adds an extra response-generation LLM
call.
"""

import asyncio
import re
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, List

from fashion_bot.env_loader import get_int
from fashion_bot.services.product_ingestion.upstash_search_service import (
    get_upstash_search_service,
)
from fashion_bot.services.product_ingestion.models import iso_to_epoch

# New-arrivals recency window (days). Resolved per request by precedence:
#   1. an explicit user timeframe (QU `recency_days`, e.g. "new this month"),
#   2. a per-client default in client_configs (NEW_ARRIVAL_WINDOW_CONFIG_KEY),
#   3. the env-driven global default below.
# Every layer is clamped to [MIN, MAX] so neither a bad config value nor an
# LLM-emitted number can produce a nonsensical (≤0) or unbounded window. No
# magic numbers: the defaults/bounds are env-overridable (AGENTS.md §config).
NEW_ARRIVAL_WINDOW_CONFIG_KEY = "new_arrival_window_days"
NEW_ARRIVAL_DEFAULT_WINDOW_DAYS = get_int("NEW_ARRIVAL_DEFAULT_WINDOW_DAYS", 365)
NEW_ARRIVAL_MIN_WINDOW_DAYS = get_int("NEW_ARRIVAL_MIN_WINDOW_DAYS", 1)
NEW_ARRIVAL_MAX_WINDOW_DAYS = get_int("NEW_ARRIVAL_MAX_WINDOW_DAYS", 3650)

# Per-client product-discovery counts. Each resolves per request as
# `client_configs` override > env-driven default, clamped to sane bounds and
# fail-open to default (same pattern as the new-arrivals window above):
#   * how many docs we FETCH from Upstash Search for new-arrivals vs bestseller
#     queries (a bigger pool gives reranking room), and
#   * how many products the search_products tool RETURNS after reranking.
NEW_ARRIVAL_FETCH_LIMIT_CONFIG_KEY = "new_arrival_fetch_limit"
NEW_ARRIVAL_FETCH_LIMIT_DEFAULT = get_int("NEW_ARRIVAL_FETCH_LIMIT_DEFAULT", 20)
BESTSELLER_FETCH_LIMIT_CONFIG_KEY = "bestseller_fetch_limit"
BESTSELLER_FETCH_LIMIT_DEFAULT = get_int("BESTSELLER_FETCH_LIMIT_DEFAULT", 20)
# Default Upstash fetch for a plain (non-newest, non-bestseller) search.
PRODUCT_SEARCH_FETCH_LIMIT_DEFAULT = get_int("PRODUCT_SEARCH_FETCH_LIMIT_DEFAULT", 20)
# Shared bounds for any Upstash fetch limit (topK must be positive & bounded).
PRODUCT_FETCH_LIMIT_MIN = get_int("PRODUCT_FETCH_LIMIT_MIN", 1)
PRODUCT_FETCH_LIMIT_MAX = get_int("PRODUCT_FETCH_LIMIT_MAX", 250)

PRODUCT_RESULTS_COUNT_CONFIG_KEY = "product_results_count"
PRODUCT_RESULTS_COUNT_DEFAULT = get_int("PRODUCT_RESULTS_COUNT_DEFAULT", 5)
PRODUCT_RESULTS_COUNT_MIN = get_int("PRODUCT_RESULTS_COUNT_MIN", 1)
PRODUCT_RESULTS_COUNT_MAX = get_int("PRODUCT_RESULTS_COUNT_MAX", 50)

# Per-client variant-availability threshold for the QU hard filter. Products
# with variant_availability_pct <= this value are excluded from browse/listing
# results. Resolved per request: client_configs override > env default.
VARIANT_AVAILABILITY_MIN_PCT_CONFIG_KEY = "variant_availability_min_pct"
VARIANT_AVAILABILITY_MIN_PCT_DEFAULT = get_int("VARIANT_AVAILABILITY_MIN_PCT_DEFAULT", 40)
VARIANT_AVAILABILITY_MIN_PCT_MIN = 0
VARIANT_AVAILABILITY_MIN_PCT_MAX = 100

# Per-client minimum-results threshold that triggers filter relaxation.
# If the initial search returns fewer than this many results, the pipeline
# progressively drops filters. Resolved: client_configs > env default.
FILTER_RELAXATION_THRESHOLD_CONFIG_KEY = "filter_relaxation_threshold"
FILTER_RELAXATION_THRESHOLD_DEFAULT = get_int("FILTER_RELAXATION_THRESHOLD_DEFAULT", 3)
FILTER_RELAXATION_THRESHOLD_MIN = 1
FILTER_RELAXATION_THRESHOLD_MAX = 20

from fashion_bot.services.recommendation.query_understanding import (
    understand_query,
    QueryUnderstandingResult,
)
from fashion_bot.services.recommendation.business_rules_reranker import rerank
from fashion_bot.services.recommendation.response_generator import generate_response

logger = logging.getLogger(__name__)


async def aresolve_client_int(
    client_id: Optional[str],
    config_key: str,
    default: int,
    *,
    min_value: int,
    max_value: int,
) -> int:
    """Resolve a per-client integer setting: ``client_configs`` override (read
    through the tiered cache) > ``default``. Always clamped to
    ``[min_value, max_value]`` and fail-open to ``default`` on any read/parse
    error, so a missing or bad value never breaks product discovery
    (AGENTS.md: config-driven, tenant-scoped, graceful degradation).
    """
    value = default
    if client_id:
        try:
            # Lazy import to match the established config-read pattern and avoid
            # a module-level import cycle (config_manager is heavy).
            from fashion_bot.config_manager import aget_config
            raw = await aget_config(config_key, client_id=client_id)
            if raw is not None and str(raw).strip():
                value = int(str(raw).strip())
        except (TypeError, ValueError):
            logger.warning(
                "[search_pipeline] client=%s has non-integer %s config; using default %d",
                client_id, config_key, default,
            )
            value = default
        except Exception as e:
            logger.warning(
                "[search_pipeline] %s config read failed for client=%s: %s; using default %d",
                config_key, client_id, e, default,
            )
            value = default
    return max(min_value, min(value, max_value))


def _clamp_window_days(days: int) -> int:
    """Clamp a new-arrivals window to the configured [MIN, MAX] day bounds."""
    return max(NEW_ARRIVAL_MIN_WINDOW_DAYS, min(int(days), NEW_ARRIVAL_MAX_WINDOW_DAYS))


async def _aresolve_new_arrival_window_days(
    client_id: Optional[str], explicit_days: Optional[int]
) -> int:
    """Resolve the new-arrivals recency window (days) for this request.

    Precedence: explicit user timeframe (QU ``recency_days``) > per-client
    ``client_configs`` value > env-driven default, all clamped to the window
    bounds; config reads fail open to the default.
    """
    # 1. Explicit timeframe the user asked for (highest priority).
    if explicit_days is not None:
        return _clamp_window_days(explicit_days)
    # 2/3. Per-client configured value > env default (clamped, fail-open).
    return await aresolve_client_int(
        client_id,
        NEW_ARRIVAL_WINDOW_CONFIG_KEY,
        NEW_ARRIVAL_DEFAULT_WINDOW_DAYS,
        min_value=NEW_ARRIVAL_MIN_WINDOW_DAYS,
        max_value=NEW_ARRIVAL_MAX_WINDOW_DAYS,
    )


def _created_after_epoch(result: Dict[str, Any], cutoff_epoch: int) -> bool:
    """True when the product was created on/after ``cutoff_epoch`` (epoch seconds).

    Primary signal is the numeric ``created_at_ts`` (what the Upstash filter uses).
    Falls back to deriving epoch from the ISO ``created_at`` string for any doc not
    yet carrying ``created_at_ts`` (e.g. ingested before the field was added /
    pending backfill). A doc with neither is treated as NOT recent — recency can't
    be confirmed, so it's excluded."""
    content = result.get("content", {}) or {}
    meta = result.get("metadata", {}) or {}
    ts = content.get("created_at_ts")
    if ts is None:
        ts = meta.get("created_at_ts")
    if ts is None:
        ts = iso_to_epoch(content.get("created_at") or meta.get("created_at"))
    try:
        return ts is not None and int(ts) >= cutoff_epoch
    except (TypeError, ValueError):
        return False


def _is_non_purchasable(result: Dict[str, Any]) -> bool:
    """True when a search result is a zero-price item (e.g. a giveaway freebie)
    that must be hidden from customer-facing product search and recommendations.
    Without this guard a query like "cheapest product" returns a ₹0 freebie."""
    content = result.get("content", {}) or {}

    # Zero / missing price → not a real purchasable product.
    try:
        price_min = float(content.get("price_min") or 0)
    except (TypeError, ValueError):
        price_min = 0.0
    return price_min <= 0


def _exclude_shown_products(
    results: List[Dict[str, Any]], exclude: set
) -> tuple[List[Dict[str, Any]], bool]:
    """Drop products whose handle is in ``exclude`` (already shown to the user).

    When *every* result has already been shown, returning nothing yields a
    false "that's all we have" reply. Instead of returning zero products we
    fall back to the full (pre-exclusion) result set and signal it with the
    returned ``repeated`` flag, so the response LLM can tell the user these are
    being shown again rather than claiming nothing was found.

    Returns ``(results, repeated)`` where ``repeated`` is True only when the
    fallback kicked in (all results were already shown).
    """
    if not exclude:
        return results, False
    filtered = [
        r for r in results
        if r.get("metadata", {}).get("handle", "") not in exclude
    ]
    if not filtered and results:
        return results, True
    return filtered, False


# ---------------------------------------------------------------------------
# Shared search pipeline (QU -> Search -> Rerank) - used by all callers
# ---------------------------------------------------------------------------

@dataclass
class SearchPipelineResult:
    """Result returned by ``search_products_pipeline``."""
    products: List[Dict[str, Any]] = field(default_factory=list)
    formatted_text: str = ""
    qu_query: str = ""
    qu_filter: str = ""
    follow_up: Optional[str] = None
    # True when QU resolved this to a bestseller/trending request (the search
    # was scoped by a ``bestseller = true`` filter). Exposed as a typed signal
    # so callers gate on it instead of substring-sniffing ``qu_filter``.
    is_bestseller: bool = False
    # QU's classification of whether this is a catalog-wide trending/bestseller
    # request (True/False from the QU LLM; defaults False when unclassified).
    is_catalog_wide: bool = False
    results_before_rerank: int = 0
    results_after_rerank: int = 0
    duration_ms: int = 0
    filters_relaxed: bool = False
    # True when every match had already been shown, so previously-shown products
    # are repeated instead of returning zero results (see _exclude_shown_products).
    all_products_repeated: bool = False


async def search_products_pipeline(
    query: str,
    client_id: str,
    *,
    conversation_history: Optional[List[Dict]] = None,
    user_profile: Optional[Dict[str, Any]] = None,
    exclude_handle: Optional[str] = None,
    exclude_handles: Optional[List[str]] = None,
    max_results: int = 8,
    product_context: Optional[Dict[str, Any]] = None,
    collection_context: Optional[str] = None,
) -> SearchPipelineResult:
    """Run the shared search pipeline: QU -> Upstash Search -> Rerank.

    This is the single source of truth for semantic product search.  All
    callers (tool_factory, tool_registry, demo_chat_backend,
    RecommendationService) should use this instead of duplicating the logic.

    Args:
        query: Natural-language search query (already cleaned by caller).
        client_id: Tenant / client ID.
        conversation_history: Recent messages for multi-turn context.
        user_profile: Dict with optional keys ``segment``, ``size``,
            ``budget_max``.
        exclude_handle: Product handle to exclude from results (e.g. the
            product the customer is currently viewing).
        exclude_handles: List of product handles to exclude (e.g. products
            already shown to the user in previous turns).
        max_results: How many products to return after reranking.
        product_context: Focal product attributes (name, subcategory,
            color, style, price) for "similar" / "matching" queries.
        collection_context: Collection handle of the page the customer is
            browsing (e.g. "denim-jeans"). Generic queries like "show me the
            best ones" are scoped to this collection's product type instead of
            the whole catalog.

    Returns:
        ``SearchPipelineResult`` with reranked products and formatted text.
    """
    start = time.time()
    user_profile = user_profile or {}

    # Collection handle ("denim-jeans") → search hint ("denim jeans").
    from fashion_bot.services.recommendation.query_understanding import (
        collection_handle_to_search_hint,
    )
    collection_hint = collection_handle_to_search_hint(collection_context)

    # --- Step 1: Query Understanding (LLM) ---
    # Resolve the per-client threshold only when an explicit Postgres row exists.
    # Passing None leaves the hardcoded value in the QU prompt unchanged;
    # passing an int replaces it so the LLM uses the client-configured threshold.
    from fashion_bot.config_manager import aget_config
    _raw_avail = await aget_config(VARIANT_AVAILABILITY_MIN_PCT_CONFIG_KEY, client_id=client_id)
    variant_avail_pct_override: Optional[int] = None
    if _raw_avail is not None and str(_raw_avail).strip():
        try:
            variant_avail_pct_override = max(
                VARIANT_AVAILABILITY_MIN_PCT_MIN,
                min(int(str(_raw_avail).strip()), VARIANT_AVAILABILITY_MIN_PCT_MAX),
            )
        except (TypeError, ValueError):
            logger.warning(
                "[search_pipeline] client=%s has non-integer %s config; leaving prompt default",
                client_id, VARIANT_AVAILABILITY_MIN_PCT_CONFIG_KEY,
            )
    qu_result: QueryUnderstandingResult = await understand_query(
        user_query=query,
        client_id=client_id,
        conversation_history=conversation_history,
        user_profile=user_profile,
        product_context=product_context,
        collection_context=collection_hint or None,
        variant_availability_min_pct=variant_avail_pct_override,
    )
    logger.info(
        f"[search_pipeline] QU: input='{query}' query='{qu_result.query}' "
        f"filter='{qu_result.filter}' follow_up='{qu_result.follow_up}' "
        f"sort_by='{qu_result.sort_by}'"
    )

    # Deterministic scoping safeguard: when browsing a collection, make sure the
    # search query actually names that collection's product type. The QU LLM is
    # nudged to do this, but a generic query like "best ones" can still slip
    # through as a catalog-wide search — so if none of the collection's terms
    # made it into the rewritten query, prepend them. Skipped when the customer
    # already named the type (avoids duplicating "jeans jeans") or for
    # "similar"/"matching" queries that carry their own product_context.
    if collection_hint and not product_context:
        qu_lower = (qu_result.query or "").lower()
        if not any(tok in qu_lower for tok in collection_hint.split()):
            qu_result.query = f"{collection_hint} {qu_result.query}".strip()
            logger.info(
                f"[search_pipeline] Scoped query to collection "
                f"'{collection_context}' → '{qu_result.query}'"
            )

    # --- Step 2: Upstash Search retrieval (with fallbacks) ---
    search_service = get_upstash_search_service()
    has_bestseller_filter = bool(qu_result.filter and "bestseller" in qu_result.filter)
    # Newness is driven solely by the QU LLM's structured classification
    # (sort_by == "newest"), not a hardcoded keyword regex — the LLM handles
    # phrasing/synonyms across languages far better than a fixed word list.
    sort_by_newest = qu_result.sort_by == "newest"
    # How many docs to FETCH from Upstash — per-client configurable (a bigger
    # pool gives reranking room): new-arrivals and bestseller queries each get
    # their own limit; plain searches use the default. Clamped + fail-open.
    if sort_by_newest:
        search_limit = await aresolve_client_int(
            client_id, NEW_ARRIVAL_FETCH_LIMIT_CONFIG_KEY, NEW_ARRIVAL_FETCH_LIMIT_DEFAULT,
            min_value=PRODUCT_FETCH_LIMIT_MIN, max_value=PRODUCT_FETCH_LIMIT_MAX,
        )
    elif has_bestseller_filter:
        search_limit = await aresolve_client_int(
            client_id, BESTSELLER_FETCH_LIMIT_CONFIG_KEY, BESTSELLER_FETCH_LIMIT_DEFAULT,
            min_value=PRODUCT_FETCH_LIMIT_MIN, max_value=PRODUCT_FETCH_LIMIT_MAX,
        )
    else:
        search_limit = PRODUCT_SEARCH_FETCH_LIMIT_DEFAULT

    # "Show more" fetch widening. Already-shown handles are subtracted from this
    # pool *after* retrieval (the exclusion block further below) and *before* the
    # final rerank truncation to max_results. With a fixed fetch and a blocklist
    # that accumulates across a session, that subtraction can empty the pool —
    # surfacing only 1-2 items and a false "that's all we have" reply. Widen the
    # fetch by the number of excluded handles (+ max_results headroom for the
    # freebie / recency drops that also run before exclusion) so a full page of
    # net-new products survives. Clamped to the shared ceiling; the caller bounds
    # the blocklist, so in practice this stays well under PRODUCT_FETCH_LIMIT_MAX.
    # Guarded so non-"show more" searches (no exclusions) are completely unchanged.
    _exclude_count = len(exclude_handles or ()) + (1 if exclude_handle else 0)
    if _exclude_count:
        search_limit = min(
            PRODUCT_FETCH_LIMIT_MAX,
            search_limit + _exclude_count + max_results,
        )

    relaxation_threshold = await aresolve_client_int(
        client_id, FILTER_RELAXATION_THRESHOLD_CONFIG_KEY,
        FILTER_RELAXATION_THRESHOLD_DEFAULT,
        min_value=FILTER_RELAXATION_THRESHOLD_MIN,
        max_value=FILTER_RELAXATION_THRESHOLD_MAX,
    )
    _search_kwargs = dict(
        client_id=client_id, limit=search_limit,
        semantic_weight=0.1 if sort_by_newest else 0.75,
        reranking=not sort_by_newest,
    )

    # New-arrivals queries get a hard recency bound on the Upstash filter so only
    # products created within the resolved window (user timeframe > client config
    # > default) are returned.
    # Upstash range operators (>=) apply to NUMBERS, not strings, so we filter on
    # the numeric epoch mirror `created_at_ts` (added in to_search_document) — the
    # ISO `created_at` string can't be range-filtered. This clause is preserved
    # through every filter-relaxation fallback below — we never widen "new" back
    # to the full catalog. Category scoping (the collection_hint prepended to the
    # query above) is orthogonal and also preserved, so "new arrivals" on a
    # collection page stays on-category.
    recency_clause = None
    recency_cutoff_epoch = None
    if sort_by_newest:
        # Resolve the window: explicit user timeframe > per-client config > default.
        window_days = await _aresolve_new_arrival_window_days(
            client_id, qu_result.recency_days
        )
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=window_days)
        recency_cutoff_epoch = int(cutoff_dt.timestamp())
        recency_clause = f"created_at_ts >= {recency_cutoff_epoch}"
        base_filter = (qu_result.filter or "in_stock = true").strip()
        if "created_at_ts" not in base_filter:
            qu_result.filter = f"{base_filter} AND {recency_clause}"
        logger.info(
            f"[search_pipeline] New-arrivals recency bound applied: window="
            f"{window_days}d (source="
            f"{'user' if qu_result.recency_days is not None else 'client/default'}) "
            f"created_at_ts >= {recency_cutoff_epoch} "
            f"({cutoff_dt.strftime('%Y-%m-%d')}) filter='{qu_result.filter}'"
        )

    results = await search_service.asearch(
        query=qu_result.query, filter_str=qu_result.filter, **_search_kwargs,
    )

    filters_relaxed = False

    # --- Progressive filter relaxation ---
    # Instead of dropping filters entirely, move them into the semantic query
    # text so they still influence ranking without hard-excluding documents.
    # Process in order: variant_availability_pct -> category -> subcategory ->
    # segment, stopping as soon as results meet the threshold.
    # variant_availability_pct is dropped first (not moved to query) since it's
    # a numeric threshold, not a semantic term. Only positive `=` filters are
    # moved; NOT IN exclusions, price, in_stock, bestseller, and recency are
    # never relaxed here.
    _RELAXATION_FIELDS = [
        ("variant_availability_pct", re.compile(r"\bvariant_availability_pct\s*>\s*\d+\s*(AND\s*)?", re.IGNORECASE)),
        ("category", re.compile(r"\bcategory\s*=\s*'([^'\\]*(?:\\.[^'\\]*)*)'\s*(AND\s*)?", re.IGNORECASE)),
        ("subcategory", re.compile(r"\bsubcategory\s*=\s*'([^'\\]*(?:\\.[^'\\]*)*)'\s*(AND\s*)?", re.IGNORECASE)),
        ("segment", re.compile(r"\bsegment\s*=\s*'([^'\\]*(?:\\.[^'\\]*)*)'\s*(AND\s*)?", re.IGNORECASE)),
    ]
    if (
        len(results) < relaxation_threshold
        and qu_result.filter
        and not has_bestseller_filter
    ):
        _relaxed_filter = qu_result.filter
        _relaxed_query = qu_result.query
        for field_name, pattern in _RELAXATION_FIELDS:
            match = pattern.search(_relaxed_filter)
            if not match:
                continue
            _relaxed_filter = pattern.sub("", _relaxed_filter).strip()
            if _relaxed_filter.startswith("AND "):
                _relaxed_filter = _relaxed_filter[4:].strip()
            if _relaxed_filter.endswith(" AND"):
                _relaxed_filter = _relaxed_filter[:-4].strip()
            _relaxed_filter = _relaxed_filter or "in_stock = true"
            # Numeric filters (variant_availability_pct) are simply dropped;
            # text filters (category/segment) are moved into the query so they
            # still influence semantic ranking.
            extracted_value = match.group(1) if match.lastindex else None
            if extracted_value:
                _relaxed_query = f"{extracted_value} {_relaxed_query}"
            relaxed_results = await search_service.asearch(
                query=_relaxed_query, filter_str=_relaxed_filter, **_search_kwargs,
            )
            if len(relaxed_results) > len(results):
                logger.info(
                    f"[search_pipeline] Relaxed '{field_name}' filter "
                    f"({'dropped' if not extracted_value else f'moved {chr(39)}{extracted_value}{chr(39)} to query'}): "
                    f"{len(results)} → {len(relaxed_results)} results"
                )
                results = relaxed_results
                filters_relaxed = True
            if len(results) >= relaxation_threshold:
                break

    # Last-resort relaxations keep the recency bound for new-arrivals queries:
    # a stale catalog should return fewer (or no) results rather than old ones.
    _instock_fallback = "in_stock = true"
    if recency_clause:
        _instock_fallback = f"{_instock_fallback} AND {recency_clause}"
    if not results and qu_result.filter and qu_result.filter != _instock_fallback:
        results = await search_service.asearch(
            query=qu_result.query, filter_str=_instock_fallback, **_search_kwargs,
        )
        if results:
            filters_relaxed = True

    if not results:
        # For new-arrivals, drop every filter EXCEPT the recency bound; otherwise
        # drop all filters entirely.
        last_resort_filter = recency_clause if recency_clause else None
        results = await search_service.asearch(
            query=qu_result.query, filter_str=last_resort_filter, **_search_kwargs,
        )
        if results:
            filters_relaxed = True

    # --- Bestseller fallback: live Shopify Orders scan ---
    # When a bestseller query returns 0 results from the index (e.g. a newly
    # onboarded store without pre-tagged bestsellers), fall back to the live
    # Shopify Orders analysis. Lazy-imported to avoid circular deps.
    if not results and has_bestseller_filter:
        try:
            from fashion_bot.core.orchestrator import ProductOrchestrator
            fallback = await ProductOrchestrator.aget_top_selling(
                top_n=max_results, state={"client_id": client_id},
            )
            fallback_products = fallback.get("products") or []
            if fallback_products:
                results = [
                    {"content": p, "metadata": p, "score": 1.0}
                    for p in fallback_products
                ]
                filters_relaxed = True
                logger.info(
                    f"[search_pipeline] Bestseller fallback (live orders scan): "
                    f"{len(results)} products"
                )
        except Exception as e:
            logger.warning(
                f"[search_pipeline] Bestseller live-orders fallback failed: {e}"
            )

    # --- Filter out zero-price / freebie items (not customer-purchasable) ---
    before_freebie = len(results)
    results = [r for r in results if not _is_non_purchasable(r)]
    freebies_removed = before_freebie - len(results)
    if freebies_removed:
        logger.info(
            f"[search_pipeline] Filtered {freebies_removed} zero-price/freebie "
            f"product(s) from results"
        )

    # --- Enforce the recency bound in code too (new-arrivals only) ---
    # Belt-and-suspenders for the Upstash filter: guards against docs missing
    # created_at_ts (e.g. pending backfill) that slipped through the filter.
    if sort_by_newest and recency_cutoff_epoch is not None:
        before_recency = len(results)
        results = [r for r in results if _created_after_epoch(r, recency_cutoff_epoch)]
        recency_removed = before_recency - len(results)
        if recency_removed:
            logger.info(
                f"[search_pipeline] Recency net dropped {recency_removed} product(s) "
                f"created before epoch {recency_cutoff_epoch}"
            )

    results_before = len(results)

    # --- Exclude previously-shown and current-page products ---
    _all_excludes: set = set()
    if exclude_handle:
        _all_excludes.add(exclude_handle)
    if exclude_handles:
        _all_excludes.update(exclude_handles)
    before = len(results)
    results, all_products_repeated = _exclude_shown_products(results, _all_excludes)
    excluded_count = before - len(results)
    if excluded_count:
        logger.info(
            f"[search_pipeline] Excluded {excluded_count} previously-shown "
            f"product(s) ({len(_all_excludes)} handles in blocklist)"
        )
    if all_products_repeated:
        logger.info(
            "[search_pipeline] All matches already shown — repeating products "
            "instead of returning zero (no net-new results)"
        )

    # --- Step 3: Business-rules reranking ---
    occasions = re.findall(
        r"occasion\s+CONTAINS\s+'([^']+)'", qu_result.filter or "", re.IGNORECASE,
    )
    # Sort intent comes solely from the QU LLM's structured `sort_by` (a single
    # value), not hardcoded keyword regexes. "price_low" (cheapest → lowest
    # actual price first) and "discount" (biggest markdown) are distinct: the
    # prompt tells the LLM to pick "price_low" for "cheapest" so freebies
    # (discount_pct=100) never win a cheapest query.
    sort_by_price_low = qu_result.sort_by == "price_low"
    sort_by_price_high = qu_result.sort_by == "price_high"
    sort_by_discount = qu_result.sort_by == "discount"
    sort_by_conversion = qu_result.sort_by == "best_converting"
    if sort_by_price_low:
        logger.info("[search_pipeline] Cheapest/price-low query detected — sorting by price_min ascending")
    if sort_by_price_high:
        logger.info("[search_pipeline] Price-high query detected — sorting by price_min descending")
    if sort_by_discount:
        logger.info("[search_pipeline] Discount-oriented query detected — sorting by discount_pct")
    if sort_by_newest:
        logger.info("[search_pipeline] Newness-oriented query detected — sorting by created_at")
    if sort_by_conversion:
        logger.info("[search_pipeline] Best-converting query detected — sorting by conversion_rate")
    reranked = rerank(
        results,
        user_context={
            "occasions": occasions,
            "size": user_profile.get("size"),
            "budget_max": user_profile.get("budget_max"),
        },
        max_results=max_results,
        sort_by_discount=sort_by_discount,
        sort_by_newest=sort_by_newest,
        sort_by_price_low=sort_by_price_low,
        sort_by_price_high=sort_by_price_high,
        sort_by_conversion=sort_by_conversion,
    )

    # --- Replace internal Shopify URLs with real website URLs ---
    from fashion_bot.utils.product_utils import (
        aget_shopify_to_website_mapping,
        replace_shopify_url,
    )
    website_url = await aget_shopify_to_website_mapping(client_id)

    # --- Format text ---
    lines: List[str] = []
    if reranked:
        lines.append(f"Found {len(reranked)} matching products:\n")
        for i, p in enumerate(reranked, 1):
            content = p.get("content", {})
            metadata = p.get("metadata", {})
            url = metadata.get("product_url", "")
            if website_url and url:
                url = replace_shopify_url(url, website_url)
                metadata["product_url"] = url
            sizes_str = ", ".join(content.get("sizes", [])[:6])
            colors_str = ", ".join(content.get("colors", [])[:4])

            price_str = f"Rs.{content.get('price_min', '?')}"
            compare_at = content.get("compare_at_price_min")
            dpct = content.get("discount_pct", 0)
            if compare_at and dpct:
                price_str += f" (MRP Rs.{compare_at}, {dpct}% OFF)"
            line = f"{i}. {content.get('title', 'Unknown')} -- {price_str}"
            if url:
                line += f"\n   URL: {url}"
            if sizes_str:
                line += f"\n   Sizes: {sizes_str}"
            if colors_str:
                line += f"\n   Colors: {colors_str}"
            lines.append(line)

    formatted = "\n".join(lines)
    if all_products_repeated:
        formatted += (
            "\n\nNote: No new products were found beyond those already shown. "
            "The products above are being shown again."
        )
    if qu_result.follow_up:
        formatted += f"\n\nSuggested follow-up: {qu_result.follow_up}"

    _final_handles = [
        (p.get("metadata") or {}).get("handle")
        or (p.get("content") or {}).get("handle")
        or "?"
        for p in reranked
    ]
    logger.info(
        f"[search_pipeline] final {len(reranked)} product(s) after filtering "
        f"(before_rerank={results_before}) → handles given to tool/LLM={_final_handles}"
    )

    return SearchPipelineResult(
        products=reranked,
        formatted_text=formatted,
        qu_query=qu_result.query,
        qu_filter=qu_result.filter or "",
        follow_up=qu_result.follow_up,
        is_bestseller=has_bestseller_filter,
        is_catalog_wide=bool(qu_result.is_catalog_wide),
        results_before_rerank=results_before,
        results_after_rerank=len(reranked),
        duration_ms=int((time.time() - start) * 1000),
        filters_relaxed=filters_relaxed,
        all_products_repeated=all_products_repeated,
    )


# ---------------------------------------------------------------------------
# RecommendationService - adds response generation (LLM #2) on top
# ---------------------------------------------------------------------------

class RecommendationService:
    """
    Full recommendation pipeline: search_products_pipeline + LLM response
    generation.  Use ``search_products_pipeline`` directly when you already
    have your own LLM for formatting the response.
    """

    async def recommend(
        self,
        client_id: str,
        user_query: str,
        session_id: Optional[str] = None,
        user_profile: Optional[Dict[str, Any]] = None,
        conversation_history: Optional[List[Dict]] = None,
        filterable_fields: Optional[List[Dict]] = None,
        exclude_handle: Optional[str] = None,
        product_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run the full recommendation pipeline (search + response generation)."""
        start_time = time.time()
        user_profile = user_profile or {}

        pipeline = await search_products_pipeline(
            query=user_query,
            client_id=client_id,
            conversation_history=conversation_history,
            user_profile=user_profile,
            exclude_handle=exclude_handle,
            max_results=8,
            product_context=product_context,
        )

        if not pipeline.products and pipeline.follow_up:
            return {
                "session_id": session_id,
                "clarifying_question": pipeline.follow_up,
                "recommendations": [],
                "response_text": pipeline.follow_up,
                "filters_applied": {},
                "search_config": {},
                "duration_ms": int((time.time() - start_time) * 1000),
            }

        filters_applied = {
            "raw_filter": pipeline.qu_filter,
            "query": pipeline.qu_query,
        }

        gen_result = await generate_response(
            products=pipeline.products,
            user_query=user_query,
            filters_applied=filters_applied,
            clarifying_question=pipeline.follow_up,
        )

        duration_ms = int((time.time() - start_time) * 1000)

        products_with_reasons = gen_result.get("products", [])
        for p in products_with_reasons:
            p["reasons"] = p.get("reasons", [])

        return {
            "session_id": session_id,
            "clarifying_question": gen_result.get("clarifying_question"),
            "recommendations": products_with_reasons,
            "response_text": gen_result.get("text", ""),
            "filters_applied": filters_applied,
            "search_config": {
                "semantic_weight": 0.75,
                "reranking": True,
                "store_used": "upstash_search",
                "results_before_rerank": pipeline.results_before_rerank,
                "results_after_rerank": pipeline.results_after_rerank,
            },
            "duration_ms": duration_ms,
        }
