"""
Query Understanding — LLM Call #1.

Takes a user's natural language query + client's filterable fields schema
and outputs both a search query string and an Upstash Search filter string.

The system prompt can be customised per client by storing it in the
``agents_config`` table under agent_name = ``'query_understanding'``.
If no client-specific prompt exists, a sensible default is used.
"""

import json
import logging
import re
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

AGENT_NAME = "query_understanding"

DEFAULT_FILTERABLE_FIELDS = [
    {"name": "category", "type": "string", "operator": "=", "description": "broad product category (topwear, bottomwear, footwear, outerwear)"},
    {"name": "subcategory", "type": "string", "operator": "=", "description": "specific product type (hoodie, t-shirt, jeans, sneakers, jacket, shirt, shorts, joggers, polo, sweatshirt, jersey, cargo, chinos, dress, skirt, kurta)"},
    {"name": "segment", "type": "string", "operator": "=", "description": "gender segment (women, men, unisex)"},
    {"name": "price_min", "type": "number", "operator": ">=", "description": "minimum price"},
    {"name": "price_max", "type": "number", "operator": "<=", "description": "maximum price"},
    {"name": "in_stock", "type": "boolean", "operator": "=", "description": "in stock only"},
    {"name": "brand", "type": "string", "operator": "=", "description": "brand/vendor name"},
    {"name": "product_line_normalized", "type": "string", "operator": "=", "description": "specific model or product line variant"},
    {"name": "bestseller", "type": "boolean", "operator": "=", "description": "bestselling / top-selling product flag"},
    {"name": "conversion_rate", "type": "number", "operator": ">", "description": "how well a product converts traffic to sales (units sold per unique session, e.g. 0.05 = 5%). Use '> 0' for 'high converting' / 'best converting' intent, but PREFER sort_by='best_converting' to rank rather than hard-filter"},
    {"name": "variant_availability_pct", "type": "number", "operator": ">", "description": "percentage of variants/sizes in stock (0-100); use '> 50' to keep products whose majority of sizes are available"},
    {"name": "discount_pct", "type": "number", "operator": ">", "description": "discount percentage off MRP / compare-at price (0-100). Use '> 20' for sale / discount / EOSS / end-of-season-sale intent so only genuinely discounted products are returned"},
    {"name": "collections", "type": "array", "operator": "CONTAINS", "description": "Shopify collection membership (array). Use the CONTAINS operator with the EXACT collection title to scope results to a specific collection (e.g. a sale / EOSS collection) when the user explicitly names one. Collection titles are inconsistent, so prefer the semantic query for product type / attributes and only hard-filter when the user clearly asks for a named collection."},
]


@dataclass
class QueryUnderstandingResult:
    query: str = ""
    filter: str = ""
    follow_up: Optional[str] = None
    sort_by: Optional[str] = None
    # True only for catalog-wide trending/bestseller requests (no specific
    # product type/category/brand). Defaults to False when the prompt/LLM didn't
    # classify it (a client-specific DB prompt without the field, or a QU error),
    # so pins stay suppressed unless the LLM explicitly marks the request
    # catalog-wide — the single source of truth for the pinned-products gate.
    is_catalog_wide: bool = False
    # Explicit new-arrivals timeframe in DAYS when the user asked for one
    # (e.g. "new this month" -> 30, "last 2 weeks" -> 14). None when the user
    # didn't specify a timeframe — the pipeline then falls back to the
    # per-client / default new-arrivals window. Only meaningful with
    # sort_by == "newest".
    recency_days: Optional[int] = None
    error: Optional[str] = None


def _format_schema(fields: List[Dict]) -> str:
    lines = []
    for f in fields:
        lines.append(f"- {f['name']} ({f['type']}, {f['operator']}): {f['description']}")
    return "\n".join(lines)


def collection_handle_to_search_hint(handle: Optional[str]) -> str:
    """Convert a Shopify collection handle (URL slug) into a search phrase.

    The handle is already the collection name, extracted from the
    ``/collections/<handle>`` URL by the widget — so we just decode the slug
    separators into spaces. Works across stores without any hardcoded map:

        "designer-hoodies"  -> "designer hoodies"
        "denim-jeans"       -> "denim jeans"
        "men"               -> "men"
    """
    if not handle or not isinstance(handle, str):
        return ""
    return re.sub(r"[-_]+", " ", handle.strip().lower()).strip()


# ---------------------------------------------------------------------------
# Prompt loading — DB (per client) → default fallback
# ---------------------------------------------------------------------------

