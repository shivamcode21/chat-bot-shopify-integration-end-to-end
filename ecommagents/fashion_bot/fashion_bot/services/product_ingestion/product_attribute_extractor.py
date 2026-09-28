"""
Product Attribute Extractor - Uses Gemini 2.5 Flash Lite to extract structured
product attributes from Shopify product data for better vector search accuracy.

Extracts:
- base_product_name: Core product name without model/variant/color/size
- product_line: Specific model, sub-category, or product line variant
- color: Color variant
- size: Size variant
- material: Material type (e.g., "leather", "silicone", "cotton")

Uses LLM at ingestion time (batch, offline) for generic extraction across any
product type — phone cases, fashion, jewelry, skincare, etc.
"""

import json
import logging
import asyncio
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

from fashion_bot.monitoring.otel_metrics import llm_caller, request_client_id

logger = logging.getLogger(__name__)

# Disable LangSmith tracing for product ingestion LLM calls to control costs.
# Passing callbacks=[] at invocation time is async-safe and does not affect
# conversation-handling LLM calls sharing the same process.
_NO_TRACE_CONFIG: Dict[str, Any] = {"callbacks": []}

# Agent name used to store/retrieve this prompt from the agents_config table
AGENT_NAME = "product_attribute_extractor"

# OTel `caller` label applied to every llm.* metric emitted by this extractor.
# Must wrap each ``ainvoke`` call (see comments at the call sites) because the
# LLM instance is lazy-initialised once per ProductAttributeExtractor and then
# reused across many async task contexts — set_llm_caller() runs only at lazy-
# init time, in whatever context first touched the property, and the
# ContextVar value is "unknown" in every subsequent task's context. Wrapping
# at call time forces the correct tag in the actual emit context.
_LLM_CALLER = "product_ingestion"


@dataclass
class ExtractedAttributes:
    """Structured product attributes extracted by LLM."""
    base_product_name: str = ""
    product_line: Optional[str] = None
    product_line_normalized: Optional[str] = None
    color: Optional[str] = None
    size: Optional[str] = None
    material: Optional[str] = None
    # Recommendation engine fields
    occasion: List[str] = field(default_factory=list)
    style: List[str] = field(default_factory=list)
    vibe: List[str] = field(default_factory=list)
    pairing_tags: List[str] = field(default_factory=list)
    category: Optional[str] = None
    subcategory: Optional[str] = None
    segment: Optional[str] = None
    color_family: Optional[str] = None
    pattern: Optional[str] = None
    fit: Optional[str] = None
    extraction_success: bool = False
    error: Optional[str] = None


