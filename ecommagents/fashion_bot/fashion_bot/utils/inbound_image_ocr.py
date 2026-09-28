"""
Inbound WhatsApp image analysis — OCR text extraction + product recognition.

Stateless async utility (per AGENTS.md §2: tools must not mutate state).
Uses Gemini 2.5 Flash Lite via OpenRouter (background key 2) to analyze
a customer-sent image in a single LLM call that handles both:

1. **OCR**: Extract readable text (order screenshots, payment confirmations).
2. **Product vision**: Describe the product if it's a product photo, extracting
   structured attributes (category, subcategory, color, etc.) aligned with the
   existing taxonomy so the downstream search pipeline can find matches.

Returns ``InboundImageAnalysis`` on success, ``None`` on any failure (fail-open).

NOT related to ``product_image_ocr_extractor.py`` — that module is tightly
coupled to the product ingestion pipeline (tiered caching, Shopify CDN
filtering, per-product summary). This module is a one-shot, no-cache,
different-prompt operation for inbound customer images.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from fashion_bot.config_manager import aget_config
from fashion_bot.monitoring.otel_metrics import llm_caller

logger = logging.getLogger("inbound_image_ocr")

_NO_TRACE_CONFIG: Dict[str, Any] = {"callbacks": []}

_LLM_CALLER = "inbound_image_ocr"

_OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY_2"
_OPENROUTER_MODEL = "google/gemini-2.5-flash-lite"
_MAX_OUTPUT_TOKENS = 1024

# A warm vision call measured ~3.5s end-to-end in production, so the original
# 5s budget left ~1.5s of headroom and the first call on a fresh process (LLM
# client construction + first TLS handshake to OpenRouter) blew straight
# through it. Override with INBOUND_OCR_TIMEOUT_SECONDS to retune without a
# deploy. The cost of a wider budget is paid only on the failure path: the
# customer waits this long before the media-unsupported reply.
_DEFAULT_TIMEOUT_SECONDS = 10.0

# When a product photo also carries text, the product description is what the
# catalog can actually be searched with — the printed text is decoration on the
# garment, not the query. It is still worth passing along as context, capped so
# a slogan-covered tee cannot bury the description it is attached to.
MAX_OCR_CONTEXT_CHARS = 200


_AGENT_NAME = "inbound_image_analysis"


@dataclass
class InboundImageAnalysis:
    """Result of analyzing an inbound customer image."""
    ocr_text: Optional[str] = None
    is_product_image: bool = False
    product_summary: Optional[str] = None
    product_attributes: Optional[Dict[str, str]] = None

    def prefer_ocr(self) -> bool:
        """Whether the OCR text, rather than the product summary, is the message.

        A recognisable product with a usable description is a product query,
        however much text happens to be printed on it. Searching a catalog for
        "ONE WITH ALL EXISTENCE / 戰 / 愛" finds nothing; searching it for "red
        oversized t-shirt with abstract print" finds the shirt that slogan is
        printed on. The text is not lost either way — when the product leads,
        the OCR goes along as context (see ``ocr_context``).

        Everything else — screenshots, receipts, order confirmations, and
        product photos the model could not describe — is an OCR message.
        """
        if not self.ocr_text:
            return False
        return not (self.is_product_image and self.product_summary)

    def ocr_context(self) -> Optional[str]:
        """Text printed on the item, to accompany a product-led message.

        ``None`` when there is no OCR text or when the OCR text *is* the
        message, so the caller never repeats itself.
        """
        if not self.ocr_text or self.prefer_ocr():
            return None
        collapsed = " ".join(self.ocr_text.split())
        if not collapsed:
            return None
        if len(collapsed) > MAX_OCR_CONTEXT_CHARS:
            collapsed = collapsed[:MAX_OCR_CONTEXT_CHARS].rstrip() + "…"
        return collapsed


_DEFAULT_IMAGE_ANALYSIS_PROMPT = """You are analyzing a customer-sent image for an e-commerce support chatbot.

Analyze this image and return ONLY a JSON object with these fields:

