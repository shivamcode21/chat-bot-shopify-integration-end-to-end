"""
Product Utilities Module

Contains vendor-agnostic helper functions for product operations.
"""

import json
import logging
import re
from difflib import SequenceMatcher, get_close_matches
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse

from fashion_bot.rollbar_config import report_error

logger = logging.getLogger(__name__)

_STRIP_KEYS = frozenset({
    "client_id",
    "content_hash",
    "content_hash_no_inventory",
    "product_id",
    "seo_title",
    "seo_description",
    "product_line_normalized",
    "base_product_name",
    "all_images",
    "size_guide",
    "image_texts",
    "image_ocr_hash",
    "created_at_ts",
    "variant_availability_pct",
})


def extract_product_rating(product: Dict) -> Optional[Dict]:
    """Pull {rating, rating_count} off a product's metafield_attributes.

    Shared by the web-widget card (``format_product_for_carousel`` in
    websocket_chat.py) and the LLM-facing tool result (``_normalize_product``
    in tool_factory.py) -- one copy per AGENTS.md "Shared Utilities Over
    Duplication" rather than duplicated per surface, so a future fix to this
    logic can't drift between the two call sites.

    Judge.me writes these as generic Shopify metafields, which the existing
    product-ingestion pipeline already captures business-agnostically:
    ``rating`` is a JSON string
    (``{"scale_min":"1.0","scale_max":"5.0","value":"5.0"}``), ``rating_count``
    a plain string int (occasionally with a thousands separator, or a
    Shopify-typed decimal like ``"128.0"``). No reviews yet -> no metafield at
    all, the expected/common case, not an error.

    ``value`` is normalized onto a 1-5 scale using the JSON's own
    scale_min/scale_max (identity for the common Judge.me 1-5 case) rather
    than assumed to already be out of 5 -- a reviews app on a different scale
    (e.g. out of 10) would otherwise both overfill the card's star row and
    have the LLM state a "X out of 5" that isn't true.

    Defensive and fail-open: any missing/malformed value returns None so the
    caller renders/reports exactly as it did before rating support existed.
    """
    try:
        mf = product.get("metafield_attributes")
        if not isinstance(mf, dict):
            return None

        raw_rating = mf.get("rating")
        raw_count = mf.get("rating_count")
        if not raw_rating or raw_count is None:
            return None

        try:
            rating_count = int(raw_count)
        except (TypeError, ValueError):
            # Shopify metafield typed as a decimal ("128.0"), or a thousands
            # separator ("1,234") -- both are valid ints, just not directly
            # int()-able. Falls through to the outer except on anything else.
            rating_count = int(float(str(raw_count).replace(",", "")))
        if rating_count <= 0:
            return None

        rating_data = json.loads(raw_rating) if isinstance(raw_rating, str) else raw_rating
        if isinstance(rating_data, dict):
            value = float(rating_data.get("value"))
            scale_min = float(rating_data.get("scale_min", 1.0))
            scale_max = float(rating_data.get("scale_max", 5.0))
        else:
            value, scale_min, scale_max = float(rating_data), 1.0, 5.0

        if scale_max > scale_min:
            rating = 1.0 + (value - scale_min) * 4.0 / (scale_max - scale_min)
        else:
            # Degenerate/malformed scale -- use the raw value rather than
            # divide by zero; still clamped below.
            rating = value
        rating = round(min(max(rating, 1.0), 5.0), 1)
        if rating <= 0:
            return None

        return {"rating": rating, "rating_count": rating_count}
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
        return None


def _clean_product(product: Dict) -> Dict:
    """Strip internal-only keys and empty values from a product dict."""
    return {
        k: v
        for k, v in product.items()
        if k not in _STRIP_KEYS and v is not None and v != "" and v != [] and v != {}
    }


def format_products_for_llm(products: List[Dict], follow_up: Optional[str] = None) -> str:
    """Serialize product dicts as JSON for the LLM ToolMessage.

    Strips internal-only fields and empty values to keep tokens reasonable
    while giving the LLM full access to all product attributes (variants,
    care instructions, size chart, etc.).
    """
    cleaned = [_clean_product(p) for p in products]
    payload: Dict = {"products": cleaned}
    if follow_up:
        payload["follow_up"] = follow_up
    return json.dumps(payload, default=str)