# The default extraction prompt — used as fallback when no prompt is configured
# in the agents_config table for a given client_id.
# Allowed values for taxonomy-driven fields (category, occasion, style, vibe,
# segment, color_family, pattern, fit, pairing_tags) are injected via the
# TAXONOMY addendum appended by _aget_taxonomy_addendum — not hardcoded here.
DEFAULT_EXTRACTION_PROMPT = """You are a product data extraction assistant. Given product details from an e-commerce store, extract structured attributes for search indexing and recommendations.

Extract the following fields:
1. **base_product_name**: Core product name WITHOUT model/variant/color/size/material qualifiers.
2. **product_line**: Specific model, device, series, or sub-category. For phone cases = phone model. For fashion = fit/line name. null if N/A.
3. **color**: Color variant. null if not specified.
4. **size**: Size variant. null if not specified.
5. **material**: Primary material (e.g., "leather", "silicone", "cotton", "satin"). null if not specified.
6. **category**: Normalized product category. Use allowed values from TAXONOMY section below. Infer from title/tags/category.
7. **subcategory**: More specific type within the category. Use subcategory mapping from TAXONOMY section if provided. null if unclear.
8. **occasion**: Array of occasions this product suits. Use allowed values from TAXONOMY section. Return [] if unclear.
9. **style**: Array of style descriptors. Use allowed values from TAXONOMY section. Return [] if unclear.
10. **vibe**: Array of aesthetic vibes. Use allowed values from TAXONOMY section. Return [] if unclear.
11. **pairing_tags**: Array of items that pair well with this product. Use allowed values from TAXONOMY section. Return [] if unclear.
12. **segment**: Gender segment. Use allowed values from TAXONOMY section. Infer from title/tags/collections/metafields. IMPORTANT: If the title, tags, or a metafield (e.g. gender="Women"/"Men") explicitly states the gender, always use that — never assign "unisex" when the gender is clearly stated. null if genuinely unclear.
13. **color_family**: Normalized color group. Use allowed values from TAXONOMY section. null if unclear.
14. **pattern**: Pattern type. Use allowed values from TAXONOMY section. null if unclear.
15. **fit**: Fit type. Use allowed values from TAXONOMY section. null if not applicable.

RULES:
- Normalize all string values to lowercase.
- For product_line: Be PRECISE — closely named variants must be distinguished exactly.
- For arrays (occasion, style, vibe, pairing_tags): return empty array [] if you can't determine any values.
- If a scalar field cannot be determined, return null.
- ALWAYS use the allowed values from the TAXONOMY section appended below. Do not invent new values.
- Image Text is OCR'd from the product's gallery + metafield images (may be noisy). Use it as a supplementary signal — especially for material, vibe, occasion, subcategory — but PREFER Title/Tags/Description when sources conflict.
- For **segment**: explicit gender signals (e.g. "Women" or "Men" in the title, a tag like "Women Topwear", or a metafield like gender="Women") ALWAYS override generic style signals. Use "unisex" ONLY when no gender signal is present at all.

Product Details:
- Title: "{title}"
- Vendor: "{vendor}"
- Collections: {collections}
- Tags: {tags}
- Category: "{category}"
- Description (first 200 chars): "{description}"
- Template Suffix: "{template_suffix}"
- Options: {options}
- Available Colors: {available_colors}
- Metafields: {metafield_hints}
- Image Text (OCR'd from product gallery + metafield images, may be noisy): "{image_text_excerpt}"

Return ONLY valid JSON, no markdown, no explanation:
{{"base_product_name": "...", "product_line": null, "color": null, "size": null, "material": null, "category": "...", "subcategory": null, "occasion": [], "style": [], "vibe": [], "pairing_tags": [], "segment": null, "color_family": null, "pattern": null, "fit": null}}"""


async def aget_extraction_prompt(client_id: Optional[str] = None) -> str:
    """
    Async: get the product attribute extraction prompt via async tiered caching.
    """
    if not client_id:
        return DEFAULT_EXTRACTION_PROMPT

    try:
        from fashion_bot.utils.utils import aget_agent_prompt_with_caching

        db_prompt = await aget_agent_prompt_with_caching(client_id, AGENT_NAME)
        if db_prompt:
            logger.info(
                f"📖 Async loaded {AGENT_NAME} prompt from DB/cache for client: {client_id} "
                f"({len(db_prompt)} chars)"
            )
            return db_prompt
        else:
            logger.debug(
                f"ℹ️ No {AGENT_NAME} prompt in DB for client: {client_id}, using default"
            )
            return DEFAULT_EXTRACTION_PROMPT
    except Exception as e:
        logger.warning(
            f"⚠️ Failed to async load {AGENT_NAME} prompt from DB for client {client_id}: {e}. "
            f"Using default prompt."
        )
        return DEFAULT_EXTRACTION_PROMPT