{
  "has_text": true or false,
  "text": "extracted text here or empty string",
  "is_product_image": true or false,
  "product_description": {
    "category": "product category",
    "subcategory": "specific product type",
    "color": "primary color",
    "segment": "target segment",
    "material": "material if identifiable",
    "pattern": "pattern type (apparel/textiles only)",
    "style": "style type (apparel/textiles only)",
    "fit": "fit type (apparel/footwear only)",
    "summary": "a short search-friendly phrase describing the product"
  }
}

Rules:
- "has_text": true ONLY if the image contains readable text (screenshots, receipts, labels, order confirmations).
- "text": extracted text if has_text is true. Preserve numbers, order IDs, prices, dates, URLs exactly. Empty string if no text.
- "is_product_image": true if the image shows a physical product a retailer could sell — apparel, footwear, accessories, beauty, electronics, home goods, packaged food, or anything else a store might stock. Do not restrict this to any one category of goods.
- "product_description": fill ONLY when is_product_image is true. Set to null otherwise.
- "category", "subcategory", "color" and "summary" apply to every kind of product. "material", "pattern", "style" and "fit" are meaningful only for some — use an empty string whenever a field does not apply to the product you can see, and never stretch a garment term to cover a non-garment product.
- "segment": the shopper this product is aimed at, when the image makes that clear. Empty string when it does not.
- "summary": must be a concise, natural-language phrase a shopper might type to search for this product, in the vocabulary that product's own category uses — e.g. "black oversized cotton t-shirt for men", "stainless steel insulated water bottle 1 litre", "wireless over-ear noise cancelling headphones".
- Do NOT invent details you cannot see. Use empty string for fields you cannot determine.
- Return ONLY JSON, no markdown wrapping, no commentary.
"""


async def _aget_prompt(client_id: str) -> str:
    """Load the image analysis prompt: agents_config DB → default constant."""
    try:
        from fashion_bot.utils.utils import aget_agent_prompt_with_caching

        db_prompt = await aget_agent_prompt_with_caching(client_id, _AGENT_NAME)
        if db_prompt:
            logger.debug(
                "[INBOUND_OCR] loaded %s prompt from DB/cache for client=%s (%d chars)",
                _AGENT_NAME, client_id, len(db_prompt),
            )
            return db_prompt
    except Exception as exc:
        logger.debug("[INBOUND_OCR] DB prompt load failed, using default: %s", exc)

    return _DEFAULT_IMAGE_ANALYSIS_PROMPT


async def _abuild_taxonomy_addendum(client_id: str) -> str:
    """Build a taxonomy block appended to the prompt with client-specific allowed values."""
    try:
        from fashion_bot.services.product_ingestion.taxonomy_config_store import (
            aget_taxonomy_config,
            get_defaults,
        )

        cfg = None
        if client_id:
            cfg = await aget_taxonomy_config(client_id)
        if not cfg:
            cfg = get_defaults()
    except Exception as exc:
        logger.debug("[INBOUND_OCR] taxonomy lookup failed, skipping addendum: %s", exc)
        return ""

    parts = ["\n--- ALLOWED TAXONOMY VALUES (use ONLY these for the corresponding fields) ---"]
    if cfg.get("categories"):
        parts.append(f"Allowed categories: {json.dumps(cfg['categories'])}")
    if cfg.get("subcategory_mapping"):
        subcategory_names = sorted(cfg["subcategory_mapping"].keys())
        parts.append(f"Allowed subcategories: {json.dumps(subcategory_names)}")
    if cfg.get("segments"):
        parts.append(f"Allowed segments: {json.dumps(cfg['segments'])}")
    if cfg.get("color_families"):
        parts.append(f"Allowed colors: {json.dumps(cfg['color_families'])}")
    if cfg.get("patterns"):
        parts.append(f"Allowed patterns: {json.dumps(cfg['patterns'])}")
    if cfg.get("fits"):
        parts.append(f"Allowed fits: {json.dumps(cfg['fits'])}")
    if cfg.get("styles"):
        parts.append(f"Allowed styles: {json.dumps(cfg['styles'])}")
    parts.append("--- END TAXONOMY ---")
    return "\n".join(parts)

# Lazy singleton — same pattern as product_image_ocr_extractor.
_llm = None
_use_openrouter = False


def _effective_timeout_seconds(fallback: float) -> float:
    """Analysis budget, overridable via ``INBOUND_OCR_TIMEOUT_SECONDS``.

    Falls back to the caller's value on anything unparseable or non-positive,
    so a bad env var cannot disable the timeout entirely.
    """
    try:
        from fashion_bot.env_loader import get_env

        raw = get_env("INBOUND_OCR_TIMEOUT_SECONDS")
        if raw:
            parsed = float(raw)
            if parsed > 0:
                return parsed
    except Exception:
        pass
    return fallback


def _get_llm():
    """Lazy-init the vision LLM. Prefers OpenRouter (background key 2)."""
    global _llm, _use_openrouter
    if _llm is not None:
        return _llm

    from fashion_bot.env_loader import get_env

    if get_env(_OPENROUTER_KEY_ENV):
        try:
            from fashion_bot.core.llm_config import LLMConfig, LLMProvider
            from fashion_bot.core.llm_factory import LLMFactory

            model = get_env("INBOUND_OCR_LLM_MODEL", _OPENROUTER_MODEL)
            config = LLMConfig(
                provider=LLMProvider.OPENROUTER,
                model=model,
                temperature=0.0,
                max_tokens=_MAX_OUTPUT_TOKENS,
                api_key_env_var=_OPENROUTER_KEY_ENV,
                base_url="https://openrouter.ai/api/v1",
            )
            _llm = LLMFactory._get_or_create(config)
            _use_openrouter = True
            logger.info("[INBOUND_OCR] vision LLM via OpenRouter (key 2): %s", model)
            return _llm
        except Exception as exc:
            logger.warning(
                "[INBOUND_OCR] OpenRouter init failed (%s); "
                "falling back to Gemini-direct.",
                exc,
            )

    try:
        from langchain_google_genai import ChatGoogleGenerativeAI

        api_key = get_env("GOOGLE_API_KEY")
        _llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash-lite",
            temperature=0.0,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
            google_api_key=api_key,
        )
        _use_openrouter = False
        logger.info("[INBOUND_OCR] vision LLM via Gemini-direct: gemini-2.5-flash-lite")
        return _llm
    except Exception as exc:
        logger.warning("[INBOUND_OCR] Gemini-direct init failed: %s", exc)
        return None


def _image_content_part(url: str) -> Dict[str, Any]:
    if _use_openrouter:
        return {"type": "image_url", "image_url": {"url": url}}
    return {"type": "image_url", "image_url": url}


def _parse_response(text: str) -> Dict[str, Any]:
    """Parse LLM response, tolerating markdown fences."""
    original = (text or "").strip()
    if not original:
        return {}
    try:
        return json.loads(original)
    except json.JSONDecodeError:
        pass
    # Strip markdown code fence
    if original.startswith("```"):
        lines = original.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        fenced = "\n".join(lines).strip()
        if fenced:
            try:
                return json.loads(fenced)
            except json.JSONDecodeError:
                pass
    # Greedy JSON extraction
    match = re.search(r"\{.*\}", original, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {}


async def _arun_analysis(image_url: str, client_id: str) -> Optional[InboundImageAnalysis]:
    """Run the combined OCR + product vision LLM call. Returns analysis or None."""
    from langchain_core.messages import HumanMessage

    llm = _get_llm()
    if llm is None:
        return None

    prompt = await _aget_prompt(client_id)
    taxonomy = await _abuild_taxonomy_addendum(client_id)
    full_prompt = prompt + taxonomy if taxonomy else prompt

    msg = HumanMessage(
        content=[
            {"type": "text", "text": full_prompt},
            _image_content_part(image_url),
        ]
    )
    with llm_caller(_LLM_CALLER):
        response = await llm.ainvoke([msg], config=_NO_TRACE_CONFIG)

    raw = response.content if hasattr(response, "content") else str(response)
    parsed = _parse_response(raw if isinstance(raw, str) else json.dumps(raw))

    if not parsed:
        # The model answered, but not with JSON we could read. Without the text
        # itself this is undiagnosable after the fact, so keep a bounded sample.
        logger.warning(
            "[INBOUND_OCR] unparseable model response (%d chars): %.400s",
            len(raw) if isinstance(raw, str) else -1,
            raw if isinstance(raw, str) else json.dumps(raw),
        )
        return None

    result = InboundImageAnalysis()

    has_text = _coerce_bool_flag(parsed.get("has_text", False))
    text = (parsed.get("text") or "").strip()
    if has_text and text:
        result.ocr_text = text

    is_product = _coerce_bool_flag(parsed.get("is_product_image", False))
    product_desc = parsed.get("product_description")
    if is_product and isinstance(product_desc, dict):
        summary = (product_desc.get("summary") or "").strip()
        if summary:
            result.is_product_image = True
            result.product_summary = summary
            result.product_attributes = {
                k: v for k, v in product_desc.items()
                if k != "summary" and isinstance(v, str) and v.strip()
            }

    if result.ocr_text or result.is_product_image:
        return result

    # Parsed fine and still said nothing usable: no text, or a product the model
    # would not describe. Indistinguishable from "the feature is off" downstream,
    # so record what it actually returned.
    logger.warning(
        "[INBOUND_OCR] no actionable content — has_text=%s text_len=%d "
        "is_product=%s summary=%r",
        has_text, len(text), is_product,
        (product_desc or {}).get("summary") if isinstance(product_desc, dict) else None,
    )
    return None


async def aanalyze_inbound_image(
    image_url: str,
    client_id: str,
    trace_id: Optional[str] = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> Optional[InboundImageAnalysis]:
    """Analyze an inbound WhatsApp image — combined OCR + product recognition.

    Returns ``InboundImageAnalysis`` with OCR text and/or product description,
    or ``None`` when:
    - feature flag is off for this client
    - image contains neither readable text nor a recognizable product
    - LLM call times out or fails
    - any other error

    Fail-open: caller treats ``None`` as "nothing actionable" and falls through
    to the existing media short-circuit flow.
    """
    if not image_url or not client_id:
        return None

    try:
        raw = await aget_config("inbound_image_ocr_enabled", client_id=client_id)
        enabled = _coerce_bool_flag(raw)
        if not enabled:
            return None
    except Exception:
        return None

    timeout_seconds = _effective_timeout_seconds(timeout_seconds)

    # Built outside the budget: constructing the client is bookkeeping, not the
    # work we are timing, and on a cold process it is exactly what used to eat
    # the headroom the vision call needed.
    if _get_llm() is None:
        logger.warning("[INBOUND_OCR] [%s] no vision LLM available", trace_id)
        return None

    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            _arun_analysis(image_url, client_id),
            timeout=timeout_seconds,
        )
        elapsed = time.monotonic() - started
        if result:
            parts = []
            if result.ocr_text:
                parts.append(f"OCR={len(result.ocr_text)} chars")
            if result.is_product_image:
                parts.append(f"product='{result.product_summary}'")
            logger.info(
                "[INBOUND_OCR] [%s] ✅ %s (%.2fs)",
                trace_id,
                ", ".join(parts),
                elapsed,
            )
        else:
            # Was logger.debug, which production drops entirely — a customer got
            # the "can't view images" reply with nothing in the logs to explain it.
            logger.warning(
                "[INBOUND_OCR] [%s] ⚠️ image had no actionable content (%.2fs)",
                trace_id,
                elapsed,
            )
        return result
    except asyncio.TimeoutError:
        logger.warning(
            "[INBOUND_OCR] [%s] ⏱️ analysis timed out after %.1fs",
            trace_id,
            timeout_seconds,
        )
        return None
    except Exception as exc:
        logger.warning(
            "[INBOUND_OCR] [%s] ❌ analysis failed: %s",
            trace_id,
            exc,
        )
        return None


async def aextract_text_from_inbound_image(
    image_url: str,
    client_id: str,
    trace_id: Optional[str] = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> Optional[str]:
    """Backward-compatible wrapper — returns OCR text string or None.

    Delegates to ``aanalyze_inbound_image`` and returns only the OCR text,
    ignoring product analysis. Kept so existing callers don't break.
    """
    result = await aanalyze_inbound_image(
        image_url=image_url,
        client_id=client_id,
        trace_id=trace_id,
        timeout_seconds=timeout_seconds,
    )
    return result.ocr_text if result else None


def _coerce_bool_flag(raw: Any) -> bool:
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, dict):
        return bool(raw.get("enabled"))
    if isinstance(raw, str):
        return raw.strip().lower() in {"true", "1", "yes"}
    return False