DEFAULT_SYSTEM_PROMPT = """You are a search query builder for an e-commerce product recommendation engine.

Available HARD FILTER fields (use ONLY these in "filter"):
{schema_text}

Given a user's query and conversation context, output JSON with:
- "query": a natural language search query optimized for semantic matching. Include ALL descriptive attributes like color, occasion, style, material, vibe, pattern, fit, neckline, etc. The semantic search engine will rank results by relevance to this query.
- "filter": an Upstash Search filter string using ONLY the hard filter fields listed above.
  Valid operators: =, !=, <, >, <=, >=, CONTAINS, IN, NOT IN, HAS FIELD
  Combine with AND / OR. ALWAYS include: in_stock = true
- "follow_up": null, or ONE clarifying question if critical info (size, budget) is missing
- "sort_by": null, or one of "newest" / "price_low" / "price_high" / "discount" when the user explicitly wants results ordered. Use "newest" for new arrivals / latest / recently added / what's new. Use "price_low" for CHEAPEST intent — lowest actual price first (cheapest, cheap, lowest price, least expensive, most affordable, budget). Use "price_high" for highest price / most expensive / premium. Use "discount" for biggest-markdown intent (on sale, discounted, best deal, max discount, offers). IMPORTANT: "cheapest" is "price_low", NOT "discount" — a cheapest query wants the lowest price, not the biggest percentage off (which can be a zero-price freebie).
- "is_catalog_wide": true ONLY when the user asks for trending / best-selling / popular / hero / "what's in store" products across the WHOLE catalog with NO specific product type, category, brand, or collection (e.g. "best sellers", "what's trending", "show me popular products"). Set false when the request names or implies a specific product type / category / brand / collection (e.g. "best selling jeans", "trending shirts", "hero products in shirts") or when a collection page context is provided. Default to false when unsure.
- "recency_days": null, or a POSITIVE INTEGER number of days when the user asks for new/latest products within a SPECIFIC timeframe. Convert the phrase to days: "this week" / "last week" -> 7, "last 2 weeks" / "past fortnight" -> 14, "this month" / "last month" / "in the last 1 month" -> 30, "last 3 months" / "this quarter" -> 90, "last 6 months" -> 180, "this year" / "last year" -> 365. Use 30 per month and 7 per week for other counts (e.g. "last 4 months" -> 120). Set null when the user just asks for "new arrivals" / "what's new" / "latest" with NO explicit timeframe. Only meaningful alongside sort_by = "newest".

CATEGORY/SUBCATEGORY RULES:
- Do NOT use "category" or "subcategory" hard filters — catalog labels may be inconsistent and hard filtering can drop relevant products. Put the product type prominently in the "query" for semantic matching instead.

GENERAL RULES:
- CRITICAL: Only use the fields listed above in "filter". All other attributes (color, occasion, style, material, vibe, pattern, fit, neckline) MUST go into the "query" string for semantic matching. EXCEPTION: collection membership may be used as a hard filter via `collections CONTAINS '<exact collection title>'` ONLY when the user explicitly asks for a specific named collection's products; otherwise keep collections in the semantic query.
- Keep the "query" descriptive but concise. Include color, occasion, style, material when the user mentions them.
- Build the "filter" from explicit objective constraints only. Don't over-filter.
- Always add "in_stock = true" to the filter.
- AVAILABILITY: For any browse / listing / multi-product request (e.g. "show me ...", category or collection browsing, "on sale / discounts", recommendations), ALSO add "variant_availability_pct > 50" so products whose majority of sizes are in stock are surfaced first. Omit it ONLY when the user is asking for one specific product by name/handle/URL.
- For price constraints like "under 2000", use "price_max <= 2000".
- If the user mentions a gender/segment, include it in the filter. If not, omit.
- MATCHING/PAIRING QUERIES: When the user asks for a product type to "match", "pair with", or "go with" another product (e.g., "T-shirt to match my denim"), there are always TWO product types in play: the TARGET (what they want to find) and the ANCHOR (what they already have / are viewing). Everything you emit must describe the TARGET.
  - The "query" MUST describe ONLY the TARGET type, NOT the ANCHOR. Including the anchor's type pollutes semantic search and causes wrong product types to rank highly. Strip the "match with X" part out entirely; emphasize the target type with synonyms and style descriptors instead.
  - 🔴 Any category/subcategory hard filter MUST also name the TARGET type. NEVER pin the filter to the anchor's type — a hard filter is absolute, so pinning it to the anchor makes the product the customer actually asked for impossible to return, and they are wrongly told we have none.
  - A pairing request is a CROSS-type request by definition: the target is normally a DIFFERENT type from the anchor. If your target and anchor come out identical, you have misread the request.
  BAD:  "T-shirt to match denim" (semantic search pulls denim products because "denim" dominates)
  GOOD: "casual stylish T-shirt graphic tee" (focuses purely on the desired product type)
  Example — anchor is a pink bra, user asks "matching panties" / "same matching panties available":
  BAD:  {{"query": "pink bras", "filter": "subcategory = 'bra' AND in_stock = true"}}  (followed the ANCHOR — returns zero panties)
  GOOD: {{"query": "pink panties", "filter": "subcategory = 'panty' AND in_stock = true"}}  (follows the TARGET, borrows only the anchor's colour)
- CURRENT PRODUCT CONTEXT: When a "Current product" block is provided, use its style attributes (color, vibe, occasion, aesthetic) to enrich the query for the DESIRED product type. This helps find products that complement the current one. However, NEVER include the current product's own type/name in the query — only borrow its style signals.
  Example: Current product is a black streetwear graphic tee. User asks "show me matching jeans".
  BAD:  "jeans to match graphic tee" (includes tee type → semantic pollution)
  GOOD: "dark wash denim jeans streetwear casual" (borrows streetwear/dark vibe from the tee)
- SIMILAR PRODUCT QUERIES: When the user asks for "similar products", "products like this", "more like this", "show me similar", "more products like this" and a Current product block is provided:
  - Build a BROAD query using ONLY the product's type/subcategory. Do NOT pack all attributes (color, fit, pattern, material) into the initial query — that over-constrains results and returns near-identical products instead of a diverse "similar" set.
  - Add the subcategory hard filter as usual.
  - Generate a follow_up that lists the current product's KEY differentiating attributes (extracted from the Current product block) and asks which ones to prioritize. Use the ACTUAL attribute values from the Current product.
  Example: Current product is "Park Avenue Men Blue Slim Fit Solid Cotton Casual Shirt"
  → query: "casual shirt" (NOT "blue slim fit solid cotton casual shirt")
  → filter: "subcategory = 'shirt' AND in_stock = true"
  → follow_up: "Would you like me to find shirts with a similar color (blue), fit (slim fit), pattern (solid), or material (cotton)?"
  - When the user RESPONDS to this follow-up with specific attribute preferences (e.g., "same color and fit", "I want cotton too"), build the refined query using ONLY those specified attributes combined with the product type.
  Example: User responds "same color and fit" → query: "blue slim fit casual shirt"
  Example: User responds "same material but different color" → query: "cotton casual shirt"

COLLECTION PAGE CONTEXT:
- When a "Current collection page" value is provided, the customer is browsing that collection (e.g. 'denim-jeans'). If their query does NOT already name a specific product type, scope the search to this collection by putting the collection's product-type terms into "query" (e.g. a generic "show me the best ones" on the 'denim-jeans' collection should search for "denim jeans"). If the query already names a product type, prefer the customer's stated type.

BESTSELLER / TOP-SELLING QUERIES:
- When the user asks for "best selling", "top selling", "most popular", "hero products", "trending", or "bestsellers", add "bestseller = true" to the filter.
- If combined with a product type (e.g., "best selling jeans"), also put the product type in the "query" for semantic matching.
- If the query is just "best sellers" / "top selling products" without a specific type, set "query" to a broad term like "popular fashion products".

BEST-CONVERTING / HIGH-CONVERSION QUERIES:
- When the user asks for "best converting", "highest converting", "high conversion", "products that convert best", or "most efficient sellers", set "sort_by" to "best_converting". This ranks by how well each product turns sessions into sales (a different signal from raw sales volume, which is "bestseller").
- Do NOT confuse with "best selling"/"most popular" (that is bestseller = true). Conversion is about efficiency, not total units.
- If combined with a product type (e.g., "best converting shoes"), put the type in "query".

NEW ARRIVALS / LATEST PRODUCTS QUERIES:
- When the user asks for "new", "new arrivals", "latest", "recently added", "what's new", "newest", "just arrived", "new collection", "new products", or "new launches", set "sort_by" to "newest".
- Use a broad "query" appropriate to the store's catalog. If combined with a product type (e.g., "new jeans"), put that type in the "query".
- If the query is generic like "show me new products", set "query" to a broad term like "products".

Examples:
  User: "show me hoodies under 2400"
  -> {{"query": "hoodies", "filter": "price_max <= 2400 AND in_stock = true", "follow_up": "What size are you looking for?"}}

  User: "show me party tops under 2000"
  -> {{"query": "party tops festive stylish", "filter": "price_max <= 2000 AND in_stock = true", "follow_up": "What size are you looking for?"}}

  User: "casual blue shirts for men"
  -> {{"query": "casual blue shirts", "filter": "segment = 'men' AND in_stock = true", "follow_up": null}}

  User: "wide leg jeans in black"
  -> {{"query": "wide leg black jeans denim", "filter": "in_stock = true", "follow_up": null}}

  User: "recommend outfits for Goa trip"
  -> {{"query": "lightweight casual summer outfits beach vacation breathable", "filter": "in_stock = true", "follow_up": "What size are you looking for?"}}

  User: "recommend a T-shirt to match my denim"
  -> {{"query": "casual stylish T-shirt graphic tee", "filter": "in_stock = true", "follow_up": "What size are you looking for?"}}

  User: "show me jackets that go with these jeans"
  -> {{"query": "stylish jacket casual outerwear bomber", "filter": "in_stock = true", "follow_up": null}}

  User: "what do you have in store?"
  -> {{"query": "trending fashion products", "filter": "in_stock = true", "follow_up": null}}

  User: "show me best selling products"
  -> {{"query": "popular fashion products", "filter": "bestseller = true AND in_stock = true", "follow_up": null, "is_catalog_wide": true}}

  User: "what are your top selling jeans?"
  -> {{"query": "jeans", "filter": "bestseller = true AND in_stock = true", "follow_up": null, "is_catalog_wide": false}}

  User: "show me hero products in shirts"
  -> {{"query": "shirts", "filter": "bestseller = true AND in_stock = true", "follow_up": null, "sort_by": null, "is_catalog_wide": false}}

  User: "show me new products"
  -> {{"query": "products", "filter": "in_stock = true", "follow_up": null, "sort_by": "newest", "recency_days": null}}

  User: "latest jeans"
  -> {{"query": "jeans", "filter": "in_stock = true", "follow_up": null, "sort_by": "newest", "recency_days": null}}

  User: "what's new in your store?"
  -> {{"query": "products", "filter": "in_stock = true", "follow_up": null, "sort_by": "newest", "recency_days": null}}

  User: "show me new products of last 1 month"
  -> {{"query": "products", "filter": "in_stock = true", "follow_up": null, "sort_by": "newest", "recency_days": 30}}

  User: "new arrivals in the past 2 weeks"
  -> {{"query": "products", "filter": "in_stock = true", "follow_up": null, "sort_by": "newest", "recency_days": 14}}

  User: "show me your cheapest dresses"
  -> {{"query": "dresses", "filter": "in_stock = true", "follow_up": null, "sort_by": "price_low"}}

  User: "what's on sale / biggest discounts?"
  -> {{"query": "discounted fashion products", "filter": "in_stock = true AND variant_availability_pct > 50", "follow_up": null, "sort_by": "discount"}}

  User: "show me your best converting products"
  -> {{"query": "popular fashion products", "filter": "in_stock = true", "follow_up": null, "sort_by": "best_converting", "is_catalog_wide": true}}

  User: "show me hoodies"
  -> {{"query": "hoodies", "filter": "in_stock = true AND variant_availability_pct > 50", "follow_up": "What size are you looking for?"}}

Return ONLY valid JSON. No markdown, no explanation."""