class ProductAttributeExtractor:
    """
    Extracts structured product attributes using Gemini 2.5 Flash Lite.
    
    Used at ingestion time to parse product titles and metadata into
    structured fields for accurate vector search filtering.
    """
    
    def __init__(self):
        """Initialize the extractor with Gemini LLM."""
        self._llm = None
    
    @property
    def llm(self):
        """Lazy initialization of Gemini LLM."""
        if self._llm is None:
            from fashion_bot.core.llm_config import (
                get_smaller_llm_config,
                BACKGROUND_OPENROUTER_KEY_ENV,
            )
            from fashion_bot.core.llm_factory import LLMFactory
            self._llm = LLMFactory.get_llm(
                tool_name="product_ingestion",
                # 800 tokens fits the full 15-field schema even when every
                # taxonomy array (occasion/style/vibe/pairing_tags) is filled;
                # 500 truncated mid-array for rich fashion catalogs and caused
                # the ValueError spike on 2026-05-27.
                # Background workload → BACKGROUND OpenRouter key (key 2).
                override_config=get_smaller_llm_config(
                    temperature=0,
                    max_tokens=800,
                    api_key_env_var=BACKGROUND_OPENROUTER_KEY_ENV,
                ),
            )
            logger.info(f"✅ ProductAttributeExtractor: Smaller LLM initialized")
        return self._llm

    async def _aget_llm(self):
        return self.llm

    # Transient upstream failure codes (Gemini/OpenRouter) seen in production —
    # 504 "operation was aborted", 500 "Internal Server Error", 503 unavailable,
    # 429 rate limit. Retried with exponential backoff before falling back.
    _RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)
    _RETRYABLE_SUBSTRINGS = (
        "aborted",
        "timeout",
        "timed out",
        "temporarily unavailable",
        "internal server error",
        "rate limit",
        "rate_limit",
        "429",
        "500",
        "502",
        "503",
        "504",
    )

    @classmethod
    def _is_retryable_error(cls, exc: BaseException) -> bool:
        """True for upstream errors worth retrying (5xx / 429 / aborted)."""
        # google-genai / openrouter wrappers stash the status on the exception.
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if isinstance(code, int) and code in cls._RETRYABLE_STATUS_CODES:
            return True
        msg = str(exc).lower()
        return any(sub in msg for sub in cls._RETRYABLE_SUBSTRINGS)

    async def _ainvoke_and_parse(
        self,
        prompt: str,
        *,
        max_attempts: int = 3,
    ) -> Dict[str, Any]:
        """Invoke the LLM and parse the JSON, with bounded retries.

        Retries on transient upstream failures (5xx / 429 / aborted) and on
        ValueError raised by ``_parse_llm_response`` — at temperature 0 a
        re-roll rarely changes the output, but truncation is sometimes caused
        by an upstream cutoff rather than the token cap, in which case the
        retry succeeds. Backoff: 1s, 2s, 4s, capped, with small jitter.
        """
        import random

        llm = await self._aget_llm()
        last_exc: Optional[BaseException] = None
        for attempt in range(1, max_attempts + 1):
            try:
                # llm_caller wrapper: see _LLM_CALLER comment at module top.
                with llm_caller(_LLM_CALLER):
                    response = await llm.ainvoke(prompt, config=_NO_TRACE_CONFIG)
                response_text = (
                    response.content if hasattr(response, "content") else str(response)
                )
                return self._parse_llm_response(response_text)
            except ValueError as exc:
                # Parse failure — one retry only; re-rolling rarely helps at temp=0.
                last_exc = exc
                if attempt >= 2:
                    raise
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts or not self._is_retryable_error(exc):
                    raise
            delay = min(2 ** (attempt - 1), 4) + random.uniform(0, 0.25)
            logger.info(
                f"🔁 LLM extraction retry {attempt}/{max_attempts - 1} after {delay:.2f}s: {last_exc}"
            )
            await asyncio.sleep(delay)
        # Defensive — loop always returns or raises above.
        assert last_exc is not None
        raise last_exc
    
    async def _aget_taxonomy_addendum(self, client_id: Optional[str]) -> str:
        """Build the taxonomy block appended to every extraction prompt.

        Loads from ``client_taxonomy_config`` for the given client.  When no
        client-specific row exists (or client_id is None), falls back to the
        built-in defaults from ``taxonomy_config_store.get_defaults()``.
        This ensures the LLM always receives explicit allowed values.
        """
        from fashion_bot.services.product_ingestion.taxonomy_config_store import (
            get_defaults,
        )

        cfg = None
        source = "defaults"
        if client_id:
            try:
                from fashion_bot.services.product_ingestion.taxonomy_config_store import (
                    aget_taxonomy_config,
                )
                cfg = await aget_taxonomy_config(client_id)
                if cfg:
                    source = "client_taxonomy_config"
            except Exception as exc:
                logger.debug(f"Taxonomy config lookup failed, using defaults: {exc}")

        if not cfg:
            cfg = get_defaults()

        label = "CLIENT-SPECIFIC" if source == "client_taxonomy_config" else "DEFAULT"
        parts = [f"\n\n--- {label} TAXONOMY (use ONLY these values for the corresponding fields) ---"]
        if cfg.get("categories"):
            parts.append(f"Allowed categories: {json.dumps(cfg['categories'])}")
        if cfg.get("subcategory_mapping"):
            subcategory_names = sorted(cfg["subcategory_mapping"].keys())
            parts.append(f"Allowed subcategories: {json.dumps(subcategory_names)}")
            parts.append(f"Subcategory mapping (subcategory → parent category): {json.dumps(cfg['subcategory_mapping'])}")
            parts.append(
                "OVERRIDE: For the 'subcategory' field, use ONLY the values listed in "
                "'Allowed subcategories' above. If the prompt body lists different "
                "subcategory values, ignore those and use these instead."
            )
        if cfg.get("attribute_schema"):
            parts.append(f"Per-subcategory attributes: {json.dumps(cfg['attribute_schema'])}")
        if cfg.get("occasions"):
            parts.append(f"Allowed occasions: {json.dumps(cfg['occasions'])}")
        if cfg.get("styles"):
            parts.append(f"Allowed styles: {json.dumps(cfg['styles'])}")
        if cfg.get("vibes"):
            parts.append(f"Allowed vibes: {json.dumps(cfg['vibes'])}")
        if cfg.get("pairing_tags"):
            parts.append(f"Allowed pairing_tags: {json.dumps(cfg['pairing_tags'])}")
        if cfg.get("segments"):
            parts.append(f"Allowed segments: {json.dumps(cfg['segments'])}")
        if cfg.get("color_families"):
            parts.append(f"Allowed color families: {json.dumps(cfg['color_families'])}")
        if cfg.get("patterns"):
            parts.append(f"Allowed patterns: {json.dumps(cfg['patterns'])}")
        if cfg.get("fits"):
            parts.append(f"Allowed fits: {json.dumps(cfg['fits'])}")
        parts.append("--- END TAXONOMY ---")
        return "\n".join(parts)

    async def _abuild_prompt(self, product: Dict[str, Any], client_id: Optional[str] = None) -> str:
        """Async version of _build_prompt — uses async prompt loading."""
        title = product.get("title", "Unknown Product")
        collections = []
        collections_data = product.get("collections", [])
        if collections_data:
            for c in collections_data:
                if isinstance(c, dict):
                    collections.append(c.get("title", ""))
                elif isinstance(c, str):
                    collections.append(c)
        tags = product.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        category = ""
        category_data = product.get("category", {})
        if isinstance(category_data, dict):
            category = category_data.get("fullName", category_data.get("name", ""))
        elif isinstance(category_data, str):
            category = category_data
        description = product.get("description", "")
        if not description:
            description = product.get("body_html", "")
        if description and "<" in description:
            import re
            description = re.sub(r'<[^>]+>', ' ', description)
            description = re.sub(r'\s+', ' ', description).strip()
        description = description[:200]
        template_suffix = product.get("templateSuffix", product.get("template_suffix", ""))
        options = []
        for opt in product.get("options", []):
            if isinstance(opt, dict):
                opt_name = opt.get("name", "")
                opt_values = opt.get("values", [])
                if opt_name.lower() != "title":
                    options.append({"name": opt_name, "values": opt_values})

        vendor = product.get("vendor", "")
        available_colors = product.get("available_colors", [])
        metafield_hints = product.get("metafield_hints", {})
        image_text_excerpt = product.get("image_text_excerpt", "") or ""

        prompt_template = await aget_extraction_prompt(client_id)

        format_kwargs = {
            "title": title,
            "collections": json.dumps(collections),
            "tags": json.dumps(tags),
            "category": category,
            "description": description,
            "template_suffix": template_suffix,
            "options": json.dumps(options),
            "vendor": vendor,
            "available_colors": json.dumps(available_colors),
            "metafield_hints": json.dumps(metafield_hints),
            "image_text_excerpt": image_text_excerpt,
        }

        # DB-stored prompts may contain unescaped curly braces (e.g. JSON
        # examples in [OUTPUT FORMAT]) which break str.format(). Use explicit
        # placeholder replacement so only known {key} tokens are substituted
        # and literal braces in the rest of the template are left untouched.
        base_prompt = prompt_template
        for key, value in format_kwargs.items():
            base_prompt = base_prompt.replace("{" + key + "}", str(value))

        taxonomy_addendum = await self._aget_taxonomy_addendum(client_id)
        return base_prompt + taxonomy_addendum

    def _parse_llm_response(self, response_text: str) -> Dict[str, Any]:
        """
        Parse LLM response text into structured dict.

        Handles common LLM output quirks (markdown wrapping, extra text) and,
        when the response is truncated mid-JSON (the ``max_tokens`` cap was
        hit), repairs it by dropping the partial trailing entry and closing
        any unclosed string / array / object.
        """
        text = response_text.strip()

        # Strip markdown code block if present
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Locate the outermost object; truncated responses still start with `{`.
        start = text.find("{")
        if start < 0:
            raise ValueError(
                f"Could not parse LLM response as JSON (no object found): {text[:200]}"
            )
        repaired = self._repair_truncated_json(text[start:])
        if repaired is not None:
            return repaired
        raise ValueError(f"Could not parse LLM response as JSON: {text[:200]}")

    @staticmethod
    def _repair_truncated_json(text: str) -> Optional[Dict[str, Any]]:
        """Best-effort recovery of a JSON object truncated mid-stream.

        Strategy: walk the string tracking string/escape state and the
        bracket stack. Remember the position right after the last complete
        top-level entry (a ``,`` outside any string at depth 1). Cut the
        text there, then append the closing brackets needed by the stack.

        Returns the parsed dict, or ``None`` if recovery isn't possible.
        """
        in_string = False
        escape = False
        stack: List[str] = []
        # Last index (exclusive) of a safely-truncatable prefix. Initialised
        # to the position just after the opening `{` so an object that was
        # truncated before any complete entry still parses as ``{}``.
        safe_end = 0

        for i, ch in enumerate(text):
            if escape:
                escape = False
                continue
            if in_string:
                if ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
                if ch == "{" and len(stack) == 1:
                    safe_end = i + 1
            elif ch in "}]":
                if stack and stack[-1] == ch:
                    stack.pop()
            elif ch == "," and len(stack) == 1:
                # End of a complete top-level entry; safe to truncate here.
                safe_end = i

        prefix = text[:safe_end]
        # Rebuild the stack for the truncated prefix so we know what to close.
        in_string = False
        escape = False
        close_stack: List[str] = []
        for ch in prefix:
            if escape:
                escape = False
                continue
            if in_string:
                if ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch in "{[":
                close_stack.append("}" if ch == "{" else "]")
            elif ch in "}]" and close_stack and close_stack[-1] == ch:
                close_stack.pop()

        if in_string:
            # Shouldn't happen after truncating at a top-level comma, but
            # bail out rather than emit invalid JSON.
            return None
        candidate = prefix + "".join(reversed(close_stack))
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    
    async def extract_attributes(self, product: Dict[str, Any], client_id: Optional[str] = None) -> ExtractedAttributes:
        """
        Extract structured attributes from a single product (async).

        Args:
            product: Shopify product dict
            client_id: Optional client ID for loading client-specific prompt

        Returns:
            ExtractedAttributes with extracted fields
        """
        try:
            prompt = await self._abuild_prompt(product, client_id=client_id)
            parsed = await self._ainvoke_and_parse(prompt)

            product_line = parsed.get("product_line")
            product_line_normalized = product_line.strip().lower() if product_line else None

            result = ExtractedAttributes(
                base_product_name=parsed.get("base_product_name", "").strip().lower(),
                product_line=product_line,
                product_line_normalized=product_line_normalized,
                color=parsed.get("color"),
                size=parsed.get("size"),
                material=parsed.get("material"),
                occasion=parsed.get("occasion") or [],
                style=parsed.get("style") or [],
                vibe=parsed.get("vibe") or [],
                pairing_tags=parsed.get("pairing_tags") or [],
                category=parsed.get("category"),
                subcategory=parsed.get("subcategory"),
                segment=parsed.get("segment"),
                color_family=parsed.get("color_family"),
                pattern=parsed.get("pattern"),
                fit=parsed.get("fit"),
                extraction_success=True,
            )

            logger.debug(
                f"✅ Extracted: title='{product.get('title', '')}' → "
                f"base='{result.base_product_name}', line='{result.product_line}', "
                f"color='{result.color}', material='{result.material}'"
            )
            return result

        except Exception as e:
            logger.warning(
                f"⚠️ Attribute extraction failed for '{product.get('title', 'unknown')}': {e}"
            )
            return ExtractedAttributes(
                base_product_name=product.get("title", "").lower(),
                extraction_success=False,
                error=str(e),
            )
    
    async def extract_attributes_async(self, product: Dict[str, Any], client_id: Optional[str] = None) -> ExtractedAttributes:
        """
        Native async attribute extraction using the model's async invoke path.
        
        Args:
            product: Shopify product dict
            client_id: Optional client ID for loading client-specific prompt
            
        Returns:
            ExtractedAttributes with extracted fields
        """
        try:
            prompt = await self._abuild_prompt(product, client_id=client_id)
            parsed = await self._ainvoke_and_parse(prompt)

            product_line = parsed.get("product_line")
            product_line_normalized = product_line.strip().lower() if product_line else None

            return ExtractedAttributes(
                base_product_name=parsed.get("base_product_name", "").strip().lower(),
                product_line=product_line,
                product_line_normalized=product_line_normalized,
                color=parsed.get("color"),
                size=parsed.get("size"),
                material=parsed.get("material"),
                occasion=parsed.get("occasion") or [],
                style=parsed.get("style") or [],
                vibe=parsed.get("vibe") or [],
                pairing_tags=parsed.get("pairing_tags") or [],
                category=parsed.get("category"),
                subcategory=parsed.get("subcategory"),
                segment=parsed.get("segment"),
                color_family=parsed.get("color_family"),
                pattern=parsed.get("pattern"),
                fit=parsed.get("fit"),
                extraction_success=True,
            )
        except Exception as e:
            logger.warning(
                f"⚠️ Async attribute extraction failed for '{product.get('title', 'unknown')}': {e}"
            )
            return ExtractedAttributes(
                base_product_name=product.get("title", "").lower(),
                extraction_success=False,
                error=str(e),
            )
    
    async def extract_batch_async(
        self,
        products: List[Dict[str, Any]],
        max_concurrent: int = 10,
        client_id: Optional[str] = None,
    ) -> List[ExtractedAttributes]:
        """
        Extract attributes for a batch of products concurrently.
        
        Args:
            products: List of Shopify product dicts
            max_concurrent: Maximum concurrent LLM calls
            client_id: Optional client ID for loading client-specific prompt
            
        Returns:
            List of ExtractedAttributes (same order as input)
        """
        semaphore = asyncio.Semaphore(max_concurrent)

        async def extract_with_semaphore(product):
            async with semaphore:
                # Tag client_id on OTel baggage so llm_calls_total / llm_errors_total
                # carry the right `client_id` label for this batch (otherwise they
                # report `unknown` for ingestion-time extraction).
                with request_client_id(client_id):
                    return await self.extract_attributes_async(product, client_id=client_id)
        
        prompt_source = "DB/cache" if client_id else "default"
        try:
            test_prompt = await aget_extraction_prompt(client_id)
            prompt_source = f"DB/cache ({len(test_prompt)} chars)" if client_id else f"default ({len(test_prompt)} chars)"
        except Exception as exc:
            logger.warning(f"⚠️ Failed to load extraction prompt: {exc}")
        logger.info(
            f"🔄 Extracting attributes for {len(products)} products "
            f"(max_concurrent={max_concurrent}, prompt={prompt_source})"
        )
        
        tasks = [extract_with_semaphore(p) for p in products]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Convert exceptions to failed ExtractedAttributes
        final_results = []
        success_count = 0
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(f"⚠️ Extraction failed for product {i}: {result}")
                final_results.append(ExtractedAttributes(
                    base_product_name=products[i].get("title", "").lower(),
                    extraction_success=False,
                    error=str(result),
                ))
            else:
                final_results.append(result)
                if result.extraction_success:
                    success_count += 1
        
        logger.info(f"✅ Attribute extraction complete: {success_count}/{len(products)} succeeded")
        return final_results
    


# Module-level singleton
_extractor_instance: Optional[ProductAttributeExtractor] = None


def get_product_attribute_extractor() -> ProductAttributeExtractor:
    """Get or create the singleton ProductAttributeExtractor instance."""
    global _extractor_instance
    if _extractor_instance is None:
        _extractor_instance = ProductAttributeExtractor()
    return _extractor_instance