def format_product_tool_result(raw) -> Optional[Tuple[str, Dict]]:
    """Format a product/search tool result for the model: returns ``(content, artifact)``.

    ``content`` is the cleaned, model-facing text (internal fields stripped, products
    serialized via :func:`format_products_for_llm`, with optional summary and the
    tool's own ``note`` carried through). ``artifact`` is the raw dict that downstream
    carousel extraction / ``messages_to_intermediate_steps`` reads.

    Returns ``None`` when ``raw`` is not a product/text result dict, so callers leave
    the original message untouched.

    Note: we pass the tool's ``note`` (e.g. the "products already shown again" hint set
    by ``search_products``) straight through to the model and let it phrase the reply —
    no code-side prefixing or forced opening lines.

    Single source of truth shared by ``ProductObservationFormatterMiddleware`` (the
    live tool-execution path) and ``SearchGroundingMiddleware`` (the injected grounding
    path) — keep both in sync by editing only here.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("products"):
        formatted = format_products_for_llm(raw["products"], raw.get("follow_up"))
        summary = raw.get("formatted_text") or raw.get("summary")
        if summary:
            formatted = f"{summary}\n\n{formatted}"
        note = raw.get("note")
        if note:  # tool-supplied hint (e.g. all_products_repeated) — model decides wording
            formatted = f"{note}\n\n{formatted}"
        return formatted, raw
    if "text" in raw:  # native loop unwrapped {"text": ...}
        return raw["text"], raw
    return None


# ── Near-miss handle recovery ────────────────────────────────────────────────
# The LLM copies a product handle into its card-selection block by hand. Handles
# are long slugs (one production handle is 165 chars), and the model reliably
# mangles them in one of three ways, each observed in production:
#
#   dropped word      iconic-2502-...      <- iconic-men-2502-...
#   hyphen normalised ...red-polo-t-shirt  <- ...red-polo-tshirt      (x8 in 30d)
#   appended word     ...multicolor-top    <- ...multicolor
#
# In every traced case the product WAS in the turn's candidate set, so the card
# is recoverable without another network call. Matching is anchored on the SKU /
# style tokens (the segments containing a digit: polo4598, top1565, cw01, a077),
# NOT on overall string similarity: a 30-day replay of every real failure showed
# plain similarity picking a DIFFERENT colourway or variant in 2 of 19 cases,
# because a mangled handle can be textually closer to a sibling than to the
# product the customer asked about.
_SKU_TOKEN_RE = re.compile(r"\d")
_NEAR_MISS_MIN_RATIO = 0.75


def _handle_segments(handle: str) -> List[str]:
    return [s for s in (handle or "").lower().split("-") if s]


def _sku_tokens(handle: str) -> frozenset:
    """Segments carrying a digit — the SKU / style / season codes of a handle."""
    return frozenset(s for s in _handle_segments(handle) if _SKU_TOKEN_RE.search(s))


def find_near_miss_handle(emitted: str, candidate_handles: List[str]) -> Optional[str]:
    """Resolve a mangled *emitted* handle to one of *candidate_handles*, or None.

    Deliberately conservative — it returns None rather than guess, because
    showing the wrong product is worse than showing no card (the caller already
    has an Upstash fallback for the None case):

    * abstains entirely when *emitted* carries no SKU token, since there is then
      nothing reliable to anchor on (bare slugs like ``anti-acne-duo``);
    * requires the candidate's SKU tokens to match *exactly*;
    * uses the trailing segment (the colourway) only to separate siblings that
      share a SKU, and abstains if that still leaves more than one;
    * requires a final similarity floor so an unrelated product cannot slip in.

    Pure and stateless (AGENTS.md §2). Callers must try exact matching first.
    """
    e = (emitted or "").strip().lower()
    if not e:
        return None

    dehyphenated = {h.lower().replace("-", ""): h for h in candidate_handles if h}
    exact_ignoring_hyphens = dehyphenated.get(e.replace("-", ""))
    if exact_ignoring_hyphens:              # tshirt vs t-shirt — unambiguous
        return exact_ignoring_hyphens

    emitted_sku = _sku_tokens(e)
    if not emitted_sku:
        return None                          # nothing to anchor on — abstain

    pool = [h for h in candidate_handles if h and _sku_tokens(h) == emitted_sku]
    if len(pool) > 1:                        # same SKU, different colourways
        tail = _handle_segments(e)[-1]
        pool = [h for h in pool if tail in _handle_segments(h)]
    if len(pool) != 1:
        return None                          # absent or still ambiguous — abstain

    best = pool[0]
    ratio = SequenceMatcher(None, e, best.lower()).ratio()
    return best if ratio >= _NEAR_MISS_MIN_RATIO else None


def _title_match_score(title: str, reply_lower: str) -> float:
    """Fraction of title words found in the reply (order-independent)."""
    words = [w for w in title.lower().split() if len(w) > 2]
    if not words:
        return 0.0
    return sum(1 for w in words if w in reply_lower) / len(words)


def _count_listed_products(reply: str) -> int:
    """Count how many numbered products the LLM actually listed (e.g. '1.', '2.')."""
    numbers = re.findall(r'(?:^|\n)\s*(\d+)\.\s', reply)
    return max(int(n) for n in numbers) if numbers else 0


def match_products_to_reply(
    products: List[Dict], reply: str, max_results: int = 3
) -> List[Dict]:
    """Return only products whose title the LLM actually mentioned in its reply.

    Uses word-overlap scoring (>=60% of title words present in reply) so
    truncated or lightly paraphrased titles still match.  Falls back to the
    first *max_results* products (reranker order) when nothing scores high
    enough — e.g. when the LLM paraphrased heavily.

    Also caps results to the number of products the LLM actually listed
    (detected via numbered items like "1.", "2.") to avoid showing products
    the LLM intentionally excluded.
    """
    reply_lower = reply.lower()

    scored = [
        (_title_match_score(p.get("title", ""), reply_lower), p)
        for p in products
    ]
    scored.sort(key=lambda x: x[0], reverse=True)

    matched = [p for score, p in scored if score >= 0.6]
    selected = matched if matched else products

    listed_count = _count_listed_products(reply)
    cap = min(max_results, listed_count) if listed_count > 0 else max_results
    result = selected[:cap]

    if not result:
        titles = [p.get("title", "?") for p in products[:3]]
        report_error(
            "match_products_to_reply returned empty",
            level='warning',
            product_count=len(products),
            reply_len=len(reply),
            titles=titles,
        )
    return result


def normalize_size(size: str) -> str:
    """
    Normalize size input - map full size names to abbreviations.
    Handles exact matches, abbreviations, and fuzzy matching for spelling mistakes.
    
    Args:
        size: The size string to normalize (e.g., 'small', 'S', 'medium', '32')
        
    Returns:
        Normalized size string in uppercase
    """
    if not size:
        return ""

    size_mapping = {
        'small': 'S',
        'medium': 'M',
        'large': 'L',
        'extra large': 'XL',
        'extralarge': 'XL',
        'x-large': 'XL',
        'xlarge': 'XL',
        'extra small': 'XS',
        'extrasmall': 'XS',
        'x-small': 'XS',
        'xsmall': 'XS',
        'xxl': 'XXL',
        'extra extra large': 'XXL',
        'xxxl': 'XXXL'
    }
    
    size_lower = size.strip().lower()
    
    # First, try exact match
    if size_lower in size_mapping:
        return size_mapping[size_lower].upper()
    
    # Try fuzzy matching for spelling mistakes (e.g., "smal" → "small")
    close_matches = get_close_matches(size_lower, size_mapping.keys(), n=1, cutoff=0.7)
    
    if close_matches:
        matched_key = close_matches[0]
        return size_mapping[matched_key].upper()
    
    # No match found, use as-is (could be abbreviation like "S", "M", or numeric like "32")
    return size.strip().upper()


async def get_category_links_from_config(client_id: str = None) -> dict:
    """
    Fetch category URLs from configuration and format them for display.

    Args:
        client_id: Optional client ID for multi-tenant support

    Returns:
        Dictionary with formatted category links
    """
    import json
    from fashion_bot.config_manager import aget_config

    try:
        category_urls_json = await aget_config('category_urls', client_id=client_id)
        
        if category_urls_json:
            if isinstance(category_urls_json, str):
                category_urls = json.loads(category_urls_json)
            else:
                category_urls = category_urls_json
            
            if category_urls and isinstance(category_urls, dict):
                category_links = []
                for category, url in category_urls.items():
                    category_name = category.replace('-', ' ').title()
                    category_links.append({
                        "name": category_name,
                        "url": url,
                        "formatted": f"• {category_name}: {url}"
                    })
                
                return {
                    "success": True,
                    "categories": category_links,
                    "formatted_text": "\n".join([c["formatted"] for c in category_links])
                }
        
        return {"success": False, "categories": [], "formatted_text": ""}
        
    except Exception as e:
        return {"success": False, "categories": [], "error": str(e)}


def variant_is_available(variant: Dict) -> bool:
    """Whether a normalized/GraphQL-shaped variant is sellable right now.

    Reads the ``is_available`` flag computed during the GraphQL product
    transform (``inventoryQuantity > 0 or inventoryPolicy == CONTINUE``),
    falling back to the legacy ``available`` key. Defaults to ``True`` so a
    missing inventory signal never blocks a legitimate order (fail-open).

    Shared by the COD order path (``ShopifyOrderAdapter._find_variant_by_size``)
    and the prepaid path (``create_draft_order_for_prepaid``) so availability is
    judged identically everywhere. NOTE: this is for the GraphQL/normalized shape
    that carries ``is_available``; the raw REST Admin variant shape (which only
    has ``inventory_management`` / ``inventory_policy`` / ``inventory_quantity``)
    is handled separately by ``OrderCreationAPI._rest_variant_in_stock``.
    """
    return bool(variant.get("is_available", variant.get("available", True)))


# ==================== VARIANT MATCHING ====================


def _variant_option_map(variant: Dict) -> Dict[str, str]:
    """Lower-cased ``{option_name: option_value}`` from a variant's ``selectedOptions``."""
    option_map: Dict[str, str] = {}
    for option in variant.get("selectedOptions", []) or []:
        name = (option.get("name") or "").strip().lower()
        value = (option.get("value") or "").strip().lower()
        if name:
            option_map[name] = value
    return option_map


