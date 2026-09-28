"""
Product Image OCR Extractor — runs a vision LLM (Gemini 2.5 Flash Lite) on
product gallery + metafield-embedded images and returns the extracted text plus
a deduped, search-friendly summary that can be folded into Upstash Search's
``content`` blob (within a 4096-byte budget).

Used by:
- Full-ingestion and delta-sync orchestrator paths (batch, no per-call
  timeout — bounded by the global OCR concurrency semaphore).
- Shopify ``products/update`` webhook hot path (single product, bounded by
  ``WEBHOOK_OCR_TIMEOUT_SECONDS`` + Upstash-preserve fallback).
- ``POST /api/v1/products/ingest/single`` endpoint (same single-product path,
  bounded by the wider ``SINGLE_PRODUCT_API_OCR_TIMEOUT_SECONDS``).

Single-product callers should use ``aapply_ocr_to_product`` defined at the
bottom of this module — it's the single source of truth for the
gate / Tier-1-hit / LLM-with-timeout / preserve-from-Upstash decision tree.
"""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from fashion_bot.services.product_ingestion.product_image_ocr_store import (
    aget_cached_ocr_for_urls,
    aupsert_ocr_results,
    url_sha256,
)
from fashion_bot.services.product_ingestion.product_ocr_summary_store import (
    aget_product_ocr_summary,
    aupsert_product_ocr_summary,
    compute_url_set_hash,
)
from fashion_bot.monitoring.otel_metrics import llm_caller

logger = logging.getLogger(__name__)

_NO_TRACE_CONFIG: Dict[str, Any] = {"callbacks": []}

OCR_PROMPT_VERSION = 1
DEFAULT_MODEL_NAME = "gemini-2.5-flash-lite"

# ─── OpenRouter routing (background key isolation) ───────────────────────────
# OCR is a background/batch workload, so when OPENROUTER_API_KEY_2 is set we
# route it through OpenRouter on the BACKGROUND key (key 2) instead of calling
# Gemini directly. This keeps OCR off the chat key entirely. If the key is
# absent or OpenRouter init fails, we fall back to the legacy Gemini-direct
# path below (which keeps its safety_settings + JSON-mode loop guards).
#
# NOTE: routed through OpenRouter we lose Gemini-native safety_settings and the
# typed GenerationConfig JSON-mode loop guard; the per-image fallback + the
# regex JSON extraction in _parse_response still recover from non-JSON / looped
# output, and OCR_MAX_OUTPUT_TOKENS bounds any runaway.
OCR_OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY_2"
# Vision-capable model slug on OpenRouter; override with OCR_LLM_MODEL.
OCR_OPENROUTER_MODEL = "google/gemini-2.5-flash-lite"


def _image_content_part(url: str, use_openrouter: bool) -> Dict[str, Any]:
    """Build a multimodal ``image_url`` content part for a HumanMessage.

    OpenRouter (OpenAI-compatible) expects ``image_url`` to be an object
    ``{"url": ...}``; Gemini-direct (langchain-google-genai) accepts the bare
    URL string. Use the right shape so the same call site works on both paths.
    """
    if use_openrouter:
        return {"type": "image_url", "image_url": {"url": url}}
    return {"type": "image_url", "image_url": url}

# OTel `caller` label applied to every llm.* metric emitted by this extractor's
# LLMFactory fallback path. The primary path (direct ChatGoogleGenerativeAI in
# _get_llm) does NOT attach the OTel callback handler and therefore emits no
# llm.* metrics regardless of this wrapper; for the fallback path that goes
# through LLMFactory._get_or_create, the handler IS attached, and the wrapper
# is the only way to tag the caller correctly because the lazy-init pattern
# means the ContextVar set at instantiation time is invisible to the
# subsequent ainvoke call's task context.
_LLM_CALLER = "product_image_ocr"

# Output-token budget for the OCR LLM. The combined call emits one JSON
# object containing per-image text for up to ~20 product images plus a
# search-friendly summary. Each image's text can run 100–300 chars
# (~30–80 tokens), so a 12-image product needs ~1000–1500 tokens just for
# the per-image array, plus the JSON scaffolding and summary. We size for
# ~4× that to handle long, text-rich PDPs without truncation. ``gemini-2.5-
# flash-lite`` caps output at 8192 tokens, so 4096 is well within limits.
# Truncation here (finish_reason=MAX_TOKENS) breaks JSON parsing and
# silently drops a product's OCR text on the floor — easy to underestimate,
# expensive to debug.
OCR_MAX_OUTPUT_TOKENS = 4096

# Per-product summary char cap returned by the combined LLM call.
PRODUCT_SUMMARY_MAX_CHARS = 500

# Per-image text cap inside the LLM response. Smaller than
# ``MAX_PER_IMAGE_TEXT_CHARS`` (the post-truncate metadata cap) so the model
# stays well clear of the output-token budget on text-dense PDPs like
# nutraceutical / regulatory ingredient lists where the model would otherwise
# enter a transcription loop and exhaust ``OCR_MAX_OUTPUT_TOKENS``.
PROMPT_PER_IMAGE_TEXT_CAP_CHARS = 400

# Final content.image_text composition cap (after layering shop terms on top).
COMBINED_IMAGE_TEXT_MAX_CHARS = 2000

# Hard caps — keep aligned with the design doc decisions.
MAX_GALLERY_IMAGES_PER_PRODUCT = 10
MAX_METAFIELD_IMAGES_PER_PRODUCT = 12
MAX_SIZE_CHART_IMAGES_PER_PRODUCT = 4
HARD_CAP_IMAGES_PER_PRODUCT = 25
MAX_OCR_CALLS_PER_RUN = 10_000
MAX_CONCURRENT_OCR = 8

# Cap the size of content.image_text BEFORE we even compute the per-product
# budget; the budget gate in to_search_document narrows it further if needed.
MAX_CONTENT_IMAGE_TEXT_CHARS = 800

# Per-image raw text budget in metadata so a single chatty image doesn't blow up
# the metadata blob.
MAX_PER_IMAGE_TEXT_CHARS = 1500

# Only OCR images from these CDNs. Shopify-hosted images for the active store.
_ALLOWED_CDN_HOST = "cdn.shopify.com"

# Skip metafield namespaces that hold user-generated content (reviews, etc.)
_METAFIELD_NAMESPACE_DENYLIST = {"vstar", "judgeme", "trustoo"}

# Matches Shopify-hosted images at both URL forms:
#   - Canonical CDN:        https://cdn.shopify.com/s/files/<store_id>/files/<file>
#   - Custom-domain proxy:  https://{shop}.com/cdn/shop/files/<file>
# Also accepts protocol-relative URLs (//...) that show up in storefront HTML.
# NOTE: ``svg`` is deliberately NOT in the extension list — Gemini's image_url
# input rejects ``image/svg+xml`` with a 400, which fails the entire combined
# call for any product that has even one SVG in its image set. SVG URLs that
# slip through the regex via gallery paths are filtered by
# ``_is_unsupported_image_url`` below.
_IMG_URL_RX = re.compile(
    r"(?:https?:)?//[^\s\"'<>)]+(?:/s/files/|/cdn/shop/files/)[^\s\"'<>)]+?\.(?:png|jpg|jpeg|webp|gif)",
    re.IGNORECASE,
)

# Image MIME types Gemini 2.5 Flash Lite cannot read. We filter these at URL
# collection time so an unsupported asset never burns an LLM call (and never
# blows up the whole product's combined call with a 400).
_UNSUPPORTED_IMG_EXTENSIONS: Tuple[str, ...] = (".svg",)


def _is_unsupported_image_url(url: str) -> bool:
    """Return True if Gemini cannot read this image (e.g. SVG)."""
    if not url:
        return True
    canonical = url.split("?")[0].lower()
    return canonical.endswith(_UNSUPPORTED_IMG_EXTENSIONS)


def normalize_image_url(raw: str) -> str:
    """Ensure a URL is fetchable by the vision LLM.

    Storefront HTML often contains protocol-relative URLs like ``//foo.com/...``.
    Gemini's image_url input requires a full ``https://`` URL.
    """
    if not raw:
        return raw
    s = raw.strip()
    if s.startswith("//"):
        return "https:" + s
    return s


@dataclass
class ImageOCRResult:
    url: str
    text: str = ""
    summary_terms: List[str] = field(default_factory=list)
    has_text: bool = False
    extraction_success: bool = False
    cached: bool = False
    source: str = "gallery"  # "gallery" | "metafield" | "pdp_size_chart"
    error: Optional[str] = None


@dataclass
class ProductOCRResult:
    product_id: str
    per_image: List[ImageOCRResult] = field(default_factory=list)
    summary: str = ""
    ocr_hash: str = ""

    def to_metadata_image_texts(self) -> Dict[str, str]:
        return {r.url: (r.text or "")[:MAX_PER_IMAGE_TEXT_CHARS] for r in self.per_image if r.has_text}


# ----------------------------- URL collection -----------------------------


def collect_image_urls_from_metafields(
    all_metafields: Iterable[Dict[str, Any]],
) -> List[str]:
    """Walk metafield values (HTML / JSON / string) and pull product-owned Shopify CDN
    image URLs. Strip review-widget namespaces.
    """
    seen: Set[str] = set()
    out: List[str] = []
    for mf in all_metafields or []:
        if not isinstance(mf, dict):
            continue
        namespace = (mf.get("namespace") or "").lower()
        if namespace and namespace in _METAFIELD_NAMESPACE_DENYLIST:
            continue
        value = mf.get("value", "")
        if not isinstance(value, str):
            try:
                value = json.dumps(value)
            except Exception:
                continue
        for match in _IMG_URL_RX.findall(value):
            url = normalize_image_url(match)
            if _is_unsupported_image_url(url):
                continue
            canonical = url.split("?")[0]
            if canonical in seen:
                continue
            seen.add(canonical)
            out.append(url)
    return out


