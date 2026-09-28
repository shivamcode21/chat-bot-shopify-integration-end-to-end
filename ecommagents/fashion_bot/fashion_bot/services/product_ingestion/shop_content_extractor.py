"""
Shop-Level Content Extractor — fetches one PDP from the storefront once per
delta-sync run, runs configurable CSS selectors over the rendered HTML, and
returns the shop-wide promo / announcement-bar text (e.g. "FREE Minis on
orders above ₹599", "India's 1st Psychodermatology brand").

This text is NOT in any product API response, so it can't be captured by
the per-product OCR path. We fold it into every product's ``content.image_text``
input during ingestion so shoppers can find products via shop-wide claims.

The extractor is NEVER invoked from the Shopify webhook hot path — webhooks
read the Postgres cache (see ``client_shop_content_store``).
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

from fashion_bot.services.product_ingestion.client_shop_content_store import (
    aupsert_shop_content,
    compute_promo_hash,
)
from fashion_bot.services.product_ingestion.image_ocr_config import (
    aget_shop_content_overrides,
    resolve_promo_selectors,
)
from fashion_bot.utils.http_client import get_shared_async_http_client

logger = logging.getLogger(__name__)


SHOP_CONTENT_TTL_SECONDS = 7 * 24 * 3600  # locked decision: 7 days
DEFAULT_FETCH_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_PROMO_CHARS = 600
_MIN_TERM_LEN = 4
_WHITESPACE_RX = re.compile(r"\s+")
_DEFAULT_EXCLUDE_PATTERNS = (r"^\s*$", r"^[\W_]+$")


@dataclass
class ShopContent:
    promo_terms: List[str] = field(default_factory=list)
    # Noun-phrase summary terms OCR'd from theme-rendered images embedded in the
    # storefront HTML (e.g. an "April offer" promo tile that lives in theme
    # settings and is not in product.images or metafields).
    shop_image_terms: List[str] = field(default_factory=list)
    content_hash: str = ""
    source_url: Optional[str] = None
    cached: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.promo_terms and not self.shop_image_terms


def _normalize_text(s: str) -> str:
    return _WHITESPACE_RX.sub(" ", s or "").strip()


def _passes_filters(text: str, exclude_patterns: Sequence[re.Pattern]) -> bool:
    if not text or len(text) < _MIN_TERM_LEN:
        return False
    for pat in exclude_patterns:
        if pat.search(text):
            return False
    return True


def _resolve_storefront_url(
    overrides: Dict[str, Any],
    shopify_domain: Optional[str],
    sample_handle: Optional[str],
) -> Optional[str]:
    override_domain = (overrides or {}).get("storefront_domain") if overrides else None
    domain = (override_domain or shopify_domain or "").strip()
    if not domain:
        return None
    if "://" not in domain:
        domain = f"https://{domain}"
    parsed = urlparse(domain)
    base = f"{parsed.scheme or 'https'}://{parsed.netloc or parsed.path}"
    if sample_handle:
        return f"{base.rstrip('/')}/products/{sample_handle}"
    # Fall back to the home page if no handle.
    return base.rstrip("/")


async def _fetch_pdp_html(url: str, timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS) -> Optional[str]:
    client = await get_shared_async_http_client()
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": "EcommerceAgent/1.0 (+shop-content-extractor)"},
            timeout=timeout,
        )
        if resp.status_code >= 400:
            logger.info(f"[SHOP_CONTENT] fetch {url} → HTTP {resp.status_code}")
            return None
        return resp.text
    except Exception as exc:
        logger.warning(f"[SHOP_CONTENT] fetch failed for {url}: {exc}")
        return None


def _extract_promo_from_html(
    html: str,
    selectors: Sequence[str],
    exclude_patterns: Sequence[re.Pattern],
    max_terms: int = 40,
) -> List[str]:
    try:
        # Vendored with BeautifulSoup4 — uses Python's stdlib html.parser, no lxml dep.
        from bs4 import BeautifulSoup
    except ImportError:
        logger.error("[SHOP_CONTENT] beautifulsoup4 missing; add to requirements.txt")
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.warning(f"[SHOP_CONTENT] HTML parse failed: {exc}")
        return []

    seen: List[str] = []
    seen_set: set = set()
    for selector in selectors:
        try:
            nodes = soup.select(selector)
        except Exception as exc:
            logger.debug(f"[SHOP_CONTENT] selector '{selector}' failed: {exc}")
            continue
        for node in nodes:
            text = _normalize_text(node.get_text(" ", strip=True))
            if not _passes_filters(text, exclude_patterns):
                continue
            key = text.lower()
            if key in seen_set:
                continue
            seen_set.add(key)
            seen.append(text)
            if len(seen) >= max_terms:
                return seen
    return seen


async def _ocr_shop_html_images(
    client_id: str,
    image_urls: List[str],
    product_gallery_urls: Optional[set] = None,
    trace_id: Optional[str] = None,
) -> List[str]:
    """OCR the shop-level (HTML-embedded) images once per ingestion and return
    deduped summary terms ready to fold into ``content.image_text``.

    These images live in the storefront theme — e.g. an "April offer" promo tile
    referenced from theme settings — and are NOT in any product's API response.
    We dedupe against ``product_gallery_urls`` so an image that happens to live
    in both places isn't OCR'd twice.
    """
    if not image_urls:
        return []
    try:
        from fashion_bot.services.product_ingestion.product_image_ocr_extractor import (
            get_product_image_ocr_extractor,
        )
    except Exception as exc:
        logger.warning(f"[SHOP_CONTENT] [{trace_id}] OCR extractor import failed: {exc}")
        return []

    gallery_canon = product_gallery_urls or set()
    targets: List[tuple] = []
    seen: set = set()
    for url in image_urls:
        canon = url.split("?")[0]
        if canon in gallery_canon or canon in seen:
            continue
        seen.add(canon)
        targets.append((url, "shop_html"))

    if not targets:
        return []

    extractor = get_product_image_ocr_extractor()
    try:
        results = await extractor.extract_batch_async(
            client_id=client_id,
            products=[("__shop_html__", targets)],
            trace_id=trace_id,
        )
    except Exception as exc:
        logger.warning(f"[SHOP_CONTENT] [{trace_id}] shop-image OCR failed: {exc}")
        return []

    bag: List[str] = []
    seen_terms: set = set()
    shop_result = results.get("__shop_html__")
    if not shop_result:
        return []
    for r in shop_result.per_image:
        if not r.extraction_success or not r.has_text:
            continue
        for t in r.summary_terms or []:
            v = (t or "").strip().lower()
            if v and v not in seen_terms:
                seen_terms.add(v)
                bag.append(v)
        if len(bag) >= 40:
            break
    logger.info(
        f"[SHOP_CONTENT] [{trace_id}] shop-image OCR: {len(targets)} URLs → "
        f"{len(bag)} summary terms"
    )
    return bag


async def extract_shop_content(
    client_id: str,
    shopify_domain: Optional[str] = None,
    sample_handle: Optional[str] = None,
    persist: bool = True,
    trace_id: Optional[str] = None,
    product_gallery_urls: Optional[set] = None,
) -> ShopContent:
    """Fetch the storefront PDP HTML, apply selectors, persist the resulting
    promo terms to ``client_shop_content_cache`` and return the result.

    On any failure returns an empty ``ShopContent`` and (if persist=True) does
    NOT overwrite the cached row — webhooks keep reading the last good value.
    """
    overrides = await aget_shop_content_overrides(client_id)
    selectors = resolve_promo_selectors(overrides)

    raw_exclude = (overrides or {}).get("exclude_text_patterns") or _DEFAULT_EXCLUDE_PATTERNS
    exclude_patterns: List[re.Pattern] = []
    for raw in raw_exclude:
        if not isinstance(raw, str):
            continue
        try:
            exclude_patterns.append(re.compile(raw))
        except re.error as exc:
            logger.warning(f"[SHOP_CONTENT] invalid exclude pattern '{raw}': {exc}")

    max_chars = int((overrides or {}).get("max_promo_chars") or DEFAULT_MAX_PROMO_CHARS)

    storefront_url = _resolve_storefront_url(overrides or {}, shopify_domain, sample_handle)
    if not storefront_url:
        logger.info(
            f"[SHOP_CONTENT] [{trace_id}] no storefront URL resolvable for "
            f"client {client_id} (domain={shopify_domain}, handle={sample_handle})"
        )
        return ShopContent()

    html = await _fetch_pdp_html(storefront_url)
    if not html and sample_handle:
        # Fall back to /products.json first handle if PDP 404s
        fallback_url = _resolve_storefront_url(overrides or {}, shopify_domain, None)
        if fallback_url and fallback_url != storefront_url:
            logger.info(
                f"[SHOP_CONTENT] [{trace_id}] retrying with home page {fallback_url}"
            )
            html = await _fetch_pdp_html(fallback_url)
            storefront_url = fallback_url

    if not html:
        return ShopContent(source_url=storefront_url)

    promo_terms = _extract_promo_from_html(html, selectors, exclude_patterns)
    # Cap total chars so we never blow Upstash's 4096 limit downstream.
    capped: List[str] = []
    running = 0
    for term in promo_terms:
        sep = 2 if capped else 0  # account for "; " joiner
        if running + sep + len(term) > max_chars:
            break
        capped.append(term)
        running += sep + len(term)

    # Collect <img src> URLs embedded in the HTML (theme-rendered promo tiles
    # like the april_offertile that aren't in product.images or metafields).
    # OCR them once per ingestion — they're shared across all products.
    shop_image_terms: List[str] = []
    try:
        from fashion_bot.services.product_ingestion.product_image_ocr_extractor import (
            collect_image_urls_from_html,
        )
        html_image_urls = collect_image_urls_from_html(html)
        if html_image_urls:
            shop_image_terms = await _ocr_shop_html_images(
                client_id=client_id,
                image_urls=html_image_urls,
                product_gallery_urls=product_gallery_urls,
                trace_id=trace_id,
            )
    except Exception as exc:
        logger.warning(
            f"[SHOP_CONTENT] [{trace_id}] shop-image collection failed: {exc}"
        )

    result = ShopContent(
        promo_terms=capped,
        shop_image_terms=shop_image_terms,
        content_hash=compute_promo_hash(capped, shop_image_terms),
        source_url=storefront_url,
        cached=False,
    )

    if persist and (capped or shop_image_terms):
        try:
            await aupsert_shop_content(
                client_id=client_id,
                promo_terms=capped,
                shop_image_terms=shop_image_terms,
                source_url=storefront_url,
            )
        except Exception as exc:
            logger.warning(
                f"[SHOP_CONTENT] [{trace_id}] persistence failed for {client_id}: {exc}"
            )

    logger.info(
        f"[SHOP_CONTENT] [{trace_id}] client={client_id} url={storefront_url} "
        f"text_terms={len(capped)} image_terms={len(shop_image_terms)}"
    )
    return result