def _norm_option_value(value: str) -> str:
    """Case-fold + trim a single option token for comparison, collapsing runs of
    internal whitespace to a single space.

    Deliberately still a plain casefold (no size-synonym/fuzzy normalization): the
    resolver must match variants exactly, so 'XL' must never collapse onto an
    'XXL' the product doesn't carry. Internal whitespace is the one exception —
    it is never a meaningful part of an option value, and store data routinely
    carries stray double spaces ("Pack of 2  16g") that the agent reproduces with
    single spacing. Matching those strictly dropped real orders; it also left this
    resolver stricter than `_option_values_equal`, so the add-to-cart gate accepted
    a value that order placement then rejected.
    """
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _find_current_variant(
    variants: List[Dict], current_variant_id: str, current_value: str
) -> Optional[Dict]:
    """Locate the order's existing variant to anchor a single-attribute change on."""
    cid = str(current_variant_id or "").strip().replace("gid://shopify/ProductVariant/", "")
    if cid:
        for variant in variants:
            vid = str(variant.get("id", "")).replace("gid://shopify/ProductVariant/", "")
            if vid and vid == cid:
                return variant
    cval = _norm_option_value(current_value)
    if cval:
        for variant in variants:
            if _norm_option_value(variant.get("title")) == cval:
                return variant
        for variant in variants:
            if any(_norm_option_value(v) == cval for v in _variant_option_map(variant).values()):
                return variant
    return None