def collect_image_urls_from_html(html: str) -> List[str]:
    """Pull Shopify-hosted image URLs out of a raw storefront HTML blob.

    Catches both canonical CDN URLs and custom-domain-proxied CDN URLs.
    Protocol-relative URLs (``//host/...``) are normalised to ``https://``.
    """
    if not html:
        return []
    seen: Set[str] = set()
    out: List[str] = []
    for match in _IMG_URL_RX.findall(html):
        url = normalize_image_url(match)
        if _is_unsupported_image_url(url):
            continue
        canonical = url.split("?")[0]
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(url)
    return out


def extract_size_chart_urls_from_html(
    html: str,
    selectors: Optional[Sequence[str]] = None,
) -> List[str]:
    """Parse storefront PDP HTML and extract image URLs from the size chart container.

    Shopify themes typically render size charts in a hidden ``<div class="size-chart">``
    or ``<div id="size-chart">`` that contains ``<img>`` tags pointing to Shopify CDN
    files (e.g. ``BRA_CM.jpg``, ``TOP_IN.jpg``).

    Falls back to regex extraction if BeautifulSoup is unavailable.
    """
    if not html:
        return []
    if selectors is None:
        from fashion_bot.services.product_ingestion.image_ocr_config import (
            DEFAULT_SIZE_CHART_SELECTORS,
        )
        selectors = DEFAULT_SIZE_CHART_SELECTORS

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("[SIZE_CHART] beautifulsoup4 missing; falling back to regex")
        return _extract_size_chart_urls_regex(html)

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.warning(f"[SIZE_CHART] HTML parse failed: {exc}")
        return []

    seen: Set[str] = set()
    out: List[str] = []
    for selector in selectors:
        try:
            containers = soup.select(selector)
        except Exception:
            continue
        for container in containers:
            for img in container.find_all("img", src=True):
                raw_src = img.get("src", "")
                url = normalize_image_url(raw_src)
                if not url or _is_unsupported_image_url(url):
                    continue
                canonical = url.split("?")[0]
                if canonical in seen:
                    continue
                seen.add(canonical)
                out.append(url)
    return out


_SIZE_CHART_BLOCK_RX = re.compile(
    r'<div[^>]*(?:class\s*=\s*"[^"]*size-chart[^"]*"|id\s*=\s*"[^"]*size-chart[^"]*")[^>]*>'
    r'(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_IMG_SRC_RX = re.compile(r'<img[^>]+src\s*=\s*"([^"]+)"', re.IGNORECASE)

# Heuristic splitter: 2+ consecutive spaces or a tab act as column separators
# in OCR output. Single spaces within a cell ("UNDER BUST") are preserved.
_COL_SPLIT_RX = re.compile(r"  +|\t")


def size_chart_ocr_to_html_table(raw_text: str) -> str:
    """Convert raw OCR text from a size chart image into an HTML ``<table>``.

    The vision LLM returns line-delimited, column-aligned text for tabular
    images. This function parses that into an HTML table matching the format
    that ``_extract_size_chart`` produces for Shopify Page-sourced size charts,
    so the chat widget renders both identically.

    Falls back to ``<pre>`` wrapping when the text doesn't look tabular.
    """
    if not raw_text or not raw_text.strip():
        return ""

    lines = [ln for ln in raw_text.strip().splitlines() if ln.strip()]
    if not lines:
        return ""

    # Try splitting each line into cells.
    parsed_rows: List[List[str]] = []
    for ln in lines:
        cells = [c.strip() for c in _COL_SPLIT_RX.split(ln) if c.strip()]
        parsed_rows.append(cells)

    if not parsed_rows:
        return f"<pre>{raw_text}</pre>"

    # Determine the dominant column count (mode) to filter noise lines.
    col_counts = [len(r) for r in parsed_rows if len(r) > 1]
    if not col_counts:
        return f"<pre>{raw_text}</pre>"

    dominant = max(set(col_counts), key=col_counts.count)
    table_rows = [r for r in parsed_rows if len(r) == dominant]
    if len(table_rows) < 2:
        return f"<pre>{raw_text}</pre>"

    # First matching row = header, rest = data.
    header, *data = table_rows

    parts = ['<table style="text-align: center;">', "<tbody>"]
    # Header row
    parts.append("<tr>")
    for cell in header:
        parts.append(f'<td style="text-align: center;"><strong>{cell}</strong></td>')
    parts.append("</tr>")
    # Data rows
    for row in data:
        parts.append("<tr>")
        for cell in row:
            parts.append(f'<td style="text-align: center;">{cell}</td>')
        parts.append("</tr>")
    parts.append("</tbody>")
    parts.append("</table>")
    return "\n".join(parts)


def _extract_size_chart_urls_regex(html: str) -> List[str]:
    """Regex fallback when BeautifulSoup is unavailable."""
    seen: Set[str] = set()
    out: List[str] = []
    for block_match in _SIZE_CHART_BLOCK_RX.finditer(html):
        block = block_match.group(0)
        for img_match in _IMG_SRC_RX.finditer(block):
            raw_src = img_match.group(1)
            url = normalize_image_url(raw_src)
            if not url or _is_unsupported_image_url(url):
                continue
            if not _IMG_URL_RX.search(url):
                continue
            canonical = url.split("?")[0]
            if canonical in seen:
                continue
            seen.add(canonical)
            out.append(url)
    return out


def collect_image_urls_for_ocr(
    image_url: Optional[str],
    all_images: Sequence[str],
    all_metafields: Iterable[Dict[str, Any]],
    pdp_size_chart_urls: Optional[Sequence[str]] = None,
) -> List[Tuple[str, str]]:
    """Return [(url, source)] respecting per-source and hard caps."""
    gallery_urls: List[str] = []
    seen_gallery: Set[str] = set()
    raw_gallery = [u for u in [image_url] + list(all_images or []) if u]
    for u in raw_gallery:
        if _is_unsupported_image_url(u):
            continue
        canonical = u.split("?")[0]
        if canonical in seen_gallery:
            continue
        seen_gallery.add(canonical)
        gallery_urls.append(u)
        if len(gallery_urls) >= MAX_GALLERY_IMAGES_PER_PRODUCT:
            break

    metafield_urls_raw = collect_image_urls_from_metafields(all_metafields)
    metafield_urls: List[str] = []
    seen_meta: Set[str] = set(u.split("?")[0] for u in gallery_urls)
    for u in metafield_urls_raw:
        if _is_unsupported_image_url(u):
            continue
        canonical = u.split("?")[0]
        if canonical in seen_meta:
            continue
        seen_meta.add(canonical)
        metafield_urls.append(u)
        if len(metafield_urls) >= MAX_METAFIELD_IMAGES_PER_PRODUCT:
            break

    size_chart_urls: List[str] = []
    if pdp_size_chart_urls:
        seen_all: Set[str] = set(u.split("?")[0] for u in gallery_urls + metafield_urls)
        for u in pdp_size_chart_urls:
            if _is_unsupported_image_url(u):
                continue
            canonical = u.split("?")[0]
            if canonical in seen_all:
                continue
            seen_all.add(canonical)
            size_chart_urls.append(u)
            if len(size_chart_urls) >= MAX_SIZE_CHART_IMAGES_PER_PRODUCT:
                break

    combined: List[Tuple[str, str]] = [(u, "gallery") for u in gallery_urls]
    combined += [(u, "metafield") for u in metafield_urls]
    combined += [(u, "pdp_size_chart") for u in size_chart_urls]
    return combined[:HARD_CAP_IMAGES_PER_PRODUCT]


# ----------------------------- LLM prompt -----------------------------


_OCR_PROMPT = """You are an OCR + summariser for a single e-commerce product image.

Return ONLY JSON with this exact shape, no markdown, no commentary:
{"text": "...", "summary_terms": ["...", "..."]}

Rules:
- "text": ALL legible text visible in the image, top-to-bottom, line-preserved.
  Include offer text, ingredient names, claims, badges, prices, and callouts.
  If the image is a plain product photo with no text, return "".
- "summary_terms": array of 3-12 short noun phrases (each ≤ 4 words, lowercase)
  capturing searchable claims, offers, ingredients, and feature words a shopper
  might query. Empty array [] if no text.
- Do NOT invent text that isn't visible.
- Strip decorative ASCII / emoji.
- Preserve currency symbols and numeric thresholds (e.g. "₹599", "20% off").
"""


# ----------------------------- Extractor -----------------------------