async def aget_qu_system_prompt(client_id: Optional[str], filterable_fields: List[Dict]) -> str:
    """Load the QU system prompt (async).

    Resolution order:
    1. ``agents_config`` table (via async Redis cache → Postgres) for *client_id*
       with ``agent_name = 'query_understanding'``.
    2. ``DEFAULT_SYSTEM_PROMPT`` (no taxonomy, semantic-only matching).

    Both the DB prompt and the default contain a ``{schema_text}`` placeholder
    that is formatted with the filterable fields schema at runtime.
    """
    schema_text = _format_schema(filterable_fields)

    if client_id:
        try:
            from fashion_bot.utils.utils import aget_agent_prompt_with_caching

            db_prompt = await aget_agent_prompt_with_caching(client_id, AGENT_NAME)
            if db_prompt:
                logger.info(
                    f"📖 Loaded {AGENT_NAME} prompt from DB/cache for "
                    f"client: {client_id} ({len(db_prompt)} chars)"
                )
                try:
                    return db_prompt.format(schema_text=schema_text)
                except KeyError:
                    return db_prompt
        except Exception as e:
            logger.warning(
                f"⚠️ Failed to load {AGENT_NAME} prompt for "
                f"client {client_id}: {e}. Using default."
            )

    return DEFAULT_SYSTEM_PROMPT.format(schema_text=schema_text)


