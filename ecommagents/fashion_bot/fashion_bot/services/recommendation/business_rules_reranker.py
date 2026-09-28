"""
Business Rules Reranker — deterministic post-retrieval reranking.

Applies domain-specific boosts and penalties on top of Upstash Search's
AI reranking scores. No LLM calls — pure application logic.
"""

import logging
from typing import List, Dict, Any, Optional, Set

logger = logging.getLogger(__name__)

PARTY_MATERIALS = {"satin", "sequin", "velvet", "faux leather", "silk", "chiffon", "organza"}
PARTY_COLORS = {"black", "metallic", "gold", "silver", "burgundy", "navy", "red"}
CASUAL_MATERIALS = {"cotton", "linen", "jersey", "denim"}
FORMAL_MATERIALS = {"wool", "tweed", "silk", "satin", "crepe"}

# Availability demotion floor: a product with 0% variants in stock keeps this
# fraction of its score; a fully-stocked product keeps 100%. Graduated in between
# so products with most sizes available rank above near-out-of-stock ones. The
# field is the QU hard filter's backstop — pre-backfill docs that lack it are
# treated as fully available (neutral) so existing behaviour never regresses.
_AVAILABILITY_FLOOR = 0.6


def _availability_pct(content: Dict[str, Any]) -> float:
    """variant_availability_pct (0-100) from a result's content; 100 when absent."""
    try:
        return float(content.get("variant_availability_pct", 100))
    except (TypeError, ValueError):
        return 100.0


def rerank(
    results: List[Dict[str, Any]],
    user_context: Optional[Dict[str, Any]] = None,
    max_results: int = 12,
    sort_by_discount: bool = False,
    sort_by_newest: bool = False,
    sort_by_price_low: bool = False,
    sort_by_price_high: bool = False,
    sort_by_conversion: bool = False,
) -> List[Dict[str, Any]]:
    """
    Apply business rules to rerank search results.

    Args:
        results: List of search result dicts from Upstash Search
        user_context: Dict with occasion, size, budget, etc. from query understanding
        max_results: Max results to return
        sort_by_discount: When True, sort primarily by discount_pct descending
        sort_by_newest: When True, sort primarily by created_at descending
        sort_by_price_low: When True, sort by price_min ascending (cheapest first)
        sort_by_price_high: When True, sort by price_min descending (priciest first)
        sort_by_conversion: When True, sort primarily by conversion_rate descending
            (best-converting products first). Items without a conversion_rate
            (no traffic data yet) sort last.

    Returns:
        Reranked list of results
    """
    if not results:
        return []

    user_context = user_context or {}
    occasions = set(user_context.get("occasions", []))
    user_size = user_context.get("size")
    budget_max = user_context.get("budget_max")

    scored: List[tuple] = []
    seen_brands: Set[str] = set()
    seen_color_families: Set[str] = set()

    for r in results:
        content = r.get("content", {})
        score = r.get("score", 0.5)

        # Soft penalty: size availability (don't hard-filter — different products
        # use different size systems: letter S/M/L/XL vs numeric 28-40)
        if user_size:
            available_sizes = content.get("sizes", [])
            if available_sizes and user_size not in available_sizes:
                score *= 0.6

        # Hard filter: budget
        if budget_max:
            price = content.get("price_max") or content.get("price_min", 0)
            if price and price > budget_max:
                continue

        # Occasion-aware boosts
        if "party" in occasions:
            mat = (content.get("material") or "").lower()
            if mat in PARTY_MATERIALS:
                score *= 1.3
            cf = (content.get("color_family") or "").lower()
            if cf in PARTY_COLORS:
                score *= 1.15

        if "formal" in occasions or "work" in occasions:
            mat = (content.get("material") or "").lower()
            if mat in FORMAL_MATERIALS:
                score *= 1.2

        if "casual" in occasions:
            mat = (content.get("material") or "").lower()
            if mat in CASUAL_MATERIALS:
                score *= 1.1

        # Diversity: penalize repeated brands
        brand = (content.get("brand") or "").lower()
        if brand and brand in seen_brands:
            score *= 0.85
        seen_brands.add(brand)

        # Diversity: penalize repeated color families
        cf = (content.get("color_family") or "").lower()
        if cf and cf in seen_color_families:
            score *= 0.9
        seen_color_families.add(cf)

        # Discount boost (products on sale)
        compare_price = content.get("compare_at_price_min")
        price_min = content.get("price_min", 0)
        if compare_price and price_min and compare_price > price_min:
            discount_pct = (compare_price - price_min) / compare_price
            if discount_pct > 0.2:
                score *= 1.1

        # Availability: demote products with few in-stock variants so the ones
        # with most sizes available surface first (backstop to the QU filter).
        score *= _AVAILABILITY_FLOOR + (1 - _AVAILABILITY_FLOOR) * (_availability_pct(content) / 100.0)

        scored.append((r, score))

    if sort_by_newest:
        for r, s in scored[:5]:
            meta_ca = r.get("metadata", {}).get("created_at")
            cont_ca = r.get("content", {}).get("created_at")
            ca = meta_ca or cont_ca or ""
            title = r.get("content", {}).get("title", "?")
            logger.info(
                f"[rerank] pre-sort — title='{title}' meta_created_at={meta_ca!r} "
                f"content_created_at={cont_ca!r} effective='{ca}' score={s:.3f}"
            )
        def _get_created_at(item):
            r, s = item
            ca = r.get("metadata", {}).get("created_at") or r.get("content", {}).get("created_at") or ""
            return (ca, s)

        scored.sort(key=_get_created_at, reverse=True)
        for r, s in scored[:5]:
            ca = r.get("metadata", {}).get("created_at") or r.get("content", {}).get("created_at") or ""
            title = r.get("content", {}).get("title", "?")
            logger.info(f"[rerank] post-sort — title='{title}' created_at='{ca}'")
    elif sort_by_price_low or sort_by_price_high:
        def _price(item):
            try:
                return float(item[0].get("content", {}).get("price_min") or 0)
            except (TypeError, ValueError):
                return 0.0

        # Only positively-priced items participate in price sorting; zero/missing
        # price items (e.g. freebies) are pushed to the end, never ranked first.
        priced = [it for it in scored if _price(it) > 0]
        unpriced = [it for it in scored if _price(it) <= 0]
        priced.sort(key=_price, reverse=sort_by_price_high)
        scored = priced + unpriced
    elif sort_by_discount:
        # Bucket the discount to the nearest 10% so availability breaks near-ties:
        # among comparably-discounted items, the better-stocked one leads (fixes
        # "biggest discount but only 1 size in stock on top").
        def _discount_key(item):
            content = item[0].get("content", {})
            try:
                bucket = round(float(content.get("discount_pct", 0) or 0) / 10.0)
            except (TypeError, ValueError):
                bucket = 0
            return (bucket, _availability_pct(content), item[1])

        scored.sort(key=_discount_key, reverse=True)
    elif sort_by_conversion:
        # Best-converting first (units_sold / unique_sessions, written by the
        # monthly bestseller_refresh cron). Items with no conversion_rate yet
        # (no traffic data) get -1 so they always sort last; availability and the
        # base relevance score break ties among equally-converting items.
        def _conversion_key(item):
            content = item[0].get("content", {})
            raw = content.get("conversion_rate")
            try:
                conv = float(raw) if raw is not None else -1.0
            except (TypeError, ValueError):
                conv = -1.0
            return (conv, _availability_pct(content), item[1])

        scored.sort(key=_conversion_key, reverse=True)
    else:
        scored.sort(key=lambda x: x[1], reverse=True)
    return [r for r, _ in scored[:max_results]]