def _match_anchored_variant(
    variants: List[Dict], req_tokens: List[str], current_variant_id: str, current_value: str
) -> Optional[Dict]:
    """Change only the option(s) the customer named, preserving the rest.

    Anchors on the current line item's variant, overrides the option whose
    valid values include a requested token (e.g. Size 'M' -> 'S'), and returns
    the variant matching the resulting full option set — so a size change on a
    Colour+Size product keeps the colour.
    """
    current = _find_current_variant(variants, current_variant_id, current_value)
    if not current:
        return None
    target = {name: _norm_option_value(value) for name, value in _variant_option_map(current).items()}
    if not target:
        return None

    valid_values: Dict[str, set] = {}
    for variant in variants:
        for name, value in _variant_option_map(variant).items():
            valid_values.setdefault(name, set()).add(_norm_option_value(value))

    applied = False
    for token in req_tokens:
        for name, values in valid_values.items():
            if token in values:
                target[name] = token
                applied = True
                break
    if not applied:
        return None

    for variant in variants:
        if {name: _norm_option_value(value) for name, value in _variant_option_map(variant).items()} == target:
            return variant
    return None


def resolve_variant_match(
    variants: List[Dict],
    requested: str,
    current_variant_id: str = "",
    current_value: str = "",
) -> Optional[Dict]:
    """Find the product variant a customer's requested value refers to.

    Works for single-option products (size-only or colour-only) and multi-option
    products whose Shopify ``title`` is a compound like
    ``"Fog Green Soul Balance Graphic / S"``, where a bare size such as ``'S'``
    never equals the full title.

    Resolution order:
      1. **Full identifier** — exact match on the whole title or the complete set
         of option values (the caller passed the entire "Colour / Size").
      2. **Anchored single-attribute change** — override only the option the
         customer changed and keep the others from the current variant (so
         ``'M' -> 'S'`` preserves the colour). Needs ``current_variant_id`` or
         ``current_value``.
      3. **Single-option / no-anchor fallback** — match the value against the
         title or any one option value.

    Pure and stateless. Returns the matched variant dict, or ``None``.
    """
    if not variants or not (requested or "").strip():
        return None

    req = _norm_option_value(requested)
    req_tokens = [_norm_option_value(t) for t in req.split("/") if t.strip()]

    # 1) Full-identifier match: exact title, or the complete option set.
    for variant in variants:
        if _norm_option_value(variant.get("title")) == req:
            return variant
    if len(req_tokens) > 1:
        want = sorted(req_tokens)
        for variant in variants:
            have = sorted(_norm_option_value(v) for v in _variant_option_map(variant).values())
            if have and have == want:
                return variant

    # 2) Anchored single-attribute change (keeps the untouched options).
    if current_variant_id or current_value:
        anchored = _match_anchored_variant(variants, req_tokens, current_variant_id, current_value)
        if anchored is not None:
            return anchored

    # 3) Single-option / no-anchor fallback.
    for variant in variants:
        if any(_norm_option_value(v) == req for v in _variant_option_map(variant).values()):
            return variant
    for variant in variants:
        if req in _norm_option_value(variant.get("title")):
            return variant
    return None