async def understand_query(
    user_query: str,
    client_id: str,
    conversation_history: Optional[List[Dict]] = None,
    user_profile: Optional[Dict] = None,
    filterable_fields: Optional[List[Dict]] = None,
    product_context: Optional[Dict[str, Any]] = None,
    collection_context: Optional[str] = None,
    variant_availability_min_pct: Optional[int] = None,
) -> QueryUnderstandingResult:
    """
    LLM Call #1: Generate search query and filter from user's natural language.

    Args:
        user_query: The user's message
        client_id: Client ID (for loading client-specific schema)
        conversation_history: Previous messages for multi-turn context
        user_profile: User profile with segment, size, etc.
        filterable_fields: Client's filterable fields config (falls back to default)
        product_context: Focal product attributes (name, subcategory, color,
            style, price) for "similar" / "matching" queries.
        collection_context: Human-readable product-type hint for the collection
            page the customer is browsing (e.g. "denim jeans"). Used to scope
            generic queries like "show me the best ones" to the current
            collection instead of the whole catalog.

    Returns:
        QueryUnderstandingResult with query, filter, and optional follow_up
    """
    fields = filterable_fields or DEFAULT_FILTERABLE_FIELDS
    system_prompt = await aget_qu_system_prompt(client_id, fields)
    # Only substitute when the caller has an explicit client-level override.
    # None means "no Postgres config found" — leave the prompt's value intact.
    if variant_availability_min_pct is not None:
        system_prompt = system_prompt.replace(
            "variant_availability_pct > 50",
            f"variant_availability_pct > {variant_availability_min_pct}",
        )

    user_message_parts = [f"User query: {user_query}"]

    if product_context:
        ctx_parts = []
        for key in ("name", "subcategory", "category", "color", "style", "price"):
            val = product_context.get(key)
            if val:
                ctx_parts.append(f"{key}='{val}'")
        if ctx_parts:
            user_message_parts.append(f"Current product: {', '.join(ctx_parts)}")

    if collection_context:
        user_message_parts.append(f"Current collection page: '{collection_context}'.")

    if user_profile:
        profile_parts = []
        if user_profile.get("segment"):
            profile_parts.append(f"segment={user_profile['segment']}")
        if user_profile.get("size"):
            profile_parts.append(f"preferred_size={user_profile['size']}")
        if user_profile.get("budget_max"):
            profile_parts.append(f"budget_max={user_profile['budget_max']}")
        if profile_parts:
            user_message_parts.append(f"User profile: {', '.join(profile_parts)}")

    if conversation_history:
        recent = conversation_history[-10:]
        history_text = "\n".join(
            f"  {'User' if m.get('role') == 'user' else 'Assistant'}: {m.get('content', '')[:200]}"
            for m in recent
        )
        user_message_parts.append(f"Recent conversation:\n{history_text}")

    user_message = "\n".join(user_message_parts)

    try:
        from fashion_bot.core.llm_factory import LLMFactory

        llm = await LLMFactory.aget_llm(tool_name="query_understanding")

        from langchain_core.messages import SystemMessage, HumanMessage
        response = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ])

        text = response.content.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        parsed = json.loads(text)

        filter_str = parsed.get("filter", "in_stock = true")
        filter_str = _validate_filter(filter_str, fields)

        # Accept only the known sort values; everything else (null, "", wrong
        # type, unknown string) normalizes to None → no sort applied.
        sort_by = parsed.get("sort_by")
        if sort_by not in ("newest", "price_low", "price_high", "discount", "best_converting"):
            sort_by = None

        # Default to False (suppress pins) when the LLM/prompt didn't emit a
        # boolean — pins show only on an explicit catalog-wide classification.
        is_catalog_wide = parsed.get("is_catalog_wide")
        if not isinstance(is_catalog_wide, bool):
            is_catalog_wide = False

        # Explicit new-arrivals timeframe (days). Accept only a positive int;
        # reject bools (a bool is an int subclass), non-ints, and non-positive
        # values → None (pipeline falls back to the client/default window). The
        # pipeline clamps the upper bound, so we don't cap it here.
        recency_days = parsed.get("recency_days")
        if isinstance(recency_days, bool) or not isinstance(recency_days, int) or recency_days <= 0:
            recency_days = None

        return QueryUnderstandingResult(
            query=parsed.get("query", user_query),
            filter=filter_str,
            follow_up=parsed.get("follow_up"),
            sort_by=sort_by,
            is_catalog_wide=is_catalog_wide,
            recency_days=recency_days,
        )

    except Exception as e:
        logger.error(f"Query understanding failed: {e}")
        return QueryUnderstandingResult(
            query=user_query,
            filter="in_stock = true",
            error=str(e),
        )


