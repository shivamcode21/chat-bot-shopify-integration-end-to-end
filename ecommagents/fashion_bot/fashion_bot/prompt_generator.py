"""
Personalized Prompt Generator

Generates client-specific prompts for:
- query_understanding
- recommendations_handler
- product_attribute_extractor

Uses Groovee's prompts as structural templates, adapts them to the new
client's catalog taxonomy discovered from /products.json.
"""

import asyncio
import json
import logging
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from fashion_bot.core.llm_factory import LLMFactory
from fashion_bot.core.llm_config import (
    LLMConfig,
    get_background_llm_config,
    get_smaller_llm_config,
    BACKGROUND_OPENROUTER_KEY_ENV,
)
from fashion_bot.database_manager import get_postgres_connection
from fashion_bot.services.product_ingestion.storefront_http import (
    PER_PAGE_DELAY,
    afetch_storefront_json,
)
from fashion_bot.env_loader import get_env

logger = logging.getLogger("prompt_generator")

_NO_TRACE_CONFIG: Dict[str, Any] = {"callbacks": []}

TEMPLATE_CLIENT_ID = "7878c45d-a39d-488d-b37e-7fa4fc04ed7a"

SPECIALIZED_AGENTS = [
    "query_understanding",
    "recommendations_handler",
    "product_attribute_extractor",
]


def _prompt_gen_config() -> LLMConfig:
    """OpenRouter config for prompt generation.

    Delegates to the shared background helper so the model follows
    BACKGROUND_LLM_MODEL / LLM_MODEL like every other background workload,
    instead of being pinned to a literal here. Still lands on the background
    key (OPENROUTER_API_KEY_2) so it never competes with the chat key.
    """
    return get_background_llm_config(temperature=0.1, max_tokens=16384)


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Reference prompt loading
# ---------------------------------------------------------------------------

def _load_template_prompt(agent_name: str) -> str:
    """Load the template client's (Groovee) prompt from postgres as the structural reference."""
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT agent_prompt FROM agents_config "
                "WHERE client_id = %s AND agent_name = %s",
                (TEMPLATE_CLIENT_ID, agent_name),
            )
            row = cur.fetchone()
            if row:
                return row["agent_prompt"]
    raise ValueError(f"Template prompt not found for agent: {agent_name}")


def _fetch_existing_agent_names(client_id: str) -> Set[str]:
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT agent_name FROM agents_config WHERE client_id = %s",
                (client_id,),
            )
            return {row["agent_name"] for row in cur.fetchall()}