# ==================== VARIANT SELECTION COMPLETENESS ====================

# Apparel letter sizes. This set is used ONLY for cosmetic/enhancement purposes:
#   * bridging word synonyms to letter values ("large" -> value "L"),
#   * guessing a display label ("Size") for an UNNAMED dimension, and
#   * size-aware sort order.
# It is NOT the source of truth for what counts as a size — value MATCHING is
# unit-agnostic (see `check_variant_option_selection`), so numeric ("32"), volume
# ("100ml"), weight ("500g") and any other unit values work regardless of this
# list. Extending it only improves apparel synonym handling / labelling.
_SIZE_TOKENS = frozenset({"XS", "S", "M", "L", "XL", "XXL", "XXXL", "XXXXL"})

# A value that reads like a measurement/size quantity: a number optionally
# followed by a common unit (100ml, 500 g, 34, 32.5, 40 cm). Used only for the
# cosmetic "is this a size dimension?" label/sort heuristic.
_MEASURE_RE = re.compile(
    r"^\d+(?:\.\d+)?\s*(?:ml|l|g|kg|mg|oz|lb|cm|mm|m|in|inch|\"|')?$",
    re.IGNORECASE,
)


def _variant_option_items(variant: Dict) -> List[Tuple[str, str]]:
    """``[(dimension_key, value)]`` for a variant, tolerant of every shape.

    Reads ingested (``selected_options``) or GraphQL (``selectedOptions``) option
    lists/dicts; falls back to ``option1/2/3`` and finally to splitting the
    compound Shopify ``title`` (e.g. ``"Jet Black / S"``). Unnamed options are
    keyed positionally (``option1``…) so the same dimension groups consistently
    across a product's variants (Shopify keeps option order stable).
    """
    if not isinstance(variant, dict):
        return []

    raw = variant.get("selected_options")
    if raw is None:
        raw = variant.get("selectedOptions")

    items: List[Tuple[str, str]] = []
    if isinstance(raw, list) and raw:
        for idx, opt in enumerate(raw):
            if isinstance(opt, dict):
                name = str(opt.get("name") or "").strip()
                value = str(opt.get("value") or "").strip()
                if value:
                    items.append((name or f"option{idx + 1}", value))
        if items:
            return items
    elif isinstance(raw, dict) and raw:
        for idx, (name, value) in enumerate(raw.items()):
            value = str(value or "").strip()
            if value:
                items.append((str(name).strip() or f"option{idx + 1}", value))
        if items:
            return items

    for key in ("option1", "option2", "option3"):
        value = str(variant.get(key) or "").strip()
        if value:
            items.append((key, value))
    if items:
        return items

    title = str(variant.get("title") or "").strip()
    if title and title.lower() != "default title":
        for idx, part in enumerate(p.strip() for p in title.split("/") if p.strip()):
            items.append((f"option{idx + 1}", part))
    return items