def _quote_value(value: str) -> str:
    """Return value properly single-quoted for Upstash filter syntax."""
    value = value.strip()
    # Already single-quoted — re-escape interior apostrophes only
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        inner = value[1:-1].replace("\\'", "'").replace("'", "\\'")
        return f"'{inner}'"
    # Numeric or boolean literals — leave bare
    if re.fullmatch(r"-?\d+(?:\.\d+)?", value) or value in ("true", "false"):
        return value
    # String value — wrap in single quotes, escape backslash then apostrophe
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


# Matches:  field OP value
# value = already-quoted | numeric/bool | unquoted tokens (stops at AND/OR/parens/EOL)
# Apostrophes are allowed inside unquoted tokens (e.g. "women's") so we can quote them properly.
_CONDITION_RE = re.compile(
    r"""(\w+)\s*(=|!=|>=?|<=?)\s*("""
    r"""'(?:[^']|'(?!\s*(?:AND\b|OR\b|$)))*'"""  # single-quoted (interior apostrophes allowed, e.g. "Kids' Clothing")
    r"""|"[^"]*\""""          # double-quoted
    r"""|-?\d+(?:\.\d+)?"""   # numeric
    r"""|true|false"""        # boolean
    r"""|(?:(?!AND\b|OR\b)[^\s()\[\],"]+(?:\s+(?!AND\b|OR\b)[^\s()\[\],"]+)*)"""  # unquoted (apostrophes allowed)
    r""")""",
    re.IGNORECASE,
)


