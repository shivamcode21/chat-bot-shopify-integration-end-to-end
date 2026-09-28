"""
Per-client feature flag + selector overrides for the image OCR + shop content
pipeline. Backed by the existing ``client_configs`` table, fetched through
``config_manager.aget_config`` so the read goes through the standard
memory → Redis → Postgres tiered cache.

Config keys (under client_configs.config_key):

- ``image_ocr_enabled``  →  {"enabled": true}   (default: false)
      Master flag for the entire OCR pipeline (gallery + metafield image OCR
      AND shop-level extraction). When false, nothing runs.

- ``shop_content_extraction_enabled``  →  {"enabled": true}   (default: false)
      Sub-flag for the storefront HTML scrape (Offer banner, announcement bar).
      ONLY has effect when ``image_ocr_enabled`` is also true. Lets a client
      opt into per-image OCR without paying for the shop-content fetch (or
      vice-versa — opt in to promo capture without the cost of vision OCR).

- ``shop_content_extractor_overrides`` →
        {
          "storefront_domain": "serekoshop.com",
          "promo_selectors": [...],                 # replaces default bundle
          "additional_promo_selectors": [...],      # appended to default bundle
          "exclude_text_patterns": [...],
          "max_promo_chars": 600
        }

- ``pdp_size_chart_ocr_enabled``  →  {"enabled": true}   (default: false)
      PDP-level size chart image extraction. When enabled, the ingestion
      pipeline fetches each product's storefront PDP page, finds the size
      chart container (``<div class="size-chart">`` or ``id="size-chart"``),
      extracts ``<img>`` URLs from it, and feeds them through the OCR pipeline.
      Works **independently** of ``image_ocr_enabled``:
        * Both true → full OCR (gallery + metafield + size chart images).
        * Only this flag true → size-chart-only mode (no gallery/metafield OCR).
        * Only ``image_ocr_enabled`` true → no size chart scraping.
      NOT used on the webhook hot path — only during full/delta ingestion.

- ``pdp_size_chart_overrides`` →
        {
          "selectors": ["div.size-chart", "#size-chart"],
          "storefront_domain": "www.example.com"
        }
"""

import logging
from typing import Any, Dict, List, Optional

from fashion_bot.config_manager import aget_config

logger = logging.getLogger(__name__)


# Ships with the codebase; covers the most common Shopify theme classes.
DEFAULT_PROMO_SELECTORS: List[str] = [
    "[data-announce] p",
    "[data-announce] .text-announce p",
    ".announcement-bar__item",
    ".announcement-bar__item .announce_in",
    ".promo-banner",
    ".free-gift-bar",
    ".header__announcement",
    ".announcement",
]


def _coerce_bool_flag(raw: Any) -> bool:
    """Interpret a tiered-cache config value as a boolean enable flag."""
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, dict):
        return bool(raw.get("enabled"))
    if isinstance(raw, str):
        return raw.strip().lower() in {"true", "1", "yes"}
    return False


async def ais_image_ocr_enabled(client_id: str) -> bool:
    """Return True iff the client has explicitly enabled image OCR ingestion."""
    if not client_id:
        return False
    try:
        raw = await aget_config("image_ocr_enabled", client_id=client_id)
    except Exception as exc:
        logger.warning(f"[OCR_CONFIG] flag lookup failed for {client_id}: {exc}")
        return False
    return _coerce_bool_flag(raw)


async def ais_shop_content_extraction_enabled(client_id: str) -> bool:
    """Return True iff the client has explicitly opted in to shop-level
    promo/announce-bar extraction. Default is False — this flag exists
    independently of ``image_ocr_enabled`` so a client can run per-image OCR
    without paying for the storefront HTML scrape (or vice-versa).
    """
    if not client_id:
        return False
    try:
        raw = await aget_config("shop_content_extraction_enabled", client_id=client_id)
    except Exception as exc:
        logger.warning(f"[OCR_CONFIG] shop content flag lookup failed for {client_id}: {exc}")
        return False
    return _coerce_bool_flag(raw)


async def aget_shop_content_overrides(client_id: str) -> Dict[str, Any]:
    """Return the client's selector / domain overrides, or {} when none set."""
    if not client_id:
        return {}
    try:
        raw = await aget_config("shop_content_extractor_overrides", client_id=client_id)
    except Exception as exc:
        logger.warning(f"[OCR_CONFIG] overrides lookup failed for {client_id}: {exc}")
        return {}
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        import json
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def resolve_promo_selectors(overrides: Optional[Dict[str, Any]]) -> List[str]:
    """Build the final selector list from defaults + overrides."""
    if not overrides:
        return list(DEFAULT_PROMO_SELECTORS)
    explicit = overrides.get("promo_selectors")
    if isinstance(explicit, list) and explicit:
        # Override replaces the default bundle entirely
        return [s for s in explicit if isinstance(s, str) and s.strip()]
    selectors = list(DEFAULT_PROMO_SELECTORS)
    additional = overrides.get("additional_promo_selectors") or []
    if isinstance(additional, list):
        for sel in additional:
            if isinstance(sel, str) and sel.strip() and sel not in selectors:
                selectors.append(sel)
    return selectors


# ─── PDP size chart extraction ────────────────────────────────────────────────

DEFAULT_SIZE_CHART_SELECTORS: List[str] = [
    "div.size-chart",
    "div#size-chart",
    "[id*='size-chart']",
    "[class*='size-chart']",
]


async def ais_pdp_size_chart_ocr_enabled(client_id: str) -> bool:
    """Return True iff the client has enabled PDP size chart image OCR.

    Works **independently** of ``image_ocr_enabled``:
      * Both true  → full OCR (gallery + metafield + size chart images).
      * Only this flag true → size-chart-only mode (no gallery/metafield OCR).
      * Only ``image_ocr_enabled`` true → no size chart scraping.
    """
    if not client_id:
        return False
    try:
        raw = await aget_config("pdp_size_chart_ocr_enabled", client_id=client_id)
    except Exception as exc:
        logger.warning(f"[OCR_CONFIG] pdp size chart flag lookup failed for {client_id}: {exc}")
        return False
    return _coerce_bool_flag(raw)


async def aget_pdp_size_chart_overrides(client_id: str) -> Dict[str, Any]:
    """Return the client's PDP size chart selector / domain overrides, or {}."""
    if not client_id:
        return {}
    try:
        raw = await aget_config("pdp_size_chart_overrides", client_id=client_id)
    except Exception as exc:
        logger.warning(f"[OCR_CONFIG] pdp size chart overrides lookup failed for {client_id}: {exc}")
        return {}
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        import json
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def resolve_size_chart_selectors(overrides: Optional[Dict[str, Any]]) -> List[str]:
    """Build the final size chart CSS selector list from defaults + overrides."""
    if not overrides:
        return list(DEFAULT_SIZE_CHART_SELECTORS)
    explicit = overrides.get("selectors")
    if isinstance(explicit, list) and explicit:
        return [s for s in explicit if isinstance(s, str) and s.strip()]
    return list(DEFAULT_SIZE_CHART_SELECTORS)