def _variant_is_sellable(variant: Dict) -> bool:
    """Whether a raw/ingested variant is buyable (fail-open on missing signal)."""
    if not isinstance(variant, dict):
        return False
    if "is_available" in variant:
        return bool(variant.get("is_available"))
    if "available" in variant:
        return bool(variant.get("available"))
    inv = variant.get("inventory_quantity")
    if isinstance(inv, (int, float)):
        return inv > 0
    return True


def _value_looks_like_size(value: str) -> bool:
    v = str(value or "").strip()
    if not v:
        return False
    if v.upper() in _SIZE_TOKENS:
        return True
    if normalize_size(v) in _SIZE_TOKENS:
        return True
    # Numeric / range sizes ("32", "32-34", "34.5") and unit sizes ("100ml", "500 g").
    if re.fullmatch(r"\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?", v):
        return True
    return bool(_MEASURE_RE.match(v))


def _dimension_is_size(key: str, values: set) -> bool:
    if str(key or "").strip().lower() in ("size", "sizes"):
        return True
    if not values:
        return False
    hits = sum(1 for v in values if _value_looks_like_size(v))
    return hits >= max(1, (len(values) + 1) // 2)


def _size_sort_key(value: str):
    order = {"XS": 0, "S": 1, "M": 2, "L": 3, "XL": 4, "XXL": 5, "XXXL": 6, "XXXXL": 7}
    v = str(value or "").strip()
    if v.upper() in order:
        return (0, order[v.upper()])
    norm = normalize_size(v)
    if norm in order:
        return (0, order[norm])
    lead = re.match(r"\s*(\d+(?:\.\d+)?)", v)  # numeric / unit sizes: 32, 100ml
    if lead:
        return (1, float(lead.group(1)))
    return (2, v.lower())


def _dimension_display_name(key: str, is_size: bool) -> str:
    if not key or str(key).lower().startswith("option"):
        return "Size" if is_size else "Option"
    return key


def find_variant_by_id(variants: List[Dict], variant_id: str) -> Optional[Dict]:
    target = str(variant_id or "").strip().replace("gid://shopify/ProductVariant/", "")
    if not target:
        return None
    for v in variants:
        if not isinstance(v, dict):
            continue
        vid = str(v.get("id") or v.get("variant_id") or "").strip().replace(
            "gid://shopify/ProductVariant/", ""
        )
        if vid and vid == target:
            return v
    return None


def _option_values_equal(a: str, b: str) -> bool:
    """Compare two option values leniently — the agent may pass a synonym/spacing
    variant of the canonical value (``"small"`` for ``"S"``, ``"100 ml"`` for
    ``"100ml"``). Never parses the customer's raw message; only compares the value
    the agent supplied against the product's own value."""
    a_l = str(a).strip().lower()
    b_l = str(b).strip().lower()
    if a_l == b_l:
        return True
    if a_l.replace(" ", "") == b_l.replace(" ", ""):
        return True
    return normalize_size(a) == normalize_size(b)


def _normalize_provided_options(provided_options) -> List[Tuple[Optional[str], str]]:
    """``[(dimension_name_lower | None, value)]`` from the agent-supplied selection.

    Accepts a ``{name: value}`` dict (preferred, e.g. ``{"Size": "M"}``) or a bare
    iterable of values (``["M", "Blue"]``) for products whose options are unnamed.
    """
    items: List[Tuple[Optional[str], str]] = []
    if isinstance(provided_options, dict):
        for key, value in provided_options.items():
            if value is None:
                continue
            val = str(value).strip()
            if val:
                name = str(key).strip().lower()
                items.append((name or None, val))
    elif isinstance(provided_options, (list, tuple, set)):
        for value in provided_options:
            val = str(value).strip()
            if val:
                items.append((None, val))
    elif isinstance(provided_options, str) and provided_options.strip():
        items.append((None, provided_options.strip()))
    return items


def _provided_value_for_dimension(
    provided: List[Tuple[Optional[str], str]], key: str, values: set
) -> Optional[str]:
    """The value the agent supplied for a dimension — matched by option name, or
    (for unnamed/value-only input) by membership in the dimension's value set."""
    key_l = str(key).strip().lower()
    for name, value in provided:
        if name and name == key_l:
            return value
    for name, value in provided:
        if name and name != key_l:
            continue  # named for a different dimension — don't borrow it
        if any(_option_values_equal(value, cand) for cand in values):
            return value
    return None


def check_variant_option_selection(
    variants: List[Dict], selected_variant_id: str, provided_options
) -> Dict[str, List[Dict]]:
    """Validate the customer's chosen variant options are complete and consistent.

    **LLM-driven by design.** Mapping the customer's natural language ("small",
    "100 ml", "the black one", typos, other languages) to concrete option VALUES
    is the agent's job — it passes the result in ``provided_options`` (ideally a
    ``{name: value}`` dict like ``{"Size": "S"}``). This function never reads the
    customer's message; it only checks, structurally, that:

      * every *choice-bearing* dimension (a variant option with >1 in-stock value)
        has a value in ``provided_options`` — else the choice is unconfirmed, and
      * each supplied value matches ``selected_variant_id``'s actual variant —
        else the wrong variant_id was picked.

    Handles size+colour, single-dimension, unit sizes (100ml/500g), and no-variant
    products alike — nothing here is hardcoded to apparel letter sizes. Returns
    ``{"missing": [...], "mismatch": [...]}``; both empty means safe to add.
    """
    result: Dict[str, List[Dict]] = {"missing": [], "mismatch": []}
    if not isinstance(variants, list) or len(variants) <= 1:
        return result
    chosen = find_variant_by_id(variants, selected_variant_id)
    if chosen is None:
        return result  # variant recognition is enforced elsewhere
    chosen_options = dict(_variant_option_items(chosen))

    dims: "dict[str, set]" = {}
    for variant in variants:
        if isinstance(variant, dict) and _variant_is_sellable(variant):
            for key, value in _variant_option_items(variant):
                dims.setdefault(key, set()).add(value)
    # Always represent the chosen variant's own dimensions (it may be the sole
    # sellable one, or out of stock but explicitly requested).
    for key, value in chosen_options.items():
        dims.setdefault(key, set()).add(value)

    provided = _normalize_provided_options(provided_options)

    for key, values in dims.items():
        if len(values) <= 1:
            continue  # dimension is fixed for this product — no choice to confirm
        is_size = _dimension_is_size(key, values)
        display = _dimension_display_name(key, is_size)
        sort_key = _size_sort_key if is_size else (lambda v: str(v).lower())
        chosen_val = chosen_options.get(key)
        provided_val = _provided_value_for_dimension(provided, key, values)
        if provided_val is None:
            result["missing"].append({"name": display, "values": sorted(values, key=sort_key)})
        elif chosen_val is not None and not _option_values_equal(provided_val, chosen_val):
            result["mismatch"].append({
                "name": display,
                "selected": chosen_val,
                "requested": provided_val,
            })
    return result


# ==================== SHOPIFY URL REPLACEMENT ====================

_MYSHOPIFY_RE = re.compile(r"https?://[^/]*\.myshopify\.com", re.IGNORECASE)

# Matches Shopify CDN image URLs and captures everything after the store-specific
# path segments (e.g. "files/IMAGE.jpg?v=123" or "products/IMAGE.jpg").
_SHOPIFY_CDN_RE = re.compile(
    r"https?://cdn\.shopify\.com/s/files/1/\d+/\d+/\d+/",
    re.IGNORECASE,
)


def replace_shopify_url(url: str, website_url: str) -> str:
    """Replace a *.myshopify.com base URL with the client's real website URL.

    Only acts when the URL actually contains a myshopify.com domain AND
    a valid website_url replacement is available.
    """
    if not url or not website_url:
        return url
    website_base = website_url.rstrip("/")
    return _MYSHOPIFY_RE.sub(website_base, url)


def replace_shopify_cdn_url(url: str, website_url: str) -> str:
    """Convert a Shopify CDN image URL to the store's custom domain equivalent.

    ``cdn.shopify.com/s/files/1/XXXX/XXXX/XXXX/files/IMG.jpg``
    becomes ``store.com/cdn/shop/files/IMG.jpg``

    Shopify proxies CDN assets through the custom domain automatically.
    """
    if not url or not website_url:
        return url
    website_base = website_url.rstrip("/")
    return _SHOPIFY_CDN_RE.sub(f"{website_base}/cdn/shop/", url)


async def aget_shopify_to_website_mapping(client_id: str) -> Optional[str]:
    """Load the client's website_url from the tiered cache.

    Falls back to the ``clients.domain`` column when the config-cache path
    returns nothing — guards against stale empty-dict cache entries that
    pre-date ``client_configs.website_urls`` being populated.

    Returns the website URL string (e.g. "https://groovee.in") or None.
    """
    try:
        from fashion_bot.utils.utils import aget_website_urls_with_caching
        urls = await aget_website_urls_with_caching(client_id=client_id)
        website_url = urls.get("website_url") or None
        if website_url:
            return website_url

        # Fallback: read clients.domain directly so we are never blocked by a
        # stale Redis/memory cache entry that has no website_url.
        from fashion_bot.database_manager import get_async_postgres_connection
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT domain FROM clients WHERE id = %s LIMIT 1",
                    (client_id,),
                )
                row = await cur.fetchone()
                if row:
                    domain = row.get("domain") if isinstance(row, dict) else row[0]
                    return domain.rstrip("/") if domain else None
        return None
    except Exception as e:
        logger.warning("⚠️ Could not load website_url for client %s: %s", client_id, e)
        return None


def replace_shopify_urls_in_product(product: Dict, website_url: str) -> Dict:
    """Replace myshopify.com and cdn.shopify.com URLs in a product dict."""
    if not website_url or not product:
        return product

    for key in ("url", "product_url", "product_link"):
        val = product.get(key)
        if val and isinstance(val, str):
            product[key] = replace_shopify_url(val, website_url)

    for key in ("image_url", "image"):
        val = product.get(key)
        if val and isinstance(val, str) and "cdn.shopify.com" in val:
            product[key] = replace_shopify_cdn_url(val, website_url)

    return product


def replace_shopify_urls_in_products(products: List[Dict], website_url: str) -> List[Dict]:
    """Replace myshopify.com URLs in a list of product dicts."""
    if not website_url:
        return products
    return [replace_shopify_urls_in_product(p, website_url) for p in products]