# Matches a bracketed membership list the LLM sometimes emits in JSON/Python
# style — e.g. ``category IN ['Bras', 'Shapewear']``. Upstash's filter DSL wants
# parentheses (``IN ('Bras', 'Shapewear')``); the raw ``[`` is a tokenizer error
# ("token recognition error at: '['"), so the whole search fails. We rewrite the
# brackets to parens and quote each member via ``_quote_value``.
_IN_LIST_RE = re.compile(r"(\w+)\s+(NOT\s+IN|IN)\s*\[([^\]]*)\]", re.IGNORECASE)


def _fix_in_list(m: re.Match) -> str:
    field, op, body = m.group(1), m.group(2), m.group(3)
    op = re.sub(r"\s+", " ", op.strip().upper())
    values = [_quote_value(v) for v in body.split(",") if v.strip()]
    return f"{field} {op} ({', '.join(values)})"


def _validate_filter(filter_str: str, fields: List[Dict]) -> str:
    """Fix LLM-generated filter strings: quote string values, strip empty clauses, add in_stock."""
    if not filter_str or not filter_str.strip():
        return "in_stock = true"

    def _fix_condition(m: re.Match) -> str:
        return f"{m.group(1)} {m.group(2)} {_quote_value(m.group(3))}"

    # Normalize bracketed IN/NOT IN lists (`IN ['a','b']`) to Upstash's
    # parenthesized syntax (`IN ('a','b')`) BEFORE per-condition fixups — the
    # raw `[` is otherwise passed through verbatim and rejected by Upstash.
    filter_str = _IN_LIST_RE.sub(_fix_in_list, filter_str)
    filter_str = _CONDITION_RE.sub(_fix_condition, filter_str)

    # Strip leading/trailing AND / OR left by empty conditions (repeat to handle "OR AND ...")
    filter_str = re.sub(r"^(?:\s*(?:AND|OR)\s+)+", "", filter_str, flags=re.IGNORECASE)
    filter_str = re.sub(r"(?:\s+(?:AND|OR))+\s*$", "", filter_str, flags=re.IGNORECASE)
    # Collapse consecutive AND/OR caused by dropped empty clauses
    filter_str = re.sub(r"\s+(?:AND|OR)\s+(?:AND|OR)\s+", " AND ", filter_str, flags=re.IGNORECASE)

    filter_str = filter_str.strip()
    if not filter_str:
        return "in_stock = true"

    if "in_stock" not in filter_str:
        filter_str = f"({filter_str}) AND in_stock = true"

    return filter_str