class ProductImageOCRExtractor:
    """Vision OCR + summary extractor. Instantiates its own Gemini 2.5 Flash Lite
    instance (separate from LLMFactory's shared cache) so we can apply
    OCR-specific ``safety_settings`` without affecting agent-facing prompts.

    Why we relax safety settings for OCR:
    Catalogs from skincare / wellness brands frequently combine therapeutic
    claim copy ("reduces stress hormones by 70%", "treats acne") with model
    photography. This combination reliably trips Gemini's safety filter into
    returning an empty response with ``block_reason: OTHER`` — even though
    the request is a pure transcription task with no generative risk.

    Setting all four standard harm categories to ``BLOCK_NONE`` is safe in
    this scope because:
      * we transcribe text from product images, we don't generate anything
        the customer sees,
      * the LLM's output goes into a deterministic search index (Upstash
        ``content.image_text``), with no LLM-to-LLM forwarding,
      * customer-facing prompts (agent skill nodes, demo chat, QU) use
        separate LLM instances via ``LLMFactory`` and keep their default
        safety thresholds.
    """

    def __init__(self):
        self._llm = None
        self._model_name = DEFAULT_MODEL_NAME
        # True when _get_llm resolved to the OpenRouter (key 2) path; controls
        # the image content-part shape (see _image_content_part).
        self._use_openrouter = False
        # Populated lazily by ``_get_llm``. Carries the GenerationConfig
        # (typed or dict) we want passed on every ``ainvoke`` for redundancy
        # over the constructor-level config, which some langchain versions
        # silently drop on the floor.
        self._invoke_generation_config: Optional[Any] = None

    @staticmethod
    def _build_safety_settings() -> Optional[Dict[Any, Any]]:
        """Return Gemini safety_settings that disable filtering across all
        four standard harm categories. Returns ``None`` when the
        ``HarmCategory`` / ``HarmBlockThreshold`` enums aren't importable
        from the installed langchain-google-genai version — in that case
        we fall back to defaults and just rely on block_reason logging.
        """
        try:
            from langchain_google_genai import HarmBlockThreshold, HarmCategory
        except Exception as exc:
            logger.warning(
                f"[OCR_EXTRACTOR] HarmCategory enums not importable "
                f"({exc}); falling back to default Gemini safety thresholds."
            )
            return None
        return {
            HarmCategory.HARM_CATEGORY_HARASSMENT:        HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH:       HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }

    def _get_llm(self):
        if self._llm is not None:
            return self._llm

        # Preferred path: OpenRouter on the BACKGROUND key (key 2) when set.
        from fashion_bot.env_loader import get_env as _get_env_for_router
        if _get_env_for_router(OCR_OPENROUTER_KEY_ENV):
            try:
                from fashion_bot.core.llm_config import LLMConfig, LLMProvider
                from fashion_bot.core.llm_factory import LLMFactory
                model = _get_env_for_router("OCR_LLM_MODEL", OCR_OPENROUTER_MODEL)
                config = LLMConfig(
                    provider=LLMProvider.OPENROUTER,
                    model=model,
                    temperature=0.0,
                    max_tokens=OCR_MAX_OUTPUT_TOKENS,
                    api_key_env_var=OCR_OPENROUTER_KEY_ENV,
                    base_url="https://openrouter.ai/api/v1",
                )
                self._llm = LLMFactory._get_or_create(config)
                self._use_openrouter = True
                self._model_name = model
                # OpenRouter path does not use Gemini's per-invocation
                # generation_config; the regex JSON parser handles output.
                self._invoke_generation_config = None
                logger.info(
                    f"[OCR_EXTRACTOR] vision LLM via OpenRouter (key 2): {model}"
                )
                return self._llm
            except Exception as exc:
                logger.warning(
                    f"[OCR_EXTRACTOR] OpenRouter init failed ({exc}); "
                    f"falling back to Gemini-direct (GOOGLE_API_KEY)."
                )
                self._use_openrouter = False

        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
            from fashion_bot.env_loader import get_env
        except Exception as exc:
            # Last-resort fallback to the shared factory (preserves prior
            # behaviour if langchain_google_genai changes surface).
            logger.warning(
                f"[OCR_EXTRACTOR] direct Gemini init failed ({exc}); "
                f"falling back to LLMFactory (no safety_settings override)."
            )
            from fashion_bot.core.llm_config import LLMConfig, LLMProvider
            from fashion_bot.core.llm_factory import LLMFactory
            config = LLMConfig(
                provider=LLMProvider.GEMINI,
                model=DEFAULT_MODEL_NAME,
                temperature=0.0,
                max_tokens=OCR_MAX_OUTPUT_TOKENS,
                api_key_env_var="GOOGLE_API_KEY",
            )
            self._llm = (
                LLMFactory._get_or_create(config)
                if hasattr(LLMFactory, "_get_or_create")
                else LLMFactory._create_gemini_llm(config)
            )
            self._model_name = config.model
            logger.info(
                f"[OCR_EXTRACTOR] vision LLM initialised via factory: {self._model_name}"
            )
            return self._llm

        api_key = get_env("GOOGLE_API_KEY")
        safety_settings = self._build_safety_settings()
        # Force JSON-only output. Without this, Gemini occasionally enters a
        # degenerate repetition loop on dense regulatory imagery (transcribing
        # the same "*RDA not established" or "Lab Ref. No." line, or counting
        # "458, 459, 460, ..." until ``max_output_tokens`` is exhausted).
        # ``response_mime_type`` constrains the model to valid JSON, which
        # breaks those loops at the token-by-token sampling level.
        #
        # The May-20 follow-up run showed that passing a *dict* as
        # ``generation_config`` to the constructor is silently accepted by
        # langchain-google-genai but NOT forwarded to the underlying
        # google-generativeai SDK — the model kept emitting markdown-wrapped
        # JSON and loop-truncated. So we now:
        #   1. Pass a typed ``google.generativeai.types.GenerationConfig``
        #      (the SDK's native dataclass) to the constructor when
        #      importable, which langchain forwards verbatim.
        #   2. ALSO carry the dict around for per-invocation override (some
        #      langchain versions only honour ``generation_config`` if passed
        #      to ``invoke``).
        # When neither path works (or import fails) we land on free-form
        # output and rely on the per-image fallback below to recover.
        kwargs: Dict[str, Any] = dict(
            model=DEFAULT_MODEL_NAME,
            temperature=0.0,
            max_output_tokens=OCR_MAX_OUTPUT_TOKENS,
            google_api_key=api_key,
        )
        if safety_settings is not None:
            kwargs["safety_settings"] = safety_settings

        # Cache the per-invocation override so ainvoke can pass it through.
        self._invoke_generation_config: Optional[Any] = None
        constructor_json_mode = "off"

        try:
            from google.generativeai.types import GenerationConfig
            typed_gen_config = GenerationConfig(
                response_mime_type="application/json"
            )
        except Exception as exc:
            logger.debug(
                f"[OCR_EXTRACTOR] typed GenerationConfig unavailable ({exc}); "
                f"will rely on dict + per-invocation passing only"
            )
            typed_gen_config = None

        try:
            if typed_gen_config is not None:
                self._llm = ChatGoogleGenerativeAI(
                    **kwargs,
                    generation_config=typed_gen_config,
                )
                constructor_json_mode = "on (typed)"
            else:
                self._llm = ChatGoogleGenerativeAI(
                    **kwargs,
                    generation_config={"response_mime_type": "application/json"},
                )
                constructor_json_mode = "on (dict)"
        except TypeError as exc:
            logger.warning(
                f"[OCR_EXTRACTOR] generation_config kwarg unsupported "
                f"({exc}); falling back to constructor without it. "
                f"Per-invocation override still attempted."
            )
            self._llm = ChatGoogleGenerativeAI(**kwargs)

        # Stash both the typed object and the dict for per-call passing.
        # ``aextract_for_product_combined`` tries the typed form first, then
        # the dict, then no kwarg — matching whichever the installed
        # langchain version is willing to honour.
        self._invoke_generation_config = (
            typed_gen_config if typed_gen_config is not None
            else {"response_mime_type": "application/json"}
        )

        self._model_name = DEFAULT_MODEL_NAME
        logger.info(
            f"[OCR_EXTRACTOR] vision LLM initialised: {self._model_name} "
            f"(max_output_tokens={OCR_MAX_OUTPUT_TOKENS}, "
            f"safety_settings={'BLOCK_NONE x4' if safety_settings else 'defaults'}, "
            f"json_mode={constructor_json_mode}, "
            f"per_invocation_override={'typed' if typed_gen_config is not None else 'dict'})"
        )
        return self._llm

    @staticmethod
    def _extract_block_reason(response: Any) -> Optional[str]:
        """Pull Gemini's safety / policy verdict out of the response object.

        Returns the block_reason string (e.g. ``"SAFETY"``, ``"OTHER"``,
        ``"PROHIBITED_CONTENT"``) when the call was filtered, otherwise None.
        Resilient to langchain version differences in metadata shape.

        Does NOT return ``MAX_TOKENS`` — that's a truncation, not a block.
        Use ``_extract_finish_reason`` for the raw value.
        """
        try:
            metadata = getattr(response, "response_metadata", None) or {}
            if not isinstance(metadata, dict):
                return None
            feedback = metadata.get("prompt_feedback") or {}
            if isinstance(feedback, dict):
                br = feedback.get("block_reason")
                if br:
                    # Normalise enum / int / str representations to str
                    return str(getattr(br, "name", br))
            # Also check finish_reason for SAFETY / RECITATION on the response
            finish_reason = metadata.get("finish_reason")
            if finish_reason and str(finish_reason).upper() in {
                "SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST",
            }:
                return str(finish_reason)
        except Exception:
            return None
        return None

    @staticmethod
    def _extract_finish_reason(response: Any) -> Optional[str]:
        """Return the raw ``finish_reason`` from the response (e.g.
        ``STOP``, ``MAX_TOKENS``, ``SAFETY``) or None if unavailable.

        Separate from ``_extract_block_reason`` because ``MAX_TOKENS`` is a
        truncation signal, not a policy block, and we want to handle it
        distinctly (re-issue with higher budget or repair partial JSON
        rather than just logging a block).
        """
        try:
            metadata = getattr(response, "response_metadata", None) or {}
            if not isinstance(metadata, dict):
                return None
            fr = metadata.get("finish_reason")
            if fr is None:
                return None
            return str(getattr(fr, "name", fr)).upper()
        except Exception:
            return None

    @staticmethod
    def _parse_response(text: str) -> Dict[str, Any]:
        """Parse a Gemini OCR response into ``{"per_image": [...], "summary": ...}``.

        Tolerates the three shapes Gemini emits in practice:
        1. Raw JSON.
        2. Multi-line Markdown code fence::

               ```json
               { ... }
               ```

        3. Single-line code fence (``\u0060\u0060\u0060json {...} \u0060\u0060\u0060``)
           — the bug fixed here: the line-split fence stripper used to
           collapse this case to an empty string, dropping a valid 1.2 KB
           OCR payload on the floor.

        Always falls back to a greedy ``\\{.*\\}`` extraction on the
        *original* text (with ``re.DOTALL``) so we never lose a valid JSON
        object that's wrapped in something we didn't anticipate.
        """
        original = (text or "").strip()
        if not original:
            return {}

        # Attempt 1: direct json.loads on the original text. Works when
        # Gemini complies with the "respond with raw JSON" instruction.
        try:
            return json.loads(original)
        except json.JSONDecodeError:
            pass

        # Attempt 2: strip a multi-line Markdown code fence and retry.
        fenced = original
        if fenced.startswith("```"):
            lines = fenced.split("\n")
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

        # Attempt 3: greedy JSON-object extraction on the ORIGINAL text.
        # Crucial for single-line ``\u0060\u0060\u0060json {...}\u0060\u0060\u0060``
        # responses where the line-split logic above leaves nothing behind.
        match = re.search(r"\{.*\}", original, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {}

    async def _ocr_one_url(self, url: str, source: str) -> ImageOCRResult:
        from langchain_core.messages import HumanMessage

        llm = self._get_llm()
        msg = HumanMessage(
            content=[
                {"type": "text", "text": _OCR_PROMPT},
                _image_content_part(url, self._use_openrouter),
            ]
        )
        try:
            # llm_caller wrapper: see _LLM_CALLER comment at module top.
            with llm_caller(_LLM_CALLER):
                response = await llm.ainvoke([msg], config=_NO_TRACE_CONFIG)
            block_reason = self._extract_block_reason(response)
            if block_reason:
                logger.warning(
                    f"[OCR_EXTRACTOR] Gemini blocked response for {url}: "
                    f"block_reason={block_reason}"
                )
            raw = response.content if hasattr(response, "content") else str(response)
            parsed = self._parse_response(raw if isinstance(raw, str) else json.dumps(raw))
            text = (parsed.get("text") or "").strip()
            summary_terms_raw = parsed.get("summary_terms") or []
            if not isinstance(summary_terms_raw, list):
                summary_terms_raw = []
            terms: List[str] = []
            for t in summary_terms_raw:
                if not isinstance(t, str):
                    continue
                v = t.strip().lower()
                if v and v not in terms:
                    terms.append(v)
                if len(terms) >= 12:
                    break
            return ImageOCRResult(
                url=url,
                text=text[:MAX_PER_IMAGE_TEXT_CHARS],
                summary_terms=terms,
                has_text=bool(text),
                extraction_success=True,
                source=source,
                cached=False,
            )
        except Exception as exc:
            logger.warning(f"[OCR_EXTRACTOR] OCR failed for {url}: {exc}")
            return ImageOCRResult(
                url=url,
                source=source,
                extraction_success=False,
                error=str(exc),
            )

    async def _per_image_ocr_fallback(
        self,
        urls: List[str],
        url_to_source: Dict[str, str],
        trace_id: Optional[str] = None,
    ) -> Dict[str, str]:
        """Fallback when the combined call was safety-blocked: OCR each image
        individually so the offending image fails in isolation instead of
        poisoning the whole product's OCR result.

        Returns a dict ``{url: text}`` covering only the URLs that produced
        non-empty extracted text. Bounded by ``MAX_CONCURRENT_OCR`` to avoid
        hammering the API on a 25-image fallback.
        """
        if not urls:
            return {}
        sem = asyncio.Semaphore(MAX_CONCURRENT_OCR)

        async def _one(url: str) -> Tuple[str, Optional[ImageOCRResult]]:
            async with sem:
                res = await self._ocr_one_url(url, url_to_source.get(url, "gallery"))
                return url, res

        results = await asyncio.gather(*[_one(u) for u in urls])
        recovered: Dict[str, str] = {}
        for url, res in results:
            if res is None or not res.extraction_success:
                continue
            text = (res.text or "").strip()
            if text:
                recovered[url] = text
        logger.info(
            f"[OCR_EXTRACTOR] [{trace_id}] per-image fallback recovered "
            f"{len(recovered)}/{len(urls)} images"
        )
        return recovered

    async def extract_batch_async(
        self,
        client_id: str,
        products: Sequence[Tuple[str, List[Tuple[str, str]]]],
        max_concurrent_images: int = MAX_CONCURRENT_OCR,
        max_calls_per_run: int = MAX_OCR_CALLS_PER_RUN,
        trace_id: Optional[str] = None,
    ) -> Dict[str, ProductOCRResult]:
        """For each (product_id, [(url, source)]), return a ProductOCRResult.

        Reads the Postgres cache first; only URLs without a cached row at the
        current prompt_version + model are sent to the vision LLM. Newly-OCR'd
        rows are persisted back to the cache.
        """
        all_urls: List[str] = []
        for _, urls in products:
            for u, _src in urls:
                all_urls.append(u)
        all_urls = list(dict.fromkeys(all_urls))  # dedupe, preserve order

        cached = await aget_cached_ocr_for_urls(
            client_id=client_id,
            urls=all_urls,
            prompt_version=OCR_PROMPT_VERSION,
            model=DEFAULT_MODEL_NAME,
        )

        urls_to_ocr: List[Tuple[str, str]] = []
        seen_to_ocr: Set[str] = set()
        for _, urls in products:
            for url, source in urls:
                if url in cached or url in seen_to_ocr:
                    continue
                seen_to_ocr.add(url)
                urls_to_ocr.append((url, source))

        if len(urls_to_ocr) > max_calls_per_run:
            logger.warning(
                f"[OCR_EXTRACTOR] [{trace_id}] capping OCR calls this run: "
                f"{len(urls_to_ocr)} → {max_calls_per_run}"
            )
            urls_to_ocr = urls_to_ocr[:max_calls_per_run]

        logger.info(
            f"[OCR_EXTRACTOR] [{trace_id}] client={client_id} products={len(products)} "
            f"urls_total={len(all_urls)} cache_hits={len(cached)} to_ocr={len(urls_to_ocr)}"
        )

        new_results: Dict[str, ImageOCRResult] = {}
        if urls_to_ocr:
            sem = asyncio.Semaphore(max_concurrent_images)

            async def _bound(url: str, source: str):
                async with sem:
                    return await self._ocr_one_url(url, source)

            tasks = [asyncio.create_task(_bound(u, s)) for u, s in urls_to_ocr]
            for task in asyncio.as_completed(tasks):
                res = await task
                new_results[res.url] = res

            cache_rows = [
                {
                    "url": r.url,
                    "source": r.source,
                    "extracted_text": r.text,
                    "summary_terms": r.summary_terms,
                    "has_text": r.has_text,
                    "model": DEFAULT_MODEL_NAME,
                    "prompt_version": OCR_PROMPT_VERSION,
                }
                for r in new_results.values()
                if r.extraction_success
            ]
            if cache_rows:
                await aupsert_ocr_results(client_id=client_id, rows=cache_rows)

        out: Dict[str, ProductOCRResult] = {}
        for product_id, urls in products:
            per_image: List[ImageOCRResult] = []
            for url, source in urls:
                if url in cached:
                    row = cached[url]
                    terms = row.get("summary_terms") or []
                    if isinstance(terms, str):
                        try:
                            terms = json.loads(terms)
                        except Exception:
                            terms = []
                    per_image.append(
                        ImageOCRResult(
                            url=url,
                            text=(row.get("extracted_text") or "")[:MAX_PER_IMAGE_TEXT_CHARS],
                            summary_terms=[t for t in terms if isinstance(t, str)][:12],
                            has_text=bool(row.get("has_text")),
                            extraction_success=True,
                            cached=True,
                            source=source,
                        )
                    )
                elif url in new_results:
                    per_image.append(new_results[url])
                else:
                    per_image.append(
                        ImageOCRResult(
                            url=url,
                            source=source,
                            extraction_success=False,
                            error="skipped (over per-run cap)",
                        )
                    )
            out[product_id] = ProductOCRResult(product_id=product_id, per_image=per_image)

        return out


# ----------------------------- Summarisation -----------------------------


_WORD_RX = re.compile(r"[a-zA-Z0-9₹$%+\-/.]+")


def _normalise_token(tok: str) -> str:
    return tok.strip().lower()


def existing_term_set(
    title: str,
    tags: Iterable[str],
    collections: Iterable[str],
    description: str,
    metafield_attributes: Dict[str, str],
    extra_strings: Iterable[str] = (),
) -> Set[str]:
    """Lowercased word tokens already searchable via existing content fields.

    Used to dedupe shop-promo + image OCR summary terms before they're folded
    into ``content.image_text`` — so we don't spend the budget on tokens that
    already drive recall.
    """
    bag: Set[str] = set()
    sources: List[str] = []
    sources.append(title or "")
    sources.extend(tags or [])
    sources.extend(collections or [])
    sources.append(description or "")
    for v in (metafield_attributes or {}).values():
        if isinstance(v, str):
            sources.append(v)
    sources.extend(extra_strings or [])
    for s in sources:
        if not s:
            continue
        for tok in _WORD_RX.findall(s.lower()):
            if len(tok) >= 3:
                bag.add(tok)
    return bag


def _dedupe_terms(terms: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for t in terms:
        v = _normalise_token(t)
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _score_term(term: str) -> Tuple[int, int]:
    """Lower is better. Offers / prices first, then short claims, then longer phrases."""
    is_offer = bool(re.search(r"(₹|\$|%|free|off|extra|save|gift|min|min\.|above)", term))
    return (0 if is_offer else 1, len(term))


def build_image_text_summary(
    per_image: Sequence[ImageOCRResult],
    shop_promo_terms: Sequence[str],
    excluded_terms: Set[str],
    shop_image_terms: Sequence[str] = (),
    max_chars: int = MAX_CONTENT_IMAGE_TEXT_CHARS,
) -> str:
    """Produce the deduped, budgeted summary string fed to content.image_text.

    Deterministic (no LLM call): merges per-image summary_terms with shop-level
    text + image OCR terms, drops tokens that already appear in title/tags/etc.,
    orders offers first, joins with '; ' under max_chars.

    - ``shop_promo_terms``: text scraped from announcement-bar / Offer banner HTML.
    - ``shop_image_terms``: noun-phrase terms OCR'd from theme-rendered images
      (e.g. the april_offertile promo tile that lives only in the storefront HTML
      and is not in product.images or metafields).
    - ``per_image``: per-product gallery + metafield image OCR results.
    """
    if max_chars <= 0:
        return ""

    # 1) Shop-level lines / phrases — both text and image-OCR'd — go in first.
    #    Shop-image terms are typically short noun phrases (e.g. "april sale"),
    #    promo text is full sentences. Both are kept verbatim.
    shop_phrases = _dedupe_terms(list(shop_promo_terms or []) + list(shop_image_terms or []))

    # 2) Per-product image summary terms
    raw_image_terms: List[str] = []
    for r in per_image:
        if not r.extraction_success or not r.has_text:
            continue
        raw_image_terms.extend(r.summary_terms or [])
    image_terms = _dedupe_terms(raw_image_terms)

    # Drop tokens that the existing content already covers, but never drop a shop phrase
    def _is_redundant(phrase: str) -> bool:
        tokens = [t for t in _WORD_RX.findall(phrase.lower()) if len(t) >= 3]
        if not tokens:
            return False
        return all(t in excluded_terms for t in tokens)

    filtered_image = [t for t in image_terms if not _is_redundant(t)]

    # Order: offers first across BOTH sources, then everything else
    ordered = sorted(shop_phrases + filtered_image, key=_score_term)

    # Pack under budget; truncate cleanly on '; '
    out_parts: List[str] = []
    running = 0
    for term in ordered:
        sep = "; " if out_parts else ""
        chunk = sep + term
        if running + len(chunk) > max_chars:
            break
        out_parts.append(term)
        running += len(chunk)

    return "; ".join(out_parts)


def compute_ocr_hash(per_image: Sequence[ImageOCRResult], shop_promo_hash: str = "") -> str:
    """Deterministic hash of OCR state for a product. Used to detect drift in
    delta sync's UNCHANGED bucket so we can force an upsert when image text
    changes even if Shopify content didn't.
    """
    payload = {
        "shop_promo": shop_promo_hash,
        "images": [
            {"u": url_sha256(r.url), "h": bool(r.has_text), "t": r.text[:200]}
            for r in per_image
            if r.extraction_success
        ],
    }
    payload_str = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()


# ----------------------------- Combined per-product prompt + call -----------------------------


_COMBINED_OCR_PROMPT_HEADER = """You are an OCR + summariser for ONE e-commerce product.

You receive image(s) from a single product and (optionally) text already
extracted from images of the same product in a prior run. Your job is to
produce a single combined product summary AND per-image text for the new
images so we can cache them.
"""

_COMBINED_OCR_PROMPT_TASK_TEMPLATE = """
YOUR TASK:

1. For each NEW image listed above, extract legible text top-to-bottom,
   line-preserved. Include offer copy, ingredient names, claim text, badges,
   prices, callouts. Return "" for the image if it has no text (pure product
   photography).

   **CRITICAL — hard rules to prevent degenerate output:**
   - CAP each image's text at **{per_image_max_chars} characters maximum**.
     Stop transcribing once you hit this cap, even mid-paragraph.
   - For DENSE ingredient lists, regulatory fine print, INN names, or *RDA
     footnotes: keep the first 3–5 items and append "and others", then STOP.
     Do NOT transcribe all 50 ingredients.
   - **Never repeat the same line, phrase, or token more than once per image.**
     If you find yourself emitting "*RDA not established" or
     "TESTING OF COSMETIC PRODUCT" or a counting sequence (403, 404, 405…)
     repeatedly, STOP and return what you have so far.
   - Do not invent text that isn't visible.

2. Produce ONE SUMMARY string (≤ {summary_max_chars} characters) combining
   the meaningful text from BOTH the previously-OCR'd images and the newly
   extracted text, that:
   - Removes duplicate phrases (case-insensitive).
   - Removes common English stopwords (a, an, the, of, in, on, to, with, for,
     and, or, is, are, was, were, be, by, at, as, this, that, these, those,
     it, its, from, has, have, had, will, would, can, could, should, may,
     might, do, does, did).
   - KEEPS verbatim: ingredient names, product claims, prices (with currency
     symbols), percentages, feature words like "vegan" or "paraben free",
     brand names.
   - Summarises — does NOT transcribe verbatim.
   - Orders offers and prices first, then claims/benefits, then ingredients
     and feature words.
   - Joins items with "; ".
   - **Never return an empty summary.** If text is sparse, still emit at
     least the brand name + dominant product category (e.g. "Sereko; serum;
     vitamin c"). Empty summary means a wasted ingestion call.

Return ONLY valid JSON, no markdown wrapping, no commentary:
{{
  "per_image": [
    {{"url": "<exact URL of a new image>", "text": "<extracted text or empty string>"}}
  ],
  "summary": "<concise summary under {summary_max_chars} characters>"
}}

If there are no NEW images, return "per_image": [].
"""


def _build_combined_prompt(
    cached_texts: Dict[str, str],
    new_urls: List[str],
    summary_max_chars: int,
    per_image_max_chars: int = PROMPT_PER_IMAGE_TEXT_CAP_CHARS,
) -> str:
    """Assemble the text portion of the combined LLM prompt.

    Image parts are attached separately to the HumanMessage content array,
    one ``image_url`` part per URL in ``new_urls`` (same order).
    """
    parts: List[str] = [_COMBINED_OCR_PROMPT_HEADER]

    if cached_texts:
        parts.append("\n--- PREVIOUSLY OCR'D TEXT (treat as ground truth) ---")
        for i, (url, text) in enumerate(cached_texts.items(), start=1):
            snippet = (text or "").strip()
            if len(snippet) > 600:
                snippet = snippet[:600] + "…"
            parts.append(f"\n[Cached image {i}] url={url}")
            parts.append(f"  text: {snippet}")

    if new_urls:
        parts.append("\n--- NEW IMAGES (attached below in order; OCR each) ---")
        for i, url in enumerate(new_urls, start=1):
            parts.append(f"[New image {i}] url={url}")
    else:
        parts.append("\n--- NEW IMAGES: none — only summarise the cached text ---")

    parts.append(
        _COMBINED_OCR_PROMPT_TASK_TEMPLATE.format(
            summary_max_chars=summary_max_chars,
            per_image_max_chars=per_image_max_chars,
        )
    )
    return "\n".join(parts)


_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "to", "with", "for", "and", "or",
    "is", "are", "was", "were", "be", "by", "at", "as", "this", "that",
    "these", "those", "it", "its", "from", "has", "have", "had", "will",
    "would", "can", "could", "should", "may", "might", "do", "does", "did",
}


def _belt_and_braces_stopword_strip(s: str) -> str:
    """Last-resort defence in case the LLM left a few stopwords in. Very mild
    — we only touch whole-word matches, never substrings.
    """
    if not s:
        return s
    tokens = s.split(" ")
    cleaned = [t for t in tokens if t and t.lower() not in _STOPWORDS]
    return " ".join(cleaned)


_OFFER_RX = re.compile(
    r"(?:₹|\$|€|£|¥|%|\bfree\b|\boff\b|\bsave\b|\bgift\b|\bextra\b|\bmin\.?\b|\babove\b|\bbuy\b)",
    re.IGNORECASE,
)


def synthesise_summary_from_per_image(
    per_image_texts: Dict[str, str],
    max_chars: int = PRODUCT_SUMMARY_MAX_CHARS,
) -> str:
    """Deterministically build a product summary from per-image OCR texts.

    Used as a fallback when the LLM returned valid ``per_image`` text but an
    empty ``summary`` field, OR when chunking forced us to do multiple LLM
    calls and synthesise the cross-chunk summary ourselves.

    Heuristic:
      1. Split each image's text on line / pipe / semicolon delimiters.
      2. Strip stopwords whole-word, drop short tokens (< 3 chars unless
         numeric / currency-bearing).
      3. Dedupe lines case-insensitively.
      4. Sort: offer/price-bearing lines first, then everything else,
         preserving insertion order within each group.
      5. Join with ``"; "`` under ``max_chars``.
    """
    if not per_image_texts or max_chars <= 0:
        return ""

    raw_lines: List[str] = []
    for text in per_image_texts.values():
        if not text:
            continue
        for chunk in re.split(r"[\n|;]+", text):
            stripped = chunk.strip()
            if not stripped:
                continue
            cleaned = _belt_and_braces_stopword_strip(stripped)
            if not cleaned:
                continue
            tokens = cleaned.split()
            if not tokens:
                continue
            # Drop very short / very low-information lines unless they look
            # like a price / percentage marker we want to keep.
            if (
                len(cleaned) < 4
                and not _OFFER_RX.search(cleaned)
                and not any(ch.isdigit() for ch in cleaned)
            ):
                continue
            raw_lines.append(cleaned)

    seen: Set[str] = set()
    deduped: List[str] = []
    for line in raw_lines:
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(line)

    offers = [l for l in deduped if _OFFER_RX.search(l)]
    rest = [l for l in deduped if not _OFFER_RX.search(l)]
    ordered = offers + rest

    out_parts: List[str] = []
    running = 0
    for line in ordered:
        sep = "; " if out_parts else ""
        chunk_size = len(sep) + len(line)
        if running + chunk_size > max_chars:
            break
        out_parts.append(line)
        running += chunk_size
    return "; ".join(out_parts)


def compose_final_image_text(
    shop_promo_terms: Sequence[str],
    shop_image_terms: Sequence[str],
    product_summary: str,
    max_chars: int = COMBINED_IMAGE_TEXT_MAX_CHARS,
) -> str:
    """Deterministic merge of shop-level signals + per-product OCR summary.

    Shop-level phrases go first (offers/marquee), then the product summary.
    Deduped against itself; capped to ``max_chars``; truncates cleanly on
    "; " or word boundary.
    """
    if max_chars <= 0:
        return ""

    seen: Set[str] = set()
    chunks: List[str] = []

    def _push(items: Iterable[str]):
        for item in items:
            if not item:
                continue
            key = item.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            chunks.append(item.strip())

    _push(shop_promo_terms or [])
    _push(shop_image_terms or [])
    if product_summary:
        chunks.append(product_summary.strip())

    combined = "; ".join(c for c in chunks if c)
    if len(combined) > max_chars:
        truncated = combined[:max_chars]
        cut = truncated.rsplit("; ", 1)[0]
        combined = cut if cut else truncated.rsplit(" ", 1)[0]
    return combined


# The combined-call entry point is attached to the existing
# ``ProductImageOCRExtractor`` class below; defined here to keep the
# helpers and prompt-building bits co-located.

async def _aextract_for_product_combined_impl(
    self,
    client_id: str,
    product_id: str,
    image_urls: List[Tuple[str, str]],
    trace_id: Optional[str] = None,
) -> Tuple[str, Dict[str, str], bool]:
    """Run the per-product combined OCR+summary call with tiered caching.

    Returns ``(summary, per_image_texts_map, cached)`` where:
    - ``summary``: LLM-produced product summary (≤ 500 chars), or "" on failure.
    - ``per_image_texts_map``: {url: text} for ALL images (cached + freshly OCR'd).
    - ``cached``: True iff Tier 1 (per-product summary cache) hit.
    """
    if not image_urls:
        return "", {}, False

    # Normalise + dedupe URL list (already deduped upstream, but defensive).
    # Also drop any URL whose extension Gemini can't read (e.g. SVG) — one
    # such URL in ``content_parts`` makes the entire combined call 400.
    seen_urls: Set[str] = set()
    ordered_urls: List[str] = []
    url_to_source: Dict[str, str] = {}
    for url, source in image_urls:
        if not url or _is_unsupported_image_url(url):
            continue
        canonical = url.split("?")[0]
        if not canonical or canonical in seen_urls:
            continue
        seen_urls.add(canonical)
        ordered_urls.append(url)
        url_to_source[url] = source

    if not ordered_urls:
        return "", {}, False

    url_set_hash = compute_url_set_hash(ordered_urls)

    # --- Tier 1: per-product summary cache -------------------------------
    cached_row = await aget_product_ocr_summary(client_id, product_id)
    if cached_row and cached_row.get("url_set_sha256") == url_set_hash:
        cached_summary = cached_row.get("summary") or ""
        cached_per_image = cached_row.get("per_image_texts") or {}
        cached_has_any_text = bool(cached_summary.strip()) or any(
            (v or "").strip() for v in cached_per_image.values()
        )
        if cached_has_any_text:
            logger.debug(
                f"[OCR_COMBINED] [{trace_id}] tier-1 hit: client={client_id} "
                f"product={product_id} url_set_hash={url_set_hash[:8]}"
            )
            return cached_summary, cached_per_image, True
        # Empty Tier 1 row — stale poison from a prior LLM glitch / RAI
        # deflection. Don't return the empty result; fall through and let
        # Tier 2 + LLM re-attempt. Surface this loudly so we can spot
        # accumulated poison rows in production.
        logger.warning(
            f"[OCR_COMBINED] [{trace_id}] tier-1 hit was EMPTY — bypassing "
            f"cache and re-running LLM: client={client_id} "
            f"product={product_id} url_set_hash={url_set_hash[:8]}"
        )

    # --- Tier 2: per-image cache (skip URLs already OCR'd) ---------------
    per_image_cached = await aget_cached_ocr_for_urls(
        client_id=client_id,
        urls=ordered_urls,
        prompt_version=OCR_PROMPT_VERSION,
        model=DEFAULT_MODEL_NAME,
    )

    # Carry forward per-image texts from a prior product cache row too
    # (covers the case where a URL is per-product-known but the per-image
    # row was trimmed/swept since).
    prior_per_image = (cached_row or {}).get("per_image_texts") or {}

    cached_texts: Dict[str, str] = {}
    urls_new: List[str] = []
    for url in ordered_urls:
        row = per_image_cached.get(url)
        if row and (row.get("extracted_text") is not None):
            cached_texts[url] = (row.get("extracted_text") or "")
            continue
        prior_text = prior_per_image.get(url)
        if prior_text is not None:
            cached_texts[url] = prior_text
            continue
        urls_new.append(url)

    logger.info(
        f"[OCR_COMBINED] [{trace_id}] client={client_id} product={product_id} "
        f"total_urls={len(ordered_urls)} cached_text={len(cached_texts)} "
        f"new_for_llm={len(urls_new)}"
    )

    # --- Tier 3: single combined LLM call --------------------------------
    llm = self._get_llm()
    prompt_text = _build_combined_prompt(
        cached_texts=cached_texts,
        new_urls=urls_new,
        summary_max_chars=PRODUCT_SUMMARY_MAX_CHARS,
    )

    try:
        from langchain_core.messages import HumanMessage

        content_parts: List[Dict[str, Any]] = [{"type": "text", "text": prompt_text}]
        for url in urls_new:
            content_parts.append(_image_content_part(url, self._use_openrouter))

        # Pass ``generation_config`` per-invocation as well — see _get_llm
        # docstring for the langchain-version asymmetry rationale. Tries the
        # kwarg form first; on TypeError (older versions don't accept it
        # here either), falls back to a plain ainvoke.
        invoke_gen_config = getattr(self, "_invoke_generation_config", None)
        # llm_caller wrapper: see _LLM_CALLER comment at module top. Wraps
        # both the typed/dict path AND the TypeError fallback path so every
        # branch tags the caller correctly.
        with llm_caller(_LLM_CALLER):
            try:
                if invoke_gen_config is not None:
                    response = await llm.ainvoke(
                        [HumanMessage(content=content_parts)],
                        config=_NO_TRACE_CONFIG,
                        generation_config=invoke_gen_config,
                    )
                else:
                    response = await llm.ainvoke([HumanMessage(content=content_parts)], config=_NO_TRACE_CONFIG)
            except TypeError:
                response = await llm.ainvoke([HumanMessage(content=content_parts)], config=_NO_TRACE_CONFIG)
        raw = response.content if hasattr(response, "content") else str(response)
        parsed = self._parse_response(raw if isinstance(raw, str) else json.dumps(raw))

        # Detect Gemini safety / policy blocks AND output truncation.
        # Without this, both look identical to "image had no readable text"
        # (``parsed = {}``) and we cache the emptiness, suppressing OCR for
        # that product until the URL set changes. Logging gives us a clear
        # signal in steady state.
        block_reason = self._extract_block_reason(response)
        finish_reason = self._extract_finish_reason(response)
        raw_text = raw if isinstance(raw, str) else json.dumps(raw)
        parsed_per_image = parsed.get("per_image") if isinstance(parsed, dict) else None
        parsed_summary = parsed.get("summary") if isinstance(parsed, dict) else None
        parsed_is_empty = (
            (not parsed_per_image or all(
                not (isinstance(e, dict) and (e.get("text") or "").strip())
                for e in parsed_per_image
            ))
            and not (parsed_summary or "").strip()
        )

        # Compute fallback decision up-front so both safety blocks AND
        # max-tokens truncations route through the same per-image recovery
        # path. The May-20 follow-up run (trace ad54dbd5) showed three
        # products still truncating into degenerate loops on the combined
        # call ("Lab Ref. No." / "Ashwagandha & White Seed Extract" / numeric
        # counter sequences) despite the per-image prompt cap and JSON-mode
        # constraint. Per-image calls don't loop — confirmed by the
        # safety-block recovery on product 8831866143033 in that same run.
        fallback_reason: Optional[str] = None
        if parsed_is_empty and urls_new:
            if block_reason:
                fallback_reason = "safety block"
            elif finish_reason == "MAX_TOKENS":
                fallback_reason = "max-tokens truncation"

        if block_reason:
            logger.warning(
                f"[OCR_COMBINED] [{trace_id}] Gemini blocked response "
                f"client={client_id} product={product_id} "
                f"images={len(urls_new)} block_reason={block_reason} "
                f"finish_reason={finish_reason} "
                f"raw_chars={len(raw_text)}"
            )
        elif finish_reason == "MAX_TOKENS" and parsed_is_empty and urls_new:
            # Output truncated by ``max_output_tokens``. The JSON is cut off
            # mid-string, so ``json.loads`` and the greedy ``\{.*\}`` regex
            # both fail and ``parsed`` is ``{}``. We fall through to the
            # per-image fallback below to recover the salvageable images
            # one at a time.
            collapsed = raw_text.strip().replace("\n", " ")
            head = collapsed[:300]
            tail = collapsed[-300:] if len(collapsed) > 600 else ""
            logger.warning(
                f"[OCR_COMBINED] [{trace_id}] Gemini output TRUNCATED at "
                f"max_output_tokens={OCR_MAX_OUTPUT_TOKENS}: "
                f"client={client_id} product={product_id} "
                f"images={len(urls_new)} raw_chars={len(raw_text)} "
                f"raw_head={head!r} raw_tail={tail!r}"
            )
        elif not raw_text.strip() and urls_new:
            # Empty body with no explicit block_reason — still worth noting
            # so we can distinguish "Gemini saw no text" from "Gemini was
            # silently filtered".
            logger.warning(
                f"[OCR_COMBINED] [{trace_id}] Gemini returned empty body "
                f"client={client_id} product={product_id} "
                f"images={len(urls_new)} (no block_reason set)"
            )
        elif parsed_is_empty and urls_new:
            # Third diagnostic: non-empty body that parsed cleanly but yielded
            # no per-image text and no summary. Common causes:
            #   - Response truncated by Gemini's max_output_tokens, leaving
            #     un-closed JSON that fails to parse.
            #   - Soft RAI / responsible-AI deflection without an explicit
            #     ``block_reason``.
            #   - Parser returned a non-empty dict whose ``per_image`` shape
            #     doesn't match our reader (e.g. list of strings, not list
            #     of {url, text} dicts).
            #
            # Log both ends of the raw body AND a summary of what
            # ``_parse_response`` actually returned, so we can disambiguate
            # truncation from RAI from shape mismatch in one diagnostic.
            collapsed = raw_text.strip().replace("\n", " ")
            head = collapsed[:500]
            tail = collapsed[-300:] if len(collapsed) > 800 else ""
            if isinstance(parsed, dict):
                parsed_shape = (
                    f"keys={sorted(parsed.keys())} "
                    f"per_image_n={len(parsed.get('per_image') or []) if isinstance(parsed.get('per_image'), list) else 'not-list'} "
                    f"summary_chars={len((parsed.get('summary') or '')) if isinstance(parsed.get('summary'), str) else 'not-str'}"
                )
            else:
                parsed_shape = f"type={type(parsed).__name__}"
            logger.warning(
                f"[OCR_COMBINED] [{trace_id}] Gemini parsed-but-empty response "
                f"client={client_id} product={product_id} "
                f"images={len(urls_new)} raw_chars={len(raw_text)} "
                f"parsed={parsed_shape} "
                f"raw_head={head!r} raw_tail={tail!r}"
            )

        # Unified per-image fallback for safety-block AND max-tokens
        # truncation. Per-image calls don't enter the degenerate loops that
        # combined calls hit on dense regulatory imagery, and the offending
        # image (for safety blocks) fails individually instead of poisoning
        # the whole product.
        if fallback_reason is not None:
            logger.info(
                f"[OCR_COMBINED] [{trace_id}] per-image fallback after "
                f"{fallback_reason}: client={client_id} product={product_id} "
                f"trying {len(urls_new)} URLs individually"
            )
            fallback_per_image = await self._per_image_ocr_fallback(
                urls_new, url_to_source, trace_id
            )
            if fallback_per_image:
                # Merge into ``parsed`` so the existing downstream path
                # picks them up as if the combined call had returned them.
                parsed = {
                    "per_image": [
                        {"url": u, "text": t}
                        for u, t in fallback_per_image.items()
                    ],
                    "summary": "",  # synthesized later from per-image text
                }
                parsed_per_image = parsed["per_image"]
                parsed_summary = ""
                parsed_is_empty = False
    except Exception as exc:
        logger.warning(
            f"[OCR_COMBINED] [{trace_id}] LLM call failed for "
            f"client={client_id} product={product_id}: {exc}"
        )
        # Best-effort: produce a summary deterministically from cached texts
        # alone so the caller still gets something.
        fallback_summary = "; ".join(
            line.strip()
            for url, text in cached_texts.items()
            for line in (text or "").splitlines()
            if line.strip()
        )[:PRODUCT_SUMMARY_MAX_CHARS]
        return fallback_summary, dict(cached_texts), False

    # Parse the structured response
    new_per_image: Dict[str, str] = {}
    per_image_list = parsed.get("per_image") or []
    if isinstance(per_image_list, list):
        # LLM might echo URLs as-is, or hallucinate slight variants. Match
        # tolerantly: first by exact URL, then by canonical URL, then by
        # positional order (fallback when LLM ignores URLs).
        echoed: List[Tuple[str, str]] = []
        for entry in per_image_list:
            if not isinstance(entry, dict):
                continue
            url_ret = (entry.get("url") or "").strip()
            text_ret = (entry.get("text") or "").strip()
            echoed.append((url_ret, text_ret))

        canonical_to_url = {u.split("?")[0]: u for u in urls_new}
        used_indices: Set[int] = set()
        for url_ret, text_ret in echoed:
            if url_ret in urls_new:
                new_per_image[url_ret] = text_ret
                continue
            canon = url_ret.split("?")[0]
            if canon in canonical_to_url:
                new_per_image[canonical_to_url[canon]] = text_ret
                continue
        # Positional fallback for entries the LLM didn't tag with a known URL
        leftover_urls = [u for u in urls_new if u not in new_per_image]
        leftover_entries = [
            (u, t) for (u, t) in echoed
            if u not in new_per_image and u.split("?")[0] not in canonical_to_url
        ]
        for url, (_, text_ret) in zip(leftover_urls, leftover_entries):
            new_per_image[url] = text_ret

    summary = (parsed.get("summary") or "").strip()
    summary = _belt_and_braces_stopword_strip(summary)
    if len(summary) > PRODUCT_SUMMARY_MAX_CHARS:
        truncated = summary[:PRODUCT_SUMMARY_MAX_CHARS]
        summary = truncated.rsplit("; ", 1)[0] or truncated

    # Merge cached + new per-image texts for the final per_image map.
    merged_per_image: Dict[str, str] = dict(cached_texts)
    merged_per_image.update(new_per_image)

    # Fix C: if the LLM produced valid per-image text but left the summary
    # empty (observed on small 2-image products in the May-20 Sereko run),
    # synthesise the summary deterministically from per-image text so
    # ``content.image_text`` is never blank for a product that actually has
    # OCR data. This is intentionally lossy / heuristic — the LLM summary is
    # always preferred when present.
    if not summary and any((v or "").strip() for v in merged_per_image.values()):
        synthesised = synthesise_summary_from_per_image(
            merged_per_image, max_chars=PRODUCT_SUMMARY_MAX_CHARS
        )
        if synthesised:
            logger.info(
                f"[OCR_COMBINED] [{trace_id}] LLM returned empty summary; "
                f"synthesised {len(synthesised)} chars deterministically from "
                f"per-image text for client={client_id} product={product_id}"
            )
            summary = synthesised

    # --- Persist results -------------------------------------------------
    # Per-image cache (cross-product reuse for shared images)
    if new_per_image:
        cache_rows = []
        for url, text in new_per_image.items():
            cache_rows.append({
                "url": url,
                "source": url_to_source.get(url, "gallery"),
                "extracted_text": text or "",
                "summary_terms": [],  # not used by the combined path
                "has_text": bool(text),
                "model": DEFAULT_MODEL_NAME,
                "prompt_version": OCR_PROMPT_VERSION,
            })
        try:
            await aupsert_ocr_results(client_id=client_id, rows=cache_rows)
        except Exception as exc:
            logger.warning(
                f"[OCR_COMBINED] [{trace_id}] per-image cache upsert failed: {exc}"
            )

    # Per-product summary cache.
    #
    # Skip the upsert when the result is empty (no summary text AND no
    # per-image text). An empty row is almost always a transient LLM
    # glitch or a soft RAI deflection — caching it would suppress OCR for
    # this product forever (until the URL set changes). Leaving the row
    # absent lets the next ingest / webhook / single-product call retry.
    has_any_text = bool(summary.strip()) or any(
        (v or "").strip() for v in merged_per_image.values()
    )
    if has_any_text:
        try:
            await aupsert_product_ocr_summary(
                client_id=client_id,
                product_id=product_id,
                url_set_sha256=url_set_hash,
                summary=summary,
                per_image_texts=merged_per_image,
                model=DEFAULT_MODEL_NAME,
            )
        except Exception as exc:
            logger.warning(
                f"[OCR_COMBINED] [{trace_id}] summary cache upsert failed: {exc}"
            )
    else:
        logger.info(
            f"[OCR_COMBINED] [{trace_id}] skipping tier-1 upsert: result is "
            f"empty for client={client_id} product={product_id} "
            f"(will retry on next call)"
        )

    return summary, merged_per_image, False


# Attach the new method to the class
ProductImageOCRExtractor.aextract_for_product_combined = (  # type: ignore[attr-defined]
    _aextract_for_product_combined_impl
)


# Lazy singleton — mirrors the pattern used by `get_product_attribute_extractor`.
_EXTRACTOR_SINGLETON: Optional[ProductImageOCRExtractor] = None


def get_product_image_ocr_extractor() -> ProductImageOCRExtractor:
    global _EXTRACTOR_SINGLETON
    if _EXTRACTOR_SINGLETON is None:
        _EXTRACTOR_SINGLETON = ProductImageOCRExtractor()
    return _EXTRACTOR_SINGLETON


# ----------------------------- Single-product helper -----------------------------
#
# Shared by:
#   - shopify/webhook/product_webhook.py
#   - services/product_ingestion/orchestrator.py::ingest_single_product
#
# Encapsulates the full decision tree for the single-product hot path:
#   1. Per-client flag gating.
#   2. URL collection (gallery + metafield, SVG-stripped, capped).
#   3. Three-tier cache lookup + bounded LLM call.
#   4. Shop-content cache read (no re-extraction on hot path).
#   5. Deterministic final composition of content.image_text.
#   6. Upstash-preserve fallback on every non-success branch — flag off,
#      no images, timeout, exception. Ensures a single-product upsert
#      NEVER accidentally wipes prior OCR data from Upstash.

# OCR LLM call timeouts (seconds) — sized per call site.
#
# - WEBHOOK_OCR_TIMEOUT_SECONDS: applies to the Shopify ``products/update``
#   webhook. The webhook itself is bounded by Shopify's 5 s response budget;
#   any over-run triggers Shopify retries, which our existing dedup
#   (``atry_claim_inventory_upsert`` + ``product_llm_cache``) absorbs
#   idempotently. 30 s comfortably covers the typical 10–20 s combined Gemini
#   call without making the retry cost unbounded.
#
# - SINGLE_PRODUCT_API_OCR_TIMEOUT_SECONDS: applies to
#   ``POST /api/v1/products/ingest/single``. This is a manual REST call with
#   no upstream response budget, so we give the LLM a generous window —
#   useful when re-ingesting a product whose OCR previously failed or when
#   manually debugging via curl.
#
# Full ingest / delta sync (``_run_image_ocr_pipeline``) does NOT use these
# constants — it issues ``aextract_for_product_combined`` directly with no
# ``wait_for`` wrapper, relying on the global ``OCR_CONCURRENCY`` semaphore
# for backpressure. Bumping the per-call budget here therefore covers the
# two surfaces (webhook + single-product) that need a hard ceiling.

WEBHOOK_OCR_TIMEOUT_SECONDS = 30.0
SINGLE_PRODUCT_API_OCR_TIMEOUT_SECONDS = 120.0

# Backwards-compat alias. Kept because ``aapply_ocr_to_product`` exposes
# this name in its default-argument signature; existing callers that import
# the constant directly should still work.
DEFAULT_SINGLE_PRODUCT_OCR_TIMEOUT_SECONDS = WEBHOOK_OCR_TIMEOUT_SECONDS


async def _apreserve_ocr_from_upstash(
    client_id: str,
    normalized_product: Any,
    trace_id: Optional[str],
    diagnostics: Dict[str, Any],
) -> None:
    """Fetch the existing Upstash doc and copy its OCR fields onto the
    product so a subsequent upsert doesn't blank them.

    Idempotent — if no existing doc, OCR fields remain at their defaults
    (empty), which is the right behaviour for a brand-new product.
    """
    try:
        from fashion_bot.services.product_ingestion.upstash_search_service import (
            UpstashSearchService,
        )
        search_svc = UpstashSearchService()
        existing_doc = await asyncio.to_thread(
            search_svc.fetch_document_by_id,
            client_id,
            normalized_product.id,
        )
    except Exception as exc:
        logger.warning(
            f"[OCR_SINGLE] [{trace_id}] failed to fetch existing Upstash doc "
            f"for product={normalized_product.id}: {exc}"
        )
        return

    if not existing_doc:
        diagnostics["preserved_from_upstash"] = False
        return

    existing_meta = existing_doc.get("metadata") or {}
    existing_content = existing_doc.get("content") or {}

    image_texts = existing_meta.get("image_texts") or {}
    image_summary = existing_content.get("image_text") or ""
    image_hash = existing_meta.get("image_ocr_hash") or ""

    normalized_product.image_ocr_texts = image_texts if isinstance(image_texts, dict) else {}
    normalized_product.image_ocr_summary = image_summary if isinstance(image_summary, str) else ""
    normalized_product.image_ocr_hash = image_hash if isinstance(image_hash, str) else ""

    has_data = bool(normalized_product.image_ocr_texts) or bool(normalized_product.image_ocr_summary)
    diagnostics["preserved_from_upstash"] = has_data
    if has_data:
        logger.info(
            f"[OCR_SINGLE] [{trace_id}] 🛡️ preserved existing OCR fields from "
            f"Upstash for product={normalized_product.id} "
            f"(images={len(normalized_product.image_ocr_texts)}, "
            f"summary_chars={len(normalized_product.image_ocr_summary)})"
        )


async def aapply_ocr_to_product(
    client_id: str,
    normalized_product: Any,
    *,
    trace_id: Optional[str] = None,
    timeout_seconds: float = DEFAULT_SINGLE_PRODUCT_OCR_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Apply OCR fields to ``normalized_product`` in place for single-product paths.

    Decision matrix (the helper handles all cases):

    | image_ocr_enabled | URLs collected | LLM result   | What we set on the product                   |
    |-------------------|----------------|--------------|----------------------------------------------|
    | false             | (skipped)      | (no call)    | OCR fields copied from existing Upstash doc  |
    | true              | 0              | (no call)    | OCR fields copied from existing Upstash doc  |
    | true              | ≥ 1            | Tier 1 hit   | summary from cache, no LLM call              |
    | true              | ≥ 1            | LLM ok       | fresh per_image_texts + composed summary     |
    | true              | ≥ 1            | LLM timeout  | OCR fields copied from existing Upstash doc  |
    | true              | ≥ 1            | LLM error    | OCR fields copied from existing Upstash doc  |

    Returns a diagnostics dict for the caller to log:
        {
            "enabled": bool,                        # flag value
            "ran_llm": bool,                        # combined call actually issued
            "tier1_hit": bool,                      # per-product summary cache hit
            "preserved_from_upstash": bool,         # existing OCR copied across
            "images_count": int,                    # URLs collected (post-cap)
            "summary_chars": int,                   # final image_ocr_summary length
            "image_texts_count": int,               # final image_ocr_texts size
            "timed_out": bool,                      # LLM wait_for timed out
            "error": Optional[str],                 # exception str on failure
        }

    Mutates ``normalized_product`` only — does not persist anything beyond
    the caches that ``aextract_for_product_combined`` already maintains.
    """
    diagnostics: Dict[str, Any] = {
        "enabled": False,
        "ran_llm": False,
        "tier1_hit": False,
        "preserved_from_upstash": False,
        "images_count": 0,
        "summary_chars": 0,
        "image_texts_count": 0,
        "timed_out": False,
        "error": None,
    }

    product_id = getattr(normalized_product, "id", None)
    if not product_id:
        diagnostics["error"] = "normalized_product.id is missing"
        return diagnostics

    # 1) Flag gate
    try:
        from fashion_bot.services.product_ingestion.image_ocr_config import (
            ais_image_ocr_enabled,
            ais_shop_content_extraction_enabled,
        )
        enabled = await ais_image_ocr_enabled(client_id)
    except Exception as exc:
        logger.warning(f"[OCR_SINGLE] [{trace_id}] flag lookup failed: {exc}")
        enabled = False
    diagnostics["enabled"] = enabled

    if not enabled:
        # Flag off — preserve any existing OCR fields so we don't wipe them.
        await _apreserve_ocr_from_upstash(
            client_id, normalized_product, trace_id, diagnostics
        )
        return _finalise_diagnostics(diagnostics, normalized_product)

    # 2) URL collection
    ocr_targets = collect_image_urls_for_ocr(
        image_url=getattr(normalized_product, "image_url", None),
        all_images=getattr(normalized_product, "all_images", None) or [],
        all_metafields=getattr(normalized_product, "all_metafields", None) or [],
    )
    diagnostics["images_count"] = len(ocr_targets)

    if not ocr_targets:
        # No OCR'able images. Defensive: still preserve in case Upstash has
        # OCR data from a prior state where images existed.
        await _apreserve_ocr_from_upstash(
            client_id, normalized_product, trace_id, diagnostics
        )
        return _finalise_diagnostics(diagnostics, normalized_product)

    # 3) Shop-content cache (read-only on single-product path)
    shop_promo_terms: List[str] = []
    shop_image_terms: List[str] = []
    shop_content_hash = ""
    try:
        if await ais_shop_content_extraction_enabled(client_id):
            from fashion_bot.services.product_ingestion.client_shop_content_store import (
                aget_shop_content,
            )
            shop_row = await aget_shop_content(client_id)
            shop_promo_terms = (shop_row or {}).get("promo_terms") or []
            shop_image_terms = (shop_row or {}).get("shop_image_terms") or []
            shop_content_hash = (shop_row or {}).get("content_hash") or ""
    except Exception as exc:
        logger.warning(
            f"[OCR_SINGLE] [{trace_id}] shop content cache read failed: {exc}"
        )

    # 4) Combined LLM call with timeout
    extractor = get_product_image_ocr_extractor()
    try:
        product_summary, per_image_texts, was_cached = await asyncio.wait_for(
            extractor.aextract_for_product_combined(
                client_id=client_id,
                product_id=str(product_id),
                image_urls=ocr_targets,
                trace_id=trace_id,
            ),
            timeout=timeout_seconds,
        )
        diagnostics["tier1_hit"] = bool(was_cached)
        diagnostics["ran_llm"] = not was_cached
        logger.info(
            f"[OCR_SINGLE] [{trace_id}] 🖼️ OCR combined call: client={client_id} "
            f"product={product_id} cached={was_cached} "
            f"images={len(per_image_texts)} summary_chars={len(product_summary)}"
        )
    except asyncio.TimeoutError:
        diagnostics["timed_out"] = True
        logger.warning(
            f"[OCR_SINGLE] [{trace_id}] ⏱️ OCR LLM call timed out after "
            f"{timeout_seconds}s for client={client_id} product={product_id} — "
            f"preserving existing Upstash OCR fields"
        )
        await _apreserve_ocr_from_upstash(
            client_id, normalized_product, trace_id, diagnostics
        )
        return _finalise_diagnostics(diagnostics, normalized_product)
    except Exception as exc:
        diagnostics["error"] = str(exc)
        logger.warning(
            f"[OCR_SINGLE] [{trace_id}] ⚠️ OCR call failed for "
            f"client={client_id} product={product_id}: {exc} — "
            f"preserving existing Upstash OCR fields"
        )
        await _apreserve_ocr_from_upstash(
            client_id, normalized_product, trace_id, diagnostics
        )
        return _finalise_diagnostics(diagnostics, normalized_product)

    # 5) Success — compose final image_text and apply to product
    normalized_product.image_ocr_texts = per_image_texts or {}
    normalized_product.image_ocr_summary = compose_final_image_text(
        shop_promo_terms=shop_promo_terms,
        shop_image_terms=shop_image_terms,
        product_summary=product_summary or "",
    )
    url_set_hash = compute_url_set_hash([u for u, _ in ocr_targets])
    if shop_content_hash:
        normalized_product.image_ocr_hash = url_set_hash + ":" + shop_content_hash[:16]
    else:
        normalized_product.image_ocr_hash = url_set_hash

    return _finalise_diagnostics(diagnostics, normalized_product)


def _finalise_diagnostics(
    diagnostics: Dict[str, Any],
    normalized_product: Any,
) -> Dict[str, Any]:
    """Stamp the final OCR field sizes into the diagnostics dict."""
    diagnostics["summary_chars"] = len(getattr(normalized_product, "image_ocr_summary", "") or "")
    diagnostics["image_texts_count"] = len(getattr(normalized_product, "image_ocr_texts", {}) or {})
    return diagnostics