def _upsert_agent(client_id: str, agent_name: str, prompt: str):
    """Insert or update an agent prompt in agents_config."""
    from datetime import datetime, timezone
    from psycopg.types.json import Json

    now = datetime.now(timezone.utc)
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM agents_config WHERE client_id = %s AND agent_name = %s",
                (client_id, agent_name),
            )
            if cur.fetchone():
                cur.execute(
                    "UPDATE agents_config SET agent_prompt = %s, updated_at = %s "
                    "WHERE client_id = %s AND agent_name = %s",
                    (prompt, now, client_id, agent_name),
                )
            else:
                cur.execute(
                    """INSERT INTO agents_config
                       (client_id, agent_name, agent_prompt,
                        when_to_route, when_not_to_route, created_by,
                        created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (client_id, agent_name, prompt, "", "",
                     "prompt_generator", now, now),
                )


# ---------------------------------------------------------------------------
# Catalog discovery from /products.json
# ---------------------------------------------------------------------------

async def discover_catalog_taxonomy(
    domain: str,
    client_id: Optional[str] = None,
    raw_products: Optional[List[Dict]] = None,
) -> Tuple[Dict[str, Any], List[Dict]]:
    """
    Fetch a sample of the store's product catalog and extract structured
    taxonomy: categories, subcategories, brands, attributes, etc.

    Returns (catalog_dict, raw_products) so callers can pass the raw
    products to LLM-based taxonomy discovery.

    If *raw_products* is supplied the network fetch is skipped entirely —
    including when it is supplied and empty. An empty list means the caller
    already fetched and found nothing; re-fetching here just doubles the load
    on a storefront that is likely rate-limiting us in the first place.
    """
    products = (
        raw_products if raw_products is not None
        else await _fetch_product_sample(domain, client_id)
    )
    if not products:
        logger.warning(f"No products fetched from {domain}")
        return {"store_name": domain, "products_sampled": 0}, []

    return _analyze_products(products, domain), products


async def _fetch_product_sample(
    domain: str,
    client_id: Optional[str] = None,
    limit: int = 5000,
) -> List[Dict]:
    """Fetch products via Shopify creds or public /products.json (up to `limit`)."""
    if client_id:
        try:
            products = await _fetch_via_shopify_api(client_id, limit)
            if products:
                logger.info(f"Fetched {len(products)} products via Shopify API")
                return products
        except Exception as e:
            logger.debug(f"Shopify API unavailable for {client_id}: {e}")

    base = domain.rstrip("/")
    if not base.startswith("http"):
        base = f"https://{base}"

    all_products: List[Dict] = []
    page = 1
    per_page = 250

    while len(all_products) < limit:
        try:
            # Retries 429/503 with backoff. Without this a single rate-limit
            # response silently yielded an empty catalog, which then read as
            # "this store has no products" downstream.
            data = await afetch_storefront_json(
                f"{base}/products.json",
                {"limit": per_page, "page": page},
            )
            batch = data.get("products", [])
            if not batch:
                break
            all_products.extend(batch)
            logger.info(
                f"Fetched page {page}: {len(batch)} products "
                f"(total so far: {len(all_products)})"
            )
            page += 1
            if len(batch) < per_page:
                break
            await asyncio.sleep(PER_PAGE_DELAY)
        except Exception as e:
            logger.warning(f"Failed to fetch products page {page}: {e}")
            break

    all_products = all_products[:limit]
    logger.info(f"Fetched {len(all_products)} products from {base}/products.json")
    return all_products


async def _fetch_via_shopify_api(client_id: str, limit: int) -> List[Dict]:
    """Attempt to fetch products using stored Shopify credentials (paginated)."""
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT config_value FROM client_configs "
                "WHERE client_id = %s AND config_key = 'shopify_credentials'",
                (client_id,),
            )
            row = cur.fetchone()
            if not row:
                return []

    creds = row["config_value"]
    if isinstance(creds, str):
        creds = json.loads(creds)

    shop_domain = creds.get("shop_domain", "")
    token = creds.get("access_token", "")
    if not shop_domain or not token:
        return []

    base_url = f"https://{shop_domain}/admin/api/2024-01/products.json"
    headers = {"X-Shopify-Access-Token": token}
    all_products: List[Dict] = []
    per_page = 250
    params: Dict[str, Any] = {"limit": per_page}

    async with httpx.AsyncClient(timeout=30.0) as client:
        while len(all_products) < limit:
            resp = await client.get(base_url, params=params, headers=headers)
            if resp.status_code != 200:
                break
            batch = resp.json().get("products", [])
            if not batch:
                break
            all_products.extend(batch)
            logger.info(
                f"Shopify API page: {len(batch)} products "
                f"(total so far: {len(all_products)})"
            )
            if len(batch) < per_page:
                break
            link_header = resp.headers.get("link", "")
            next_url = _parse_next_link(link_header)
            if not next_url:
                break
            base_url = next_url
            params = {}

    return all_products[:limit]


def _parse_next_link(link_header: str) -> Optional[str]:
    """Extract the 'next' page URL from a Shopify Link header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            match = re.search(r"<(.+?)>", part)
            if match:
                return match.group(1)
    return None


def _analyze_products(products: List[Dict], domain: str) -> Dict[str, Any]:
    """
    Analyze raw product JSON to extract structured catalog taxonomy.
    Works with Shopify /products.json format.
    """
    store_name = domain.split(".")[0].replace("my", "").replace("www", "").title()

    # Counters
    product_types = Counter()
    vendors = Counter()
    tags_counter = Counter()
    all_options: Dict[str, Counter] = defaultdict(Counter)
    materials_seen = Counter()
    colors_seen = Counter()
    segments = Counter()
    collections_seen = Counter()

    gender_keywords = {
        "men": ["men", "mens", "men's", "male", "gents", "him", "boy", "boys"],
        "women": ["women", "womens", "women's", "female", "ladies", "her", "girl", "girls"],
        "unisex": ["unisex"],
        "kids": ["kids", "kid", "children", "child", "baby"],
    }

    material_keywords = [
        "cotton", "linen", "polyester", "silk", "wool", "denim", "leather",
        "nylon", "spandex", "viscose", "rayon", "acrylic", "merino",
        "terry", "satin", "chiffon", "georgette", "crepe", "velvet",
        "fleece", "microfiber", "modal", "tencel", "lyocell", "cashmere",
    ]

    pattern_keywords = [
        "solid", "striped", "checks", "checkered", "printed", "floral",
        "abstract", "geometric", "dobby", "jacquard", "yarn dyed",
        "self-design", "textured", "plaid", "polka dot", "paisley",
        "embroidered", "woven",
    ]

    fit_keywords = [
        "slim fit", "regular fit", "tailored fit", "relaxed fit",
        "oversized", "skinny", "tapered", "comfort fit", "super slim",
        "contemporary fit", "loose fit", "athletic fit", "wide leg",
        "straight fit", "bootcut",
    ]

    for p in products:
        ptype = (p.get("product_type") or "").strip()
        if ptype:
            product_types[ptype.lower()] += 1

        vendor = (p.get("vendor") or "").strip()
        if vendor:
            vendors[vendor] += 1

        tags = p.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        for tag in tags:
            tag_lower = tag.strip().lower()
            if tag_lower:
                tags_counter[tag_lower] += 1

        title = (p.get("title") or "").lower()
        desc = (p.get("body_html") or p.get("description") or "").lower()
        combined_text = f"{title} {desc} {' '.join(str(t).lower() for t in tags)}"

        # Detect segment from title/tags
        for seg, kws in gender_keywords.items():
            if any(kw in combined_text for kw in kws):
                segments[seg] += 1

        # Detect materials
        for mat in material_keywords:
            if mat in combined_text:
                materials_seen[mat] += 1

        # Detect patterns
        for pat in pattern_keywords:
            if pat in combined_text:
                pass  # collected separately below

        # Collect option names and values
        for opt in p.get("options", []):
            opt_name = (opt.get("name") or "").lower()
            for val in opt.get("values", []):
                val_lower = str(val).strip().lower()
                if val_lower:
                    all_options[opt_name][val_lower] += 1

    # Determine primary segment
    primary_segment = segments.most_common(1)[0][0] if segments else None

    # Build subcategory mapping from product_types
    subcategories = {}
    for ptype, count in product_types.most_common():
        if count >= 1:
            subcategories[ptype] = {"count": count}

    # Extract color and size option values
    colors = []
    sizes = []
    for opt_name, vals in all_options.items():
        if "color" in opt_name or "colour" in opt_name:
            colors = [v for v, _ in vals.most_common(30)]
        elif "size" in opt_name:
            sizes = [v for v, _ in vals.most_common(30)]

    # Extract materials from titles/descriptions
    top_materials = [m for m, _ in materials_seen.most_common(15) if _ >= 2]

    # Extract patterns from product titles
    patterns_found = Counter()
    for p in products:
        title = (p.get("title") or "").lower()
        for pat in pattern_keywords:
            if pat in title:
                patterns_found[pat] += 1
    top_patterns = [pat for pat, _ in patterns_found.most_common(15) if _ >= 2]

    # Extract fits from product titles
    fits_found = Counter()
    for p in products:
        title = (p.get("title") or "").lower()
        for fit in fit_keywords:
            if fit in title:
                fits_found[fit.replace(" fit", "")] += 1
    top_fits = [f for f, _ in fits_found.most_common(10) if _ >= 2]

    # Build attributes per subcategory
    attrs_by_subcat: Dict[str, Dict[str, Counter]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    for p in products:
        ptype = (p.get("product_type") or "").strip().lower()
        if not ptype:
            continue
        title = (p.get("title") or "").lower()
        for mat in material_keywords:
            if mat in title:
                attrs_by_subcat[ptype]["material"][mat] += 1
        for pat in pattern_keywords:
            if pat in title:
                attrs_by_subcat[ptype]["pattern"][pat] += 1
        for fit in fit_keywords:
            if fit in title:
                attrs_by_subcat[ptype]["fit"][fit.replace(" fit", "")] += 1

    subcategory_attributes = {}
    for subcat, attr_counters in attrs_by_subcat.items():
        attrs = {}
        for attr_name, counter in attr_counters.items():
            values = [v for v, c in counter.most_common(8) if c >= 1]
            if values:
                attrs[attr_name] = values
        if attrs:
            subcategory_attributes[subcat] = attrs

    return {
        "store_name": store_name,
        "domain": domain,
        "products_sampled": len(products),
        "segment": primary_segment,
        "product_types": dict(product_types.most_common(50)),
        "subcategories": subcategories,
        "subcategory_attributes": subcategory_attributes,
        "brands": [v for v, _ in vendors.most_common(20)],
        "materials": top_materials,
        "patterns": top_patterns,
        "fits": top_fits,
        "colors": colors,
        "sizes": sizes,
        "top_tags": [t for t, _ in tags_counter.most_common(50)],
    }


# ---------------------------------------------------------------------------
# Prompt generation via LLM — single call with headings validation
# ---------------------------------------------------------------------------

_SECTION_HEADER_RE = re.compile(r"^(\[.+?\])", re.MULTILINE)


def _extract_headings(prompt: str) -> List[str]:
    """Extract all [HEADING] markers from a prompt."""
    return _SECTION_HEADER_RE.findall(prompt)


def _find_missing_headings(reference_headings: List[str], generated: str) -> List[str]:
    """Return reference headings not found in the generated prompt."""
    gen_upper = generated.upper()
    return [h for h in reference_headings if h.upper() not in gen_upper]


_GENERATION_SYSTEM_MSG = """You are an expert at adapting e-commerce AI agent prompts.
You will receive:
1. A REFERENCE PROMPT from Groovee (a streetwear fashion store) with [HEADING] sections
2. A CATALOG TAXONOMY discovered from a new client's product catalog
3. A REQUIRED SECTIONS CHECKLIST — every [HEADING] that MUST appear in your output

CRITICAL RULES:
- Your output MUST contain EVERY [HEADING] from the checklist, in the same order.
- Do NOT drop, merge, rename, or summarize any section.
- For sections with FRAMEWORK RULES (tool chains, workflows, filtering logic, nudge rules, deduplication, pitch): copy the structure and rules VERBATIM. Only change product-type examples/names.
- For sections with CLIENT-SPECIFIC DATA (categories, subcategories, attributes, brands, examples): replace Groovee-specific values with the new client's catalog data.
- The output must be approximately the SAME LENGTH as the reference prompt.

Return ONLY the adapted prompt text. No explanation, no markdown fences."""


async def _generate_prompt_via_llm(
    agent_name: str,
    reference_prompt: str,
    catalog: Dict[str, Any],
    client_name: str,
    domain: str,
    business_type: str = "",
) -> str:
    """
    Single-call prompt generation with post-validation:
    1. Extract [HEADING] markers from reference as a checklist
    2. Send the full reference + catalog to the LLM in one call
    3. Validate all headings are present; retry once if any are missing
    """
    llm = LLMFactory.get_llm(tool_name="prompt_generation", override_config=_prompt_gen_config())

    ref_headings = _extract_headings(reference_prompt)
    headings_checklist = "\n".join(f"  {i+1}. {h}" for i, h in enumerate(ref_headings))

    logger.info(
        f"    📐 {agent_name}: {len(ref_headings)} [HEADING] sections in reference "
        f"({len(reference_prompt)} chars)"
    )

    user_msg = f"""Adapt this prompt from Groovee (a streetwear fashion store) to the new client.

New client: {client_name} ({domain})
Business type: {business_type or 'e-commerce'}

CATALOG TAXONOMY discovered from the new client's product catalog:
{json.dumps(catalog, indent=2, ensure_ascii=False)}
"""

    # Inject the actual indexed field values so the LLM uses the correct
    # segment/category/subcategory values. Without this, the LLM-generated
    # prompt ends up with human-readable Shopify labels (e.g. "Men's Clothing")
    # that don't match what's stored in the search index (e.g. segment='men').
    # Applied to both QU and extractor prompts so they share a single source
    # of truth for subcategory names (the client_taxonomy_config table).
    ingestion_tax = catalog.get("ingestion_taxonomy")
    if agent_name in ("query_understanding", "product_attribute_extractor") and ingestion_tax:
        subcategory_names = sorted(ingestion_tax.get("subcategory_mapping", {}).keys())
        user_msg += f"""
CRITICAL — INDEXED FIELD VALUES FOR HARD FILTERS:
The product ingestion pipeline stores these EXACT values in the search index.
The [CATALOG TAXONOMY] section and ALL filter examples in your output MUST use
ONLY these values for segment, category, and subcategory hard filters — NOT
the human-readable labels from CATALOG TAXONOMY above.

Indexed segments: {json.dumps(ingestion_tax.get('segments', []))}
Indexed categories: {json.dumps(ingestion_tax.get('categories', []))}
Indexed subcategories (flat list): {json.dumps(subcategory_names)}
Indexed subcategories (canonical name → parent category): {json.dumps(ingestion_tax.get('subcategory_mapping', dict()))}

When writing segment filters, use the exact strings from "Indexed segments" above.
When writing category filters, use the exact strings from "Indexed categories" above.
When writing subcategory filters, use the canonical names (keys) from "Indexed subcategories" above.
For the product_attribute_extractor: the subcategory field in the output JSON MUST be
one of the values from "Indexed subcategories (flat list)" above — do not invent
granular subcategories like "casual shirts" or "smart formal shirts".
"""

    user_msg += f"""REQUIRED SECTIONS CHECKLIST — your output MUST contain ALL of these [HEADING] markers in this order:
{headings_checklist}

REFERENCE PROMPT (Groovee) — {len(reference_prompt)} characters:
---
{reference_prompt}
---

Your adapted prompt MUST be at least {int(len(reference_prompt) * 0.8)} characters long and contain every [HEADING] listed above.
Return ONLY the adapted prompt. No explanation, no markdown fences."""

    messages = [
        ("system", _GENERATION_SYSTEM_MSG),
        ("human", user_msg),
    ]

    content = ""
    for attempt in range(1, 3):
        response = await llm.ainvoke(messages, config=_NO_TRACE_CONFIG)
        content = (response.content or "").strip()
        content = _strip_markdown_fences(content)

        missing = _find_missing_headings(ref_headings, content)
        if not missing:
            logger.info(
                f"    ✅ {agent_name}: all {len(ref_headings)} headings present "
                f"(attempt {attempt}, {len(content)} chars, ref {len(reference_prompt)} chars)"
            )
            return content

        if attempt >= 2:
            logger.warning(
                f"    ⚠️ {agent_name}: {len(missing)} headings still missing after retry: "
                f"{missing[:5]}"
            )
            return content

        logger.info(
            f"    🔄 {agent_name}: {len(missing)} missing headings on attempt {attempt}, "
            f"retrying — missing: {missing}"
        )
        messages.append(("ai", content))
        messages.append(("human", (
            f"Your output is MISSING these required [HEADING] sections:\n"
            + "\n".join(f"  - {h}" for h in missing)
            + "\n\nPlease output the COMPLETE adapted prompt with ALL sections. "
            "Do NOT drop any [HEADING]."
        )))

    return content


# ---------------------------------------------------------------------------
# Business categories merging
# ---------------------------------------------------------------------------


def _merge_business_categories(
    catalog: Dict[str, Any], business_categories: List[str]
) -> None:
    """
    Merge website-scraped business categories into the catalog taxonomy so
    the LLM sees ALL categories even if the product sample didn't cover them.

    Adds missing categories to product_types and subcategories with count 0,
    and stores the full scraped list under 'website_detected_categories'.
    """
    catalog["website_detected_categories"] = business_categories

    product_types: Dict[str, int] = catalog.get("product_types", {})
    subcategories: Dict[str, Any] = catalog.get("subcategories", {})

    added = []
    for cat in business_categories:
        cat_lower = cat.strip().lower()
        if not cat_lower:
            continue
        if cat_lower not in product_types:
            product_types[cat_lower] = 0
            added.append(cat_lower)
        if cat_lower not in subcategories:
            subcategories[cat_lower] = {"count": 0, "source": "website_scraping"}

    catalog["product_types"] = product_types
    catalog["subcategories"] = subcategories

    if added:
        logger.info(
            f"  📎 Merged {len(added)} categories from website scraping into "
            f"catalog taxonomy: {added}"
        )


# ---------------------------------------------------------------------------
# LLM-based taxonomy discovery → client_taxonomy_config
# ---------------------------------------------------------------------------

_TAXONOMY_DISCOVERY_PROMPT = """Analyze this product catalog sample from an e-commerce store and build a structured taxonomy.

Store: {store_name} ({domain})
Business type: {business_type}

Product sample (title | product_type | tags):
{product_sample}

Based on the above products, return a JSON object with the following fields:

1. "categories": Array of broad product categories present in this catalog (e.g., "topwear", "bottomwear", "outerwear", "footwear", "accessories", "bags", "innerwear", "skincare", "phone_cases", "home_decor"). Only include categories that are actually represented.

2. "subcategory_mapping": Object where each key is a specific product type found in the catalog, and the value is an object with "category" (the parent broad category). Deduplicate synonyms into a single canonical name (e.g., "hoodies" and "hoodie" → just "hoodie"). Include ALL distinct product types you can identify from the titles, not just from the product_type field.

3. "occasions": Array of occasions these products suit (e.g., "casual", "party", "formal", "work", "festive", "sport", "daily", "streetwear", "winter", "summer"). Only include relevant ones.

4. "styles": Array of style descriptors relevant to this catalog (e.g., "oversized", "slim-fit", "wide-leg", "cropped", "high-waist", "regular-fit"). Only include relevant ones.

5. "vibes": Array of aesthetic vibes that match this brand/catalog (e.g., "trendy", "streetwear", "minimalist", "elegant", "bohemian", "sporty", "casual", "edgy"). Only include relevant ones.

6. "segments": Array of gender segments served (e.g., "women", "men", "unisex", "kids"). Infer from product titles and tags.

7. "color_families": Array of normalized color groups present (e.g., "black", "white", "blue", "red", "green", "pink", "brown", "grey", "beige", "yellow", "orange", "purple", "multicolor").

8. "patterns": Array of pattern types found (e.g., "solid", "striped", "printed", "floral", "checkered", "abstract", "geometric").

9. "fits": Array of fit types found (e.g., "slim", "regular", "relaxed", "oversized", "tailored", "skinny").

10. "pairing_tags": Array of items that pair well with products in this catalog (e.g., "jeans", "sneakers", "hoodie", "jacket", "boots", "heels", "blazer").

11. "attribute_schema": Object mapping each subcategory to its commonly observed attribute values. For each subcategory, list the materials, patterns, and fits you can identify from the product titles/tags. Example: {{"jeans": {{"material": ["denim", "cotton blend"], "fit": ["skinny", "regular"]}}, "hoodie": {{"material": ["fleece", "cotton", "terry"]}}}}. Only include attributes you can actually observe in the data.

RULES:
- Normalize all values to lowercase.
- Deduplicate: use one canonical name per concept ("hoodie" not both "hoodie" and "hoodies").
- Only include values actually relevant to this catalog, not generic defaults.
- For subcategory_mapping, look at BOTH the product_type field AND the product titles to identify types. Shopify product_type is often wrong or missing.

Return ONLY valid JSON, no markdown, no explanation."""


def _build_product_sample_text(products: List[Dict], max_products: int = 50) -> str:
    """Build a rich text representation of products for the taxonomy discovery LLM.

    Includes all available fields so the LLM can infer accurate categories,
    materials, fits, colors, etc. — even when titles are sparse.
    """
    lines = []
    for p in products[:max_products]:
        title = (p.get("title") or "").strip()
        ptype = (p.get("product_type") or "").strip()
        vendor = (p.get("vendor") or "").strip()

        tags = p.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        tags_str = ", ".join(tags[:12])

        option_parts = []
        for opt in p.get("options", []):
            name = opt.get("name", "")
            vals = opt.get("values", [])
            if name and vals and name.lower() != "title":
                option_parts.append(f"{name}: {', '.join(str(v) for v in vals[:10])}")
        options_str = " | ".join(option_parts) if option_parts else ""

        import re
        desc = p.get("body_html") or p.get("description") or ""
        if desc and "<" in desc:
            desc = re.sub(r"<[^>]+>", " ", desc)
            desc = re.sub(r"\s+", " ", desc).strip()
        desc = desc[:150]

        parts = [f"- {title}"]
        if ptype:
            parts.append(f"type: {ptype}")
        if vendor:
            parts.append(f"vendor: {vendor}")
        if tags_str:
            parts.append(f"tags: {tags_str}")
        if options_str:
            parts.append(f"options: [{options_str}]")
        if desc:
            parts.append(f"desc: {desc}")

        lines.append(" | ".join(parts))
    return "\n".join(lines)


async def _adiscover_taxonomy_via_llm(
    products: List[Dict],
    domain: str,
    store_name: str = "",
    business_type: str = "",
) -> Dict[str, Any]:
    """Use an LLM to analyze a product sample and discover the taxonomy.

    Returns a dict matching the client_taxonomy_config schema.
    Falls back to defaults on any failure.
    """
    from fashion_bot.services.product_ingestion.taxonomy_config_store import get_defaults

    sample_text = _build_product_sample_text(products)
    prompt = _TAXONOMY_DISCOVERY_PROMPT.format(
        store_name=store_name or domain,
        domain=domain,
        business_type=business_type or "e-commerce",
        product_sample=sample_text,
    )

    try:
        smaller_config = get_smaller_llm_config(
            temperature=0.1,
            max_tokens=4000,
            api_key_env_var=BACKGROUND_OPENROUTER_KEY_ENV,  # background → key 2
        )
        llm = LLMFactory.get_llm(tool_name="prompt_generation", override_config=smaller_config)
        response = await llm.ainvoke([("human", prompt)], config=_NO_TRACE_CONFIG)
        content = _strip_markdown_fences(response.content or "")
        taxonomy = json.loads(content)

        required_keys = ["categories", "subcategory_mapping"]
        if not all(k in taxonomy for k in required_keys):
            logger.warning("LLM taxonomy missing required keys, falling back to defaults")
            return get_defaults()

        defaults = get_defaults()
        for key in ("occasions", "styles", "vibes", "segments", "color_families",
                     "patterns", "fits", "pairing_tags"):
            if key not in taxonomy or not taxonomy[key]:
                taxonomy[key] = defaults[key]

        if "attribute_schema" not in taxonomy:
            taxonomy["attribute_schema"] = {}

        logger.info(
            f"  🧠 LLM discovered taxonomy: "
            f"{len(taxonomy.get('categories', []))} categories, "
            f"{len(taxonomy.get('subcategory_mapping', {}))} subcategories, "
            f"{len(taxonomy.get('occasions', []))} occasions, "
            f"{len(taxonomy.get('styles', []))} styles"
        )
        return taxonomy

    except Exception as exc:
        logger.error(f"❌ LLM taxonomy discovery failed: {exc}", exc_info=True)
        return get_defaults()


async def _persist_taxonomy_config(
    client_id: str,
    products: List[Dict],
    domain: str,
    store_name: str = "",
    business_type: str = "",
) -> bool:
    """Discover taxonomy via LLM and persist to client_taxonomy_config."""
    try:
        from fashion_bot.services.product_ingestion.taxonomy_config_store import (
            aupsert_taxonomy_config,
        )
        taxonomy = await _adiscover_taxonomy_via_llm(
            products, domain, store_name, business_type,
        )
        ok = await aupsert_taxonomy_config(client_id, taxonomy)
        if ok:
            logger.info(
                f"  ✅ Persisted taxonomy config for {client_id} "
                f"({len(taxonomy.get('categories', []))} categories, "
                f"{len(taxonomy.get('subcategory_mapping', {}))} subcategories)"
            )
        return ok
    except Exception as exc:
        logger.error(f"❌ Failed to persist taxonomy config for {client_id}: {exc}", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Cross-validation
# ---------------------------------------------------------------------------


async def _cross_validate_taxonomy_vs_prompts(
    client_id: str,
    created_agents: List[str],
) -> List[str]:
    """Compare subcategory values in generated prompts against taxonomy config.

    Returns a list of warning strings (empty = all good).  Non-fatal — logs
    mismatches but never raises.
    """
    warnings: List[str] = []
    try:
        from fashion_bot.services.product_ingestion.taxonomy_config_store import (
            aget_taxonomy_config,
        )
        tax_cfg = await aget_taxonomy_config(client_id)
        if not tax_cfg:
            return warnings

        subcategory_mapping = tax_cfg.get("subcategory_mapping") or {}
        if not subcategory_mapping:
            return warnings
        canonical_subcats = set(subcategory_mapping.keys())

        # Load the prompts we just wrote and extract subcategory values
        for agent_name in created_agents:
            if agent_name not in ("product_attribute_extractor", "query_understanding"):
                continue
            try:
                from fashion_bot.utils.utils import aget_agent_prompt_with_caching
                prompt_text = await aget_agent_prompt_with_caching(client_id, agent_name)
                if not prompt_text:
                    continue

                # Extract subcategory = 'value' patterns (used in QU filters)
                filter_subcats = set(
                    re.findall(r"subcategory\s*=\s*['\"]([^'\"]+)['\"]", prompt_text)
                )
                # Extract "subcategory": "value" patterns (used in extractor JSON)
                json_subcats = set(
                    re.findall(r'"subcategory":\s*"([^"]+)"', prompt_text)
                )
                # Merge and compare
                mentioned = (filter_subcats | json_subcats) - {"...", "null", ""}
                unknown = mentioned - canonical_subcats
                if unknown:
                    msg = (
                        f"⚠️ {agent_name} mentions subcategories not in taxonomy config: "
                        f"{sorted(unknown)} (canonical: {sorted(canonical_subcats)})"
                    )
                    logger.warning(msg)
                    warnings.append(msg)
                else:
                    logger.info(
                        f"  ✅ {agent_name}: all mentioned subcategories match taxonomy config"
                    )
            except Exception as exc:
                logger.debug(f"Cross-validation skipped for {agent_name}: {exc}")
    except Exception as exc:
        logger.debug(f"Cross-validation skipped entirely: {exc}")
    return warnings


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def ensure_personalized_prompts(
    client_id: str,
    domain: str,
    business_type: str = "",
    force: bool = False,
    business_categories: Optional[List[str]] = None,
    raw_products: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """
    Check which of the 3 specialized prompts are missing for this client
    and generate them using Groovee's prompts as structural templates.

    Args:
        business_categories: Categories detected from website scraping during
            onboarding. Merged into the catalog taxonomy so the LLM knows about
            ALL categories even if the product sample is incomplete.
        raw_products: Pre-fetched Shopify product dicts.  When supplied the
            expensive paginated ``/products.json`` fetch is skipped.

    Returns a dict with 'created' and 'skipped' lists.
    """
    result = {"created": [], "skipped": [], "errors": []}

    existing = _fetch_existing_agent_names(client_id)
    agents_needed = []
    for name in SPECIALIZED_AGENTS:
        if name in existing and not force:
            result["skipped"].append(name)
            logger.info(f"  ⏭️  {name} already exists for {client_id}")
        else:
            agents_needed.append(name)

    if not agents_needed:
        logger.info(f"All specialized prompts already exist for {client_id}")
        return result

    # Step 1: Discover catalog taxonomy
    logger.info(f"🔍 Discovering catalog taxonomy from {domain}...")
    catalog, raw_products = await discover_catalog_taxonomy(
        domain, client_id, raw_products=raw_products,
    )
    if catalog.get("products_sampled", 0) == 0:
        msg = f"No products found at {domain} — cannot generate personalized prompts"
        logger.warning(msg)
        result["errors"].append(msg)
        return result

    # Merge business categories from website scraping into catalog taxonomy
    if business_categories:
        _merge_business_categories(catalog, business_categories)

    logger.info(
        f"📋 Catalog: {catalog['products_sampled']} products, "
        f"{len(catalog.get('product_types', {}))} types, "
        f"{len(catalog.get('brands', []))} brands"
    )

    # Resolve client name (needed for taxonomy discovery and prompt generation)
    client_name = domain.split(".")[0].replace("my", "").replace("www", "").title()
    try:
        info = _fetch_client_name(client_id)
        if info:
            client_name = info
    except Exception as exc:
        logger.warning(f"⚠️ Failed to fetch client name for {client_id}: {exc}")

    # Step 1b: Discover taxonomy via LLM and persist to client_taxonomy_config
    logger.info(f"📝 Discovering and persisting taxonomy config for {client_id}...")
    tax_ok = await _persist_taxonomy_config(
        client_id, raw_products, domain,
        store_name=client_name, business_type=business_type,
    )
    if not tax_ok:
        result["errors"].append("Failed to persist taxonomy config (non-fatal)")

    # Step 1c: Load persisted taxonomy and inject into catalog so
    # _generate_prompt_via_llm sees the actual indexed field values
    # (segments, categories, subcategories) when generating the QU prompt.
    if tax_ok:
        try:
            from fashion_bot.services.product_ingestion.taxonomy_config_store import (
                aget_taxonomy_config,
            )
            tax_cfg = await aget_taxonomy_config(client_id)
            if tax_cfg:
                catalog["ingestion_taxonomy"] = {
                    "segments": tax_cfg.get("segments") or [],
                    "categories": tax_cfg.get("categories") or [],
                    "subcategory_mapping": tax_cfg.get("subcategory_mapping") or {},
                }
                logger.info(
                    f"  📎 Injected ingestion_taxonomy into catalog context: "
                    f"segments={catalog['ingestion_taxonomy']['segments']}, "
                    f"categories={catalog['ingestion_taxonomy']['categories']}"
                )
        except Exception as exc:
            logger.warning(
                f"⚠️ Failed to load taxonomy config for catalog injection: {exc}"
            )

    # Step 2: Generate each missing prompt
    for agent_name in agents_needed:
        try:
            logger.info(f"  🧠 Generating {agent_name} prompt...")
            reference = _load_template_prompt(agent_name)
            adapted = await _generate_prompt_via_llm(
                agent_name, reference, catalog,
                client_name, domain, business_type,
            )

            if not adapted or len(adapted) < 200:
                msg = f"LLM returned empty/short prompt for {agent_name}"
                logger.warning(msg)
                result["errors"].append(msg)
                continue

            _upsert_agent(client_id, agent_name, adapted)
            result["created"].append(agent_name)
            logger.info(f"  ✅ {agent_name} created ({len(adapted)} chars)")

            if agent_name in ("query_understanding", "recommendations_handler"):
                logger.info(
                    f"\n{'='*60}\n"
                    f"  GENERATED {agent_name} for {client_name}\n"
                    f"{'='*60}\n"
                    f"{adapted}\n"
                    f"{'='*60}"
                )

            # Invalidate Redis cache
            _invalidate_prompt_cache(client_id, agent_name)

        except Exception as e:
            msg = f"Failed to generate {agent_name}: {e}"
            logger.error(msg, exc_info=True)
            result["errors"].append(msg)

    # Step 3: Cross-validate generated prompts against taxonomy config
    if result["created"]:
        validation_warnings = await _cross_validate_taxonomy_vs_prompts(
            client_id, result["created"],
        )
        if validation_warnings:
            result.setdefault("warnings", []).extend(validation_warnings)

    return result


def _fetch_client_name(client_id: str) -> Optional[str]:
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT "name" FROM clients WHERE id = %s', (client_id,),
            )
            row = cur.fetchone()
            return row["name"] if row else None


def _invalidate_prompt_cache(client_id: str, agent_name: str):
    """Best-effort Redis cache invalidation."""
    try:
        from fashion_bot.config_manager import _get_redis_client
        redis = _get_redis_client()
        if redis:
            redis.delete(f"agents_config:{client_id}")
            redis.delete(f"mem_prompt:{client_id}:{agent_name}")
    except Exception:
        try:
            from fashion_bot.utils.utils import _get_redis_client
            client = _get_redis_client()
            if client:
                client.delete(f"agents_config:{client_id}")
                client.delete(f"mem_prompt:{client_id}:{agent_name}")
        except Exception:
            pass
