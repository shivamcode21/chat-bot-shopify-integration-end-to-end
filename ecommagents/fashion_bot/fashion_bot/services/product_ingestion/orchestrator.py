"""
Product Ingestion Orchestrator - Main orchestrator for product ingestion pipeline.

Coordinates the full ingestion workflow:
1. Determine product source (Shopify or JSON fallback)
2. Fetch all active products
3. Transform to search documents
4. Upsert to Upstash Search
"""

import asyncio
import gc
import logging
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from fashion_bot.env_loader import get_int

from fashion_bot.services.product_ingestion.models import (
    IngestionResult,
    FailedProduct,
    ProductIngestionResponse,
    IngestionStatusResponse,
    DeltaSyncResult,
    DeltaSyncResponse,
)
from fashion_bot.services.product_ingestion.product_source_factory import ProductSourceFactory
from fashion_bot.services.product_ingestion.upstash_search_service import UpstashSearchService
from fashion_bot.services.product_ingestion.sync_logger import (
    ProductSyncLogger,
    IngestionStatusTracker,
    FULL_INGESTION_STAGES,
    DELTA_SYNC_STAGES,
)

logger = logging.getLogger(__name__)

# Memory-bounding for delta sync (see delta_sync_products). The weekly delta
# can touch the whole catalog; materialising every product's enrichment
# (LLM prompt dicts, extracted attributes, serialized Search docs) at once is
# what drove the 7-Jun OOM on the webhook tier. We fan the two heavy stages out
# in fixed-size batches and gc between them so peak memory scales with the batch,
# not the window. Set ``PRODUCT_DELTA_SYNC_BATCH_SIZE=0`` to disable batching
# (restores the original single-pass behaviour); a window smaller than one batch
# is already a single pass.
DELTA_SYNC_UPSERT_BATCH_SIZE = get_int("PRODUCT_DELTA_SYNC_BATCH_SIZE", 250)


def _chunked(seq, size):
    """Yield successive ``size``-length slices of ``seq``.

    ``size <= 0`` disables chunking and yields the whole sequence once, so the
    caller's loop body runs exactly as it would have pre-batching.
    """
    if size and size > 0:
        for i in range(0, len(seq), size):
            yield seq[i:i + size]
    elif seq:
        yield list(seq)


def _build_image_text_excerpt(
    image_ocr_texts: Optional[Dict[str, str]],
    max_chars: int = 1500,
) -> str:
    """Concatenate per-image OCR text into one capped excerpt for the
    attribute-extraction prompt.

    Dedupes identical lines so a banner that bleeds into multiple gallery
    images doesn't repeat. Empty string when OCR is disabled or no images
    had text — the prompt path treats that as "no signal" and behaves as
    if OCR were never added.
    """
    if not image_ocr_texts:
        return ""
    lines: List[str] = []
    seen: Set[str] = set()
    for text in image_ocr_texts.values():
        if not text:
            continue
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            key = stripped.lower()
            if key in seen:
                continue
            seen.add(key)
            lines.append(stripped)
    excerpt = "\n".join(lines)
    if len(excerpt) > max_chars:
        truncated = excerpt[:max_chars]
        # Cut cleanly on a line boundary if one exists in the kept portion
        cut = truncated.rsplit("\n", 1)[0]
        excerpt = cut if cut else truncated
    return excerpt


def _normalized_product_to_extractor_dict(p) -> Dict[str, Any]:
    """Build a rich dict from NormalizedProduct for LLM attribute extraction.

    Passes all available product data so the LLM can accurately infer color,
    material, fit, pattern, etc. — even when the title is sparse.

    Includes per-product image OCR text (gallery + metafield image OCR) as
    ``image_text_excerpt`` when OCR has already run on this product;
    otherwise the field is an empty string and the extractor prompt behaves
    as if the field didn't exist.
    """
    options = [
        o for o in [
            {"name": "Color", "values": p.colors} if p.colors else None,
            {"name": "Size", "values": p.sizes} if p.sizes else None,
        ] if o is not None
    ]

    metafield_hints: Dict[str, str] = {}
    if p.fabric:
        metafield_hints["fabric"] = p.fabric
    if p.fit_type:
        metafield_hints["fit_type"] = p.fit_type
    if p.care_instructions:
        metafield_hints["care_instructions"] = p.care_instructions
    for attr_key, attr_val in p.metafield_attributes.items():
        if attr_key not in metafield_hints and attr_val:
            metafield_hints[attr_key] = str(attr_val)[:200]
    for mf in getattr(p, "all_metafields", []):
        key = mf.get("key", "")
        val = mf.get("value", "")
        if key and val and key not in metafield_hints:
            metafield_hints[key] = str(val)[:200]

    image_text_excerpt = _build_image_text_excerpt(
        getattr(p, "image_ocr_texts", None)
    )

    return {
        "title": p.title,
        "tags": p.tags,
        "collections": p.collections if p.collections else [],
        "category": p.product_type,
        "description": p.description,
        "template_suffix": "",
        "options": options,
        "vendor": p.vendor or "",
        "available_colors": p.available_colors if p.available_colors else [],
        "metafield_hints": metafield_hints,
        "image_text_excerpt": image_text_excerpt,
    }


class ProductIngestionOrchestrator:
    """
    Orchestrates the product ingestion pipeline.
    
    Main entry point for ingesting products to Upstash Search.
    """
    
    def __init__(self):
        """Initialize the orchestrator with required services."""
        self.source_factory = ProductSourceFactory()
        self.search_service = UpstashSearchService()

        self._ingestion_status: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _tag_bestsellers(
        products,
        sales_data: Optional[Dict[str, int]] = None,
        top_n: int = 3,
    ) -> int:
        """Mark top *top_n* in-stock products per subcategory as bestsellers.

        If *sales_data* (product GID -> qty sold) is provided, products within
        each subcategory are ranked by sales volume.  Otherwise the first
        *top_n* in-stock products per subcategory are tagged (order from the
        source, which is typically most-recently-updated first).

        Returns the number of products tagged.
        """
        by_subcat: Dict[str, list] = defaultdict(list)
        for p in products:
            subcat = (p.subcategory or p.product_type or "other").lower()
            by_subcat[subcat].append(p)

        tagged = 0
        for subcat, group in by_subcat.items():
            if sales_data:
                group.sort(
                    key=lambda p: sales_data.get(
                        p.id if p.id.startswith("gid://") else f"gid://shopify/Product/{p.id}",
                        0,
                    ),
                    reverse=True,
                )

            count = 0
            for p in group:
                if count >= top_n:
                    break
                if not p.in_stock:
                    continue
                p.bestseller = True
                count += 1
                tagged += 1

        return tagged

    @staticmethod
    def _apply_extracted_attributes(products, extracted_attrs):
        """Apply LLM-extracted attributes to NormalizedProduct objects."""
        for product, attrs in zip(products, extracted_attrs):
            if not attrs.extraction_success:
                continue
            product.base_product_name = attrs.base_product_name
            product.product_line = attrs.product_line
            product.product_line_normalized = attrs.product_line_normalized
            product.extracted_color = attrs.color
            product.material = attrs.material
            product.occasion = attrs.occasion or []
            product.style = attrs.style or []
            product.vibe = attrs.vibe or []
            product.pairing_tags = attrs.pairing_tags or []
            product.category = attrs.category or product.product_type
            product.subcategory = attrs.subcategory
            product.segment = attrs.segment
            product.color_family = attrs.color_family
            product.pattern = attrs.pattern
            if attrs.fit:
                product.fit_type = attrs.fit

    @staticmethod
    def _apply_cached_attrs_dict(product, attrs: Dict[str, Any]) -> None:
        """Apply a dict of cached attributes (from Postgres) onto a NormalizedProduct,
        preserving fields the LLM previously populated when we're re-upserting
        without a fresh attribute-extraction pass.
        """
        if not attrs:
            return
        if attrs.get("base_product_name"):
            product.base_product_name = attrs["base_product_name"]
        if attrs.get("product_line"):
            product.product_line = attrs["product_line"]
            product.product_line_normalized = (attrs["product_line"] or "").strip().lower()
        if attrs.get("color"):
            product.extracted_color = attrs["color"]
        if attrs.get("material"):
            product.material = attrs["material"]
        if attrs.get("category"):
            product.category = attrs["category"]
        if attrs.get("subcategory"):
            product.subcategory = attrs["subcategory"]
        if attrs.get("segment"):
            product.segment = attrs["segment"]
        if attrs.get("color_family"):
            product.color_family = attrs["color_family"]
        if attrs.get("pattern"):
            product.pattern = attrs["pattern"]
        if attrs.get("fit"):
            product.fit_type = attrs["fit"]
        for k in ("occasion", "style", "vibe", "pairing_tags"):
            v = attrs.get(k)
            if v:
                setattr(product, k, v if isinstance(v, list) else [])

    @staticmethod
    async def _identify_ocr_topup_products(
        client_id: str,
        unchanged_products,
        trace_id: Optional[str] = None,
    ):
        """Scan UNCHANGED bucket for products whose per-product OCR summary
        cache is missing or stale (URL set changed).

        Returns the subset that needs an OCR top-up. Cheap: one bulk Postgres
        query over the product summary cache.
        """
        if not unchanged_products:
            return []
        try:
            from fashion_bot.services.product_ingestion.product_image_ocr_extractor import (
                collect_image_urls_for_ocr,
            )
            from fashion_bot.services.product_ingestion.product_ocr_summary_store import (
                aget_product_ocr_summaries_bulk,
                compute_url_set_hash,
            )
        except Exception as exc:
            logger.warning(f"[OCR_TOPUP] [{trace_id}] import failed: {exc}")
            return []

        product_urls = []
        for p in unchanged_products:
            urls = collect_image_urls_for_ocr(
                image_url=p.image_url,
                all_images=p.all_images or [],
                all_metafields=p.all_metafields or [],
            )
            product_urls.append((p, [u for u, _ in urls]))

        candidate_products = [p for p, urls in product_urls if urls]
        if not candidate_products:
            return []

        cached_summaries = await aget_product_ocr_summaries_bulk(
            client_id=client_id,
            product_ids=[p.id for p in candidate_products],
        )

        topup = []
        for product, urls in product_urls:
            if not urls:
                continue
            row = cached_summaries.get(product.id)
            current_hash = compute_url_set_hash(urls)
            if not row or row.get("url_set_sha256") != current_hash:
                topup.append(product)
        return topup

    @staticmethod
    async def _fetch_pdp_size_chart_urls(
        products,
        selectors: Optional[List[str]] = None,
        storefront_base_url: Optional[str] = None,
        trace_id: Optional[str] = None,
        max_concurrent: int = 2,
        per_request_delay: float = 0.3,
    ) -> Dict[str, List[str]]:
        """Fetch storefront PDP pages and extract size chart image URLs.

        Returns ``{product_id: [url, ...]}`` for products that have size
        chart images in their PDP HTML. Products whose PDP fetch fails or
        whose page has no size chart container get an empty list (omitted
        from the dict).

        Deduplicates PDP fetches when multiple products share the same
        PDP page (e.g. colour variants mapped to the same handle).

        Includes per-request delay and retry with backoff to avoid
        storefront rate limiting (429/403) when scraping large catalogs.
        """
        if not products:
            return {}
        try:
            from fashion_bot.services.product_ingestion.product_image_ocr_extractor import (
                extract_size_chart_urls_from_html,
            )
            from fashion_bot.services.product_ingestion.storefront_http import (
                _get_once,
                _retry_after_seconds,
            )
        except Exception as exc:
            logger.warning(f"[SIZE_CHART] [{trace_id}] import failed: {exc}")
            return {}

        _MAX_RETRIES = 4
        _RETRY_BACKOFF_BASE = 2.0

        sem = asyncio.Semaphore(max_concurrent)
        html_cache: Dict[str, Optional[str]] = {}
        _fetch_fail_count = 0
        _fetch_success_count = 0

        async def _fetch_html(url: str) -> Optional[str]:
            nonlocal _fetch_fail_count, _fetch_success_count
            if url in html_cache:
                return html_cache[url]
            async with sem:
                await asyncio.sleep(per_request_delay)
                html: Optional[str] = None
                host = httpx.URL(url).host
                for attempt in range(_MAX_RETRIES):
                    try:
                        resp = await _get_once(url, params=None, timeout=15.0, host=host)
                        if resp.status_code < 400:
                            html = resp.text
                            _fetch_success_count += 1
                            break
                        if resp.status_code in (429, 503):
                            wait = _retry_after_seconds(
                                resp.headers, _RETRY_BACKOFF_BASE ** (attempt + 1)
                            )
                            logger.info(
                                f"[SIZE_CHART] [{trace_id}] {resp.status_code} for {url}, "
                                f"retry {attempt+1}/{_MAX_RETRIES} after {wait:.0f}s"
                            )
                            await asyncio.sleep(wait)
                            continue
                        logger.warning(
                            f"[SIZE_CHART] [{trace_id}] HTTP {resp.status_code} for {url}"
                        )
                        _fetch_fail_count += 1
                        break
                    except Exception as exc:
                        if attempt < _MAX_RETRIES - 1:
                            wait = _RETRY_BACKOFF_BASE ** (attempt + 1)
                            await asyncio.sleep(wait)
                            continue
                        logger.warning(
                            f"[SIZE_CHART] [{trace_id}] fetch failed for {url}: {exc}"
                        )
                        _fetch_fail_count += 1
                        break
            html_cache[url] = html
            return html

        async def _one(product) -> Tuple[str, List[str]]:
            product_url = getattr(product, "product_url", None) or ""
            if not product_url:
                return product.id, []
            fetch_url = product_url
            if storefront_base_url:
                handle = getattr(product, "handle", None)
                if handle:
                    fetch_url = f"{storefront_base_url.rstrip('/')}/products/{handle}"

            html = await _fetch_html(fetch_url)
            if not html:
                return product.id, []
            urls = extract_size_chart_urls_from_html(html, selectors=selectors)
            return product.id, urls

        results = await asyncio.gather(*[_one(p) for p in products])
        out: Dict[str, List[str]] = {}
        total_found = 0
        for pid, urls in results:
            if urls:
                out[pid] = urls
                total_found += len(urls)

        logger.info(
            f"[SIZE_CHART] [{trace_id}] PDP fetch stats: "
            f"{_fetch_success_count} ok, {_fetch_fail_count} failed, "
            f"{len(html_cache)} unique URLs"
        )
        if out:
            logger.info(
                f"[SIZE_CHART] [{trace_id}] extracted {total_found} size chart image URLs "
                f"from {len(out)}/{len(products)} products"
            )
        else:
            logger.warning(
                f"[SIZE_CHART] [{trace_id}] no size chart URLs found in any of "
                f"{len(products)} products"
            )
        return out

    @staticmethod
    async def _run_image_ocr_pipeline(
        client_id: str,
        products,
        shop_content_hash: str,
        shop_promo_terms,
        shop_image_terms=None,
        trace_id: Optional[str] = None,
        max_concurrent_products: int = 8,
        pdp_size_chart_urls_map: Optional[Dict[str, List[str]]] = None,
        size_chart_only: bool = False,
    ) -> int:
        """Run the per-product combined OCR + summary LLM call for each
        product and apply the results onto each one.

        For each product:
          * Tier 1: per-product summary cache — if URL set unchanged, reuse.
          * Tier 2: per-image cache — for URLs already OCR'd in any product.
          * Tier 3: one Gemini Flash Lite vision call per product with the
            remaining (new) images + cached texts as context. Output is a
            single product summary (≤ 1000 chars) + per-image text dict.

        ``shop_promo_terms`` and ``shop_image_terms`` are layered onto the
        per-product summary deterministically inside this helper so the
        cache key (URL set hash) stays product-only — meaning a shop-content
        update doesn't invalidate every product's cache.

        ``pdp_size_chart_urls_map`` maps ``product_id → [size_chart_url, ...]``
        for products whose storefront PDP page contains a size chart container.
        Populated by ``_fetch_pdp_size_chart_urls`` when the per-client
        ``pdp_size_chart_ocr_enabled`` flag is set.

        ``size_chart_only``: when True, skip gallery + metafield image
        collection and only OCR the PDP size chart images. Allows
        ``pdp_size_chart_ocr_enabled`` to work without ``image_ocr_enabled``.

        Returns the count of products that ended up with non-empty image
        OCR fields. Non-fatal: any failure leaves OCR fields empty.
        """
        if not products:
            return 0
        try:
            from fashion_bot.services.product_ingestion.product_image_ocr_extractor import (
                collect_image_urls_for_ocr,
                compose_final_image_text,
                get_product_image_ocr_extractor,
                size_chart_ocr_to_html_table,
            )
            from fashion_bot.services.product_ingestion.product_ocr_summary_store import (
                compute_url_set_hash,
            )
        except Exception as exc:
            logger.warning(f"[OCR_PIPELINE] [{trace_id}] extractor import failed: {exc}")
            return 0

        extractor = get_product_image_ocr_extractor()
        sem = asyncio.Semaphore(max_concurrent_products)
        sc_map = pdp_size_chart_urls_map or {}

        async def _one(product) -> Tuple[Any, str, Dict[str, str], List[Tuple[str, str]]]:
            sc_urls = sc_map.get(product.id)
            if size_chart_only:
                if not sc_urls:
                    return product, "", {}, []
                urls = collect_image_urls_for_ocr(
                    image_url=None,
                    all_images=[],
                    all_metafields=[],
                    pdp_size_chart_urls=sc_urls,
                )
            else:
                urls = collect_image_urls_for_ocr(
                    image_url=product.image_url,
                    all_images=product.all_images or [],
                    all_metafields=product.all_metafields or [],
                    pdp_size_chart_urls=sc_urls,
                )
            if not urls:
                return product, "", {}, urls
            try:
                async with sem:
                    summary, per_image_texts, _cached = (
                        await extractor.aextract_for_product_combined(
                            client_id=client_id,
                            product_id=product.id,
                            image_urls=urls,
                            trace_id=trace_id,
                        )
                    )
                return product, summary, per_image_texts, urls
            except Exception as exc:
                logger.warning(
                    f"[OCR_PIPELINE] [{trace_id}] product {product.id} failed: {exc}"
                )
                return product, "", {}, urls

        results = await asyncio.gather(*[_one(p) for p in products])

        applied = 0
        sc_populated = 0
        for product, product_summary, per_image_texts, urls in results:
            final_summary = compose_final_image_text(
                shop_promo_terms=shop_promo_terms or [],
                shop_image_terms=shop_image_terms or [],
                product_summary=product_summary or "",
            )
            product.image_ocr_texts = per_image_texts or {}
            product.image_ocr_summary = final_summary

            # Populate metadata.size_chart from OCR'd size chart images when
            # the product has no structured size chart from Shopify metafields.
            # Converts OCR text to an HTML table and wraps in the same
            # {"content": ...} dict shape that _extract_size_chart produces
            # for GID-resolved / plain-text metafield values so downstream
            # consumers (chat widget, find_product_by_id) render it correctly.
            if not getattr(product, "size_chart", None) and per_image_texts:
                sc_url_set = {u for u, src in (urls or []) if src == "pdp_size_chart"}
                if sc_url_set:
                    sc_texts = [
                        t for url, t in per_image_texts.items()
                        if t and url.split("?")[0] in {u.split("?")[0] for u in sc_url_set}
                    ]
                    if sc_texts:
                        html_parts = [
                            size_chart_ocr_to_html_table(t) for t in sc_texts
                        ]
                        html_combined = "\n".join(p for p in html_parts if p)
                        if html_combined:
                            product.size_chart = {
                                "content": html_combined,
                                "source": "pdp_image_ocr",
                            }
                            sc_populated += 1

            url_set_hash = compute_url_set_hash([u for u, _ in (urls or [])])
            if shop_content_hash:
                product.image_ocr_hash = url_set_hash + ":" + shop_content_hash[:16]
            else:
                product.image_ocr_hash = url_set_hash
            if product.image_ocr_texts or final_summary:
                applied += 1

        if sc_populated:
            logger.info(
                f"[OCR_PIPELINE] [{trace_id}] size_chart populated via PDP OCR "
                f"for {sc_populated}/{len(products)} products"
            )
        return applied

    async def ingest_products(
        self,
        client_id: str,
        source: str = "auto",
        force_refresh: bool = False,
        max_products: int = 0,
        raw_products: Optional[List[Dict]] = None,
        trace_id: Optional[str] = None,
        tracker: Optional[IngestionStatusTracker] = None,
    ) -> IngestionResult:
        """
        Main ingestion pipeline.

        Steps:
        1. Determine product source (Shopify or JSON fallback)
        2. Fetch all active products
        3. Extract structured attributes via LLM
        4. Persist attributes to Postgres
        5. Tag bestsellers
        6. Upsert to Upstash Search
        7. Populate Redis LLM cache

        Args:
            client_id: The client ID
            source: Source preference - "shopify", "json", or "auto"
            force_refresh: If True, replace the client's existing documents.
                The old docs are dropped only after new products have been
                fetched successfully, so a failed fetch leaves them in place.
            max_products: Cap total products (0 = unlimited, in-stock first)
            raw_products: Pre-fetched Shopify product dicts.  When supplied
                and the resolved service is ``ProductsJsonService``, the
                network fetch is skipped and these are normalised directly.
            trace_id: Optional run identifier for correlating sync log entries.
            tracker: Optional IngestionStatusTracker for progressive updates.

        Returns:
            IngestionResult with success/failure details
        """
        start_time = time.time()
        _t = tracker

        async def _step(name: str, status: str, msg: str = "", **kw):
            if _t:
                await _t.aupdate_step(name, status, msg, **kw)

        logger.info(f"🚀 Starting product ingestion for client {client_id}")
        logger.info(f"   Source: {source}, Force refresh: {force_refresh}")

        try:
            # Step 1: Get appropriate product service
            await _step("source_detection", "running", "Detecting product source...")
            product_service = await self.source_factory.aget_service(client_id, source)
            source_used = product_service.source_name
            logger.info(f"📦 Using {source_used} as product source")
            await _step("source_detection", "success", f"Using {source_used}")

            # Step 2: Fetch active products (capped if max_products > 0)
            #
            # This MUST happen before the force_refresh delete. Deleting first
            # meant any transient source failure (a storefront 429, an expired
            # Shopify token) emptied a live client's index with nothing to put
            # back — the catalog stayed dark until someone noticed and re-ran.
            # Fetching first makes the refresh atomic from the client's point
            # of view: we only drop the old docs once we hold new ones.
            await _step("product_fetch", "running", "Fetching products from source...")
            if raw_products is not None and hasattr(product_service, "normalize_raw_products"):
                logger.info(
                    f"📦 Using {len(raw_products)} pre-fetched products "
                    f"(skipping /products.json fetch)"
                )
                products = product_service.normalize_raw_products(
                    raw_products, max_products=max_products,
                )
            else:
                products = await product_service.fetch_active_products(
                    max_products=max_products,
                )

            if not products:
                logger.warning(f"⚠️ No products found for client {client_id}")
                # A full refresh that fetched nothing has not done its job.
                # Reporting it as success is how an empty catalog previously
                # passed for a healthy run, so fail loudly instead. The
                # existing index is deliberately left untouched.
                if force_refresh:
                    logger.error(
                        f"❌ Full refresh for client {client_id} fetched 0 products; "
                        f"leaving the existing Search index intact"
                    )
                    await _step("product_fetch", "failure",
                                "No products fetched — existing index left intact")
                else:
                    await _step("product_fetch", "success_with_warnings", "No products found")
                if _t:
                    for stage in ("llm_extraction", "attribute_persistence",
                                  "bestseller_tagging", "search_upsert", "redis_cache"):
                        await _step(stage, "skipped", "No products to process")
                    await _t.aderive_and_complete(time.time() - start_time)
                return IngestionResult(
                    success_count=0,
                    failed_count=1 if force_refresh else 0,
                    source_used=source_used,
                    duration=time.time() - start_time,
                    timestamp=datetime.utcnow(),
                    failed_details=(
                        [{"error": "Full refresh fetched 0 products; index left intact"}]
                        if force_refresh else []
                    ),
                )

            logger.info(f"📦 Fetched {len(products)} products")
            await _step("product_fetch", "success",
                        f"Fetched {len(products)} products",
                        products_fetched=len(products))

            # Step 3: Now that new products are in hand, drop the old docs.
            # Ordering matters — see the note above Step 2.
            if force_refresh:
                search_deleted = self.search_service.delete_all_for_client(client_id)
                logger.info(f"🗑️ Deleted {search_deleted} Search docs (force_refresh=True)")

            # Step 4: Image OCR + shop-level content (gated by per-client flags).
            # MUST run before attribute extraction so the OCR'd text can feed
            # the extractor prompt as `image_text_excerpt` (improves material /
            # vibe / occasion inference for image-heavy brands).
            # Non-fatal: failure leaves OCR fields empty; attribute extraction
            # then runs with an empty image_text_excerpt and behaves as today.
            try:
                from fashion_bot.services.product_ingestion.image_ocr_config import (
                    ais_image_ocr_enabled,
                    ais_shop_content_extraction_enabled,
                    ais_pdp_size_chart_ocr_enabled,
                    aget_pdp_size_chart_overrides,
                    resolve_size_chart_selectors,
                )
                full_ocr_enabled = await ais_image_ocr_enabled(client_id)
                sc_ocr_enabled = await ais_pdp_size_chart_ocr_enabled(client_id)

                if full_ocr_enabled or sc_ocr_enabled:
                    shop_promo_terms: List[str] = []
                    shop_image_terms: List[str] = []
                    shop_content_hash = ""

                    # Shop content extraction only makes sense with full OCR.
                    if full_ocr_enabled and await ais_shop_content_extraction_enabled(client_id):
                        try:
                            from fashion_bot.services.product_ingestion.shop_content_extractor import (
                                extract_shop_content,
                            )
                            from fashion_bot.config_manager import aget_shopify_config
                            shopify_cfg = await aget_shopify_config(client_id=client_id)
                            domain = shopify_cfg.get("shop_url") if shopify_cfg else None
                            sample_handle = (
                                products[0].handle if products and products[0].handle else None
                            )
                            gallery_canon = set()
                            for p in products:
                                if p.image_url:
                                    gallery_canon.add(p.image_url.split("?")[0])
                                for u in (p.all_images or []):
                                    gallery_canon.add(u.split("?")[0])
                            shop_content = await extract_shop_content(
                                client_id=client_id,
                                shopify_domain=domain,
                                sample_handle=sample_handle,
                                persist=True,
                                trace_id=trace_id,
                                product_gallery_urls=gallery_canon,
                            )
                            shop_promo_terms = shop_content.promo_terms
                            shop_image_terms = shop_content.shop_image_terms
                            shop_content_hash = shop_content.content_hash
                        except Exception as exc:
                            logger.warning(
                                f"[INGEST] [{trace_id}] shop content extraction failed: {exc}"
                            )

                    # PDP size chart image extraction (independent of full OCR).
                    pdp_sc_map: Dict[str, List[str]] = {}
                    if sc_ocr_enabled:
                        try:
                            sc_overrides = await aget_pdp_size_chart_overrides(client_id)
                            sc_selectors = resolve_size_chart_selectors(sc_overrides)
                            sc_storefront = (sc_overrides or {}).get("storefront_domain")
                            sc_base_url: Optional[str] = None
                            if sc_storefront:
                                d = sc_storefront.strip()
                                sc_base_url = d if "://" in d else f"https://{d}"
                            pdp_sc_map = await self._fetch_pdp_size_chart_urls(
                                products=products,
                                selectors=sc_selectors,
                                storefront_base_url=sc_base_url,
                                trace_id=trace_id,
                            )
                        except Exception as exc:
                            logger.warning(
                                f"[INGEST] [{trace_id}] PDP size chart extraction failed: {exc}"
                            )

                    ocr_count = await self._run_image_ocr_pipeline(
                        client_id=client_id,
                        products=products,
                        shop_content_hash=shop_content_hash,
                        shop_promo_terms=shop_promo_terms,
                        shop_image_terms=shop_image_terms,
                        trace_id=trace_id,
                        pdp_size_chart_urls_map=pdp_sc_map,
                        size_chart_only=not full_ocr_enabled,
                    )
                    logger.info(
                        f"[INGEST] [{trace_id}] image OCR applied to {ocr_count}/{len(products)} products"
                    )
            except Exception as exc:
                logger.warning(f"[INGEST] [{trace_id}] image OCR pipeline failed: {exc}")

            # Step 5: Extract structured attributes via LLM (Gemini 2.5 Flash Lite).
            # When image OCR ran above, per-product OCR text is passed in via
            # `image_text_excerpt` so attributes like material/vibe/occasion
            # can draw on claim copy embedded in gallery images.
            await _step("llm_extraction", "running",
                        f"Extracting attributes for {len(products)} products...")
            llm_success_count = 0
            try:
                from fashion_bot.services.product_ingestion.product_attribute_extractor import (
                    get_product_attribute_extractor,
                )
                extractor = get_product_attribute_extractor()

                product_dicts = []
                for p in products:
                    product_dicts.append(_normalized_product_to_extractor_dict(p))

                extracted_attrs = await extractor.extract_batch_async(
                    product_dicts, max_concurrent=10, client_id=client_id
                )

                self._apply_extracted_attributes(products, extracted_attrs)

                llm_success_count = sum(1 for a in extracted_attrs if a.extraction_success)
                logger.info(
                    f"🧠 LLM attribute extraction: {llm_success_count}/{len(products)} succeeded"
                )
                llm_status = "success" if llm_success_count == len(products) else "success_with_warnings"
                await _step("llm_extraction", llm_status,
                            f"{llm_success_count}/{len(products)} succeeded",
                            extracted=llm_success_count, total=len(products))

                # Persist extracted attributes to Postgres
                await _step("attribute_persistence", "running",
                            "Persisting attributes to Postgres...")
                try:
                    from fashion_bot.services.product_ingestion.product_attributes_store import (
                        aupsert_product_attributes,
                        aget_manually_edited_product_ids,
                    )
                    manual_edits = await aget_manually_edited_product_ids(client_id)

                    for product, attrs in zip(products, extracted_attrs):
                        if product.id in manual_edits:
                            self._apply_manual_attributes(product, manual_edits[product.id])
                            logger.debug(f"📝 Using manual overrides for {product.id}")
                            continue
                        if not attrs.extraction_success:
                            continue
                        await aupsert_product_attributes(
                            client_id=client_id,
                            product_id=product.id,
                            attrs={
                                "category": attrs.category,
                                "subcategory": attrs.subcategory,
                                "base_product_name": attrs.base_product_name,
                                "product_line": attrs.product_line,
                                "color": attrs.color,
                                "material": attrs.material,
                                "occasion": attrs.occasion or [],
                                "style": attrs.style or [],
                                "vibe": attrs.vibe or [],
                                "pairing_tags": attrs.pairing_tags or [],
                                "segment": attrs.segment,
                                "color_family": attrs.color_family,
                                "pattern": attrs.pattern,
                                "fit": attrs.fit,
                            },
                            product_title=product.title,
                            is_manually_edited=False,
                        )
                    logger.info("💾 Extracted attributes persisted to Postgres")
                    await _step("attribute_persistence", "success",
                                "Attributes persisted to Postgres")
                except Exception as persist_exc:
                    logger.warning(f"⚠️ Attribute persistence to Postgres failed: {persist_exc}")
                    await _step("attribute_persistence", "success_with_warnings",
                                f"Persistence failed: {persist_exc}")

            except Exception as e:
                logger.warning(
                    f"⚠️ LLM attribute extraction failed, proceeding without structured fields: {e}"
                )
                await _step("llm_extraction", "success_with_warnings",
                            f"Extraction failed: {e}")
                await _step("attribute_persistence", "skipped",
                            "Skipped (LLM extraction failed)")

            # Step 6: Tag bestsellers (top 3 in-stock per subcategory)
            await _step("bestseller_tagging", "running", "Tagging bestsellers...")
            sales_data: Optional[Dict[str, int]] = None
            try:
                from fashion_bot.services.product_ingestion.shopify_product_service import ShopifyProductService
                if isinstance(product_service, ShopifyProductService):
                    sales_data = await product_service.fetch_top_selling_product_ids(
                        top_n_per_subcategory=3, days=10,
                    )
                    if sales_data:
                        logger.info(f"📊 Fetched Shopify sales data for {len(sales_data)} products")
                    else:
                        logger.info("📊 No Shopify sales data found, using fallback ordering")
            except Exception as e:
                logger.warning(f"⚠️ Could not fetch Shopify sales data, using fallback: {e}")

            bestseller_count = self._tag_bestsellers(products, sales_data=sales_data, top_n=3)
            logger.info(f"⭐ Tagged {bestseller_count} products as bestsellers (top 3 per subcategory)")
            await _step("bestseller_tagging", "success",
                        f"Tagged {bestseller_count} bestsellers",
                        bestseller_count=bestseller_count)

            # Step 7: Upsert to Upstash Search
            await _step("search_upsert", "running",
                        f"Upserting {len(products)} docs to Upstash Search...")
            search_docs = [p.to_search_document(client_id) for p in products]
            search_result = await self.search_service.aupsert_documents(
                search_docs, client_id=client_id
            )
            upsert_success = search_result.get("success_count", 0)
            upsert_failed = search_result.get("failed", [])
            logger.info(
                f"📡 Upstash Search upsert: {upsert_success} docs"
            )
            upsert_status = "success" if not upsert_failed else "success_with_warnings"
            await _step("search_upsert", upsert_status,
                        f"{upsert_success} upserted, {len(upsert_failed)} failed",
                        upserted=upsert_success, failed=len(upsert_failed))

            # Step 8: Populate Redis LLM cache for webhook fast-path
            await _step("redis_cache", "running", "Populating Redis LLM cache...")
            try:
                from fashion_bot.services.product_ingestion.product_llm_cache import (
                    aset_cached_llm_data_bulk,
                )
                cached = await aset_cached_llm_data_bulk(
                    client_id,
                    search_docs,
                    # Docs the index rejected must not carry an index receipt,
                    # or their next webhook reads them as already-current.
                    failed_doc_ids={
                        f.get("product_id") for f in upsert_failed if f.get("product_id")
                    },
                )
                logger.info(f"📡 Redis LLM cache: populated {cached} product entries")
                await _step("redis_cache", "success",
                            f"Cached {cached} product entries", cached=cached)
            except Exception as e:
                logger.warning(f"⚠️ Redis LLM cache bulk write failed: {e}")
                await _step("redis_cache", "success_with_warnings",
                            f"Cache write failed: {e}")

            duration = time.time() - start_time

            failed_details = [
                FailedProduct(product_id=f["product_id"], error=f["error"])
                for f in upsert_failed
            ]

            result = IngestionResult(
                success_count=upsert_success,
                failed_count=len(failed_details),
                source_used=source_used,
                duration=duration,
                timestamp=datetime.utcnow(),
                failed_details=failed_details
            )

            self._ingestion_status[client_id] = {
                "last_ingestion": result.timestamp.isoformat(),
                "product_count": result.success_count,
                "source_used": source_used,
                "duration": duration
            }

            logger.info(f"✅ Ingestion complete: {result.success_count} products, "
                       f"{result.failed_count} failed, {duration:.2f}s")

            status = (
                ProductSyncLogger.STATUS_SUCCESS if result.failed_count == 0
                else ProductSyncLogger.STATUS_PARTIAL_FAILURE if result.success_count > 0
                else ProductSyncLogger.STATUS_FAILURE
            )

            if _t:
                await _t.aupdate_counters(
                    products_added=result.success_count,
                    products_failed=result.failed_count,
                )
                await _t.acomplete(status, duration)
            else:
                await ProductSyncLogger.alog_manual_api_event(
                    client_id=client_id,
                    sync_type="full_ingestion",
                    status=status,
                    products_added=result.success_count,
                    products_failed=result.failed_count,
                    duration_seconds=duration,
                    trace_id=trace_id,
                )

            return result

        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"❌ Ingestion failed for client {client_id}: {e}")

            if _t:
                await _t.aupdate_counters(
                    products_failed=1, error_message=str(e),
                )
                await _t.acomplete("failed", duration)
            else:
                await ProductSyncLogger.alog_manual_api_event(
                    client_id=client_id,
                    sync_type="full_ingestion",
                    status=ProductSyncLogger.STATUS_FAILURE,
                    products_failed=1,
                    error_message=str(e),
                    duration_seconds=duration,
                    trace_id=trace_id,
                )

            return IngestionResult(
                success_count=0,
                failed_count=1,
                source_used=source if source != "auto" else "unknown",
                duration=duration,
                timestamp=datetime.utcnow(),
                failed_details=[FailedProduct(product_id="*", error=str(e))]
            )
    
    def get_status(self, client_id: str) -> IngestionStatusResponse:
        """
        Get ingestion status for a client.
        
        Args:
            client_id: The client ID
            
        Returns:
            IngestionStatusResponse with status details
        """
        # Check in-memory cache first
        cached_status = self._ingestion_status.get(client_id)

        if cached_status:
            current_count = self.search_service.get_document_count(client_id)

            return IngestionStatusResponse(
                client_id=client_id,
                last_ingestion=cached_status.get("last_ingestion"),
                product_count=current_count,
                source_used=cached_status.get("source_used"),
                index_health="healthy" if self.search_service.health_check() else "unhealthy"
            )

        current_count = self.search_service.get_document_count(client_id)

        return IngestionStatusResponse(
            client_id=client_id,
            last_ingestion=None,
            product_count=current_count,
            source_used=None,
            index_health="healthy" if self.search_service.health_check() else "unhealthy"
        )
    
    def to_response(self, result: IngestionResult, client_id: str) -> ProductIngestionResponse:
        """
        Convert IngestionResult to API response.
        
        Args:
            result: The IngestionResult
            client_id: The client ID
            
        Returns:
            ProductIngestionResponse for API
        """
        return ProductIngestionResponse(
            success=result.success,
            client_id=client_id,
            source_used=result.source_used,
            products_ingested=result.success_count,
            products_failed=result.failed_count,
            duration_seconds=round(result.duration, 2),
            timestamp=result.timestamp.isoformat(),
            failed_products=[
                {"product_id": f.product_id, "error": f.error}
                for f in result.failed_details
            ]
        )

    async def delta_sync_products(
        self,
        client_id: str,
        source: str = "auto",
        hours: int = 168,  # Default to 7 days (168 hours) for weekly sync
        tracker: Optional[IngestionStatusTracker] = None,
    ) -> DeltaSyncResult:
        """
        Delta sync: Only update changed products in the Search index.

        Compares products from source (Shopify) with existing Search docs:
        - ADD: New products not in index
        - UPDATE: Products with changed content (based on content_hash)
        - DELETE: Products in index but removed from source
        - UNCHANGED: Products with same content_hash (skip)

        Args:
            client_id: The client ID
            source: Source preference - "shopify", "json", or "auto"
            hours: Number of hours to look back for updated products (default: 168 = 7 days)

        Returns:
            DeltaSyncResult with detailed sync statistics
        """
        start_time = time.time()
        _t = tracker

        async def _step(name: str, status: str, msg: str = "", **kw):
            if _t:
                await _t.aupdate_step(name, status, msg, **kw)

        logger.info(f"🔄 Starting delta sync for client {client_id} (looking back {hours} hours)")

        result = DeltaSyncResult(timestamp=datetime.utcnow())

        try:
            # Step 1: Get product service
            await _step("source_detection", "running", "Detecting product source...")
            product_service = await self.source_factory.aget_service(client_id, source)
            result.source_used = product_service.source_name
            logger.info(f"📦 Using {result.source_used} as product source")
            await _step("source_detection", "success", f"Using {result.source_used}")

            # Step 2: Fetch recently updated products
            await _step("product_fetch", "running",
                        f"Fetching products updated in last {hours}h...")
            source_products = await product_service.fetch_recently_updated_products(hours=hours)

            if not source_products:
                logger.info(f"ℹ️ No products updated in last {hours} hours for client {client_id}")
                result.duration = time.time() - start_time
                await _step("product_fetch", "success", "No products updated")
                if _t:
                    for stage in ("hash_comparison", "llm_extraction",
                                  "attribute_persistence", "bestseller_tagging",
                                  "search_upsert", "redis_cache"):
                        await _step(stage, "skipped", "No products to process")
                    await _t.aderive_and_complete(result.duration)
                return result

            logger.info(f"📦 Fetched {len(source_products)} recently updated products from source")
            await _step("product_fetch", "success",
                        f"Fetched {len(source_products)} products",
                        products_fetched=len(source_products))

            # Step 3: Compute content hashes and compare
            await _step("hash_comparison", "running", "Comparing content hashes...")
            source_hashes = {}
            source_product_map = {}
            for product in source_products:
                content_hash = product.compute_content_hash_excluding_inventory()
                source_hashes[product.id] = content_hash
                source_product_map[product.id] = product

            source_ids = set(source_hashes.keys())
            existing_hashes = self.search_service.get_existing_product_hashes(client_id)

            relevant_existing_hashes = {
                pid: h for pid, h in existing_hashes.items()
                if pid in source_ids
            }

            logger.info(f"📋 Found {len(relevant_existing_hashes)} existing products in Search index (of {len(source_ids)} updated)")

            existing_ids = set(relevant_existing_hashes.keys())
            products_to_add = source_ids - existing_ids
            products_to_delete = set()

            products_to_check = source_ids & existing_ids
            products_to_update = set()
            products_unchanged = set()

            for product_id in products_to_check:
                source_hash = source_hashes.get(product_id, "")
                existing_hash = relevant_existing_hashes.get(product_id, "")

                if source_hash != existing_hash:
                    products_to_update.add(product_id)
                    product_title = source_product_map.get(product_id, None)
                    title = product_title.title if product_title else product_id
                    logger.info(f"🔀 Hash mismatch for '{title}': source={source_hash[:8]}… stored={existing_hash[:8]}…")
                else:
                    products_unchanged.add(product_id)

            # For hash-unchanged products, check Redis for stock status drift
            products_stock_update: set = set()
            stock_update_llm_fields: Dict[str, Dict] = {}
            try:
                from fashion_bot.services.product_ingestion.product_llm_cache import (
                    aget_cached_product_data,
                    stock_status_changed,
                )
                for product_id in list(products_unchanged):
                    product = source_product_map.get(product_id)
                    if not product:
                        continue
                    cached = await aget_cached_product_data(client_id, product_id)
                    if not cached:
                        continue
                    new_sizes = list(product.available_sizes) if product.available_sizes else []
                    new_colors = list(product.available_colors) if product.available_colors else []
                    if stock_status_changed(cached, product.in_stock, new_sizes, new_colors):
                        products_stock_update.add(product_id)
                        products_unchanged.discard(product_id)
                        stock_update_llm_fields[product_id] = cached.get("llm_fields") or {}
                        logger.info(
                            f"📦 Stock status changed for '{product.title}': "
                            f"in_stock={product.in_stock}, sizes={new_sizes}"
                        )
            except Exception as exc:
                logger.warning(f"⚠️ Delta sync stock status check failed: {exc}")

            logger.info(f"📊 Delta analysis: "
                       f"ADD={len(products_to_add)}, "
                       f"UPDATE={len(products_to_update)}, "
                       f"STOCK_UPDATE={len(products_stock_update)}, "
                       f"DELETE={len(products_to_delete)} (not supported in time-based sync), "
                       f"UNCHANGED={len(products_unchanged)}")
            await _step("hash_comparison", "success",
                        f"ADD={len(products_to_add)}, UPDATE={len(products_to_update)}, STOCK_UPDATE={len(products_stock_update)}, UNCHANGED={len(products_unchanged)}",
                        to_add=len(products_to_add),
                        to_update=len(products_to_update),
                        stock_update=len(products_stock_update),
                        unchanged=len(products_unchanged))
            
            # Step 6: Process deletions
            if products_to_delete:
                delete_ids = [f"{client_id}_{pid}" for pid in products_to_delete]
                deleted_count = self.search_service.delete_documents(client_id, ids=delete_ids)
                result.products_deleted = deleted_count
                logger.info(f"🗑️ Deleted {deleted_count} removed products")
            
            # Step 6b: Apply cached LLM fields to stock-only updates
            for pid in products_stock_update:
                product = source_product_map.get(pid)
                if product and pid in stock_update_llm_fields:
                    llm = stock_update_llm_fields[pid]
                    product.base_product_name = llm.get("base_product_name") or product.base_product_name
                    product.product_line = llm.get("product_line") or product.product_line
                    product.product_line_normalized = llm.get("product_line_normalized") or product.product_line_normalized
                    product.extracted_color = llm.get("extracted_color") or product.extracted_color
                    product.material = llm.get("material") or product.material
                    product.occasion = llm.get("occasion") or product.occasion
                    product.style = llm.get("style") or product.style
                    product.vibe = llm.get("vibe") or product.vibe
                    product.pairing_tags = llm.get("pairing_tags") or product.pairing_tags
                    product.category = llm.get("category") or product.category
                    product.subcategory = llm.get("subcategory") or product.subcategory
                    product.segment = llm.get("segment") or product.segment
                    product.color_family = llm.get("color_family") or product.color_family
                    product.pattern = llm.get("pattern") or product.pattern
                    if llm.get("fit"):
                        product.fit_type = llm["fit"]

            # Step 7: Process additions and updates
            products_to_upsert = products_to_add | products_to_update

            if products_to_upsert or products_stock_update:
                # Content-changed products need LLM extraction; stock-only
                # products will reuse cached LLM fields applied above.
                content_changed_products = [
                    source_product_map[pid] for pid in products_to_upsert
                    if pid in source_product_map
                ]
                stock_only_products = [
                    source_product_map[pid] for pid in products_stock_update
                    if pid in source_product_map
                ]

                # Peak-memory cap for the two heavy stages below (LLM extraction
                # and Search upsert). batch_size<=0 (or a window that fits in one
                # batch) keeps the original single-pass behaviour byte-for-byte.
                batch_size = DELTA_SYNC_UPSERT_BATCH_SIZE

                # ---- Image OCR + shop-content extraction (MUST run before
                # attribute extraction so OCR text can feed `image_text_excerpt`
                # in the extractor prompt). Also drags in UNCHANGED products
                # with missing OCR cache rows so a newly-enabled flag back-fills
                # without forcing a full ingestion.
                ocr_topup_products = []
                try:
                    from fashion_bot.services.product_ingestion.image_ocr_config import (
                        ais_image_ocr_enabled,
                        ais_shop_content_extraction_enabled,
                        ais_pdp_size_chart_ocr_enabled,
                        aget_pdp_size_chart_overrides,
                        resolve_size_chart_selectors,
                    )
                    full_ocr_enabled = await ais_image_ocr_enabled(client_id)
                    sc_ocr_enabled = await ais_pdp_size_chart_ocr_enabled(client_id)

                    if full_ocr_enabled or sc_ocr_enabled:
                        shop_promo_terms: List[str] = []
                        shop_image_terms: List[str] = []
                        shop_content_hash = ""

                        if full_ocr_enabled and await ais_shop_content_extraction_enabled(client_id):
                            try:
                                from fashion_bot.services.product_ingestion.shop_content_extractor import (
                                    extract_shop_content,
                                )
                                from fashion_bot.config_manager import aget_shopify_config
                                shopify_cfg = await aget_shopify_config(client_id=client_id)
                                domain = shopify_cfg.get("shop_url") if shopify_cfg else None
                                sample_handle = None
                                for candidate in source_products or []:
                                    if getattr(candidate, "handle", None):
                                        sample_handle = candidate.handle
                                        break
                                gallery_canon = set()
                                for candidate in source_products or []:
                                    img_url = getattr(candidate, "image_url", None)
                                    if img_url:
                                        gallery_canon.add(img_url.split("?")[0])
                                    for u in (getattr(candidate, "all_images", None) or []):
                                        gallery_canon.add(u.split("?")[0])
                                shop_content = await extract_shop_content(
                                    client_id=client_id,
                                    shopify_domain=domain,
                                    sample_handle=sample_handle,
                                    persist=True,
                                    trace_id=None,
                                    product_gallery_urls=gallery_canon,
                                )
                                shop_promo_terms = shop_content.promo_terms
                                shop_image_terms = shop_content.shop_image_terms
                                shop_content_hash = shop_content.content_hash
                            except Exception as exc:
                                logger.warning(f"[DELTA_OCR] shop content extraction failed: {exc}")

                        # UNCHANGED top-up (only relevant when full OCR is on)
                        if full_ocr_enabled:
                            unchanged_products = [
                                source_product_map[pid] for pid in products_unchanged
                                if pid in source_product_map
                            ]
                            ocr_topup_products = await self._identify_ocr_topup_products(
                                client_id=client_id,
                                unchanged_products=unchanged_products,
                            )
                            if ocr_topup_products:
                                logger.info(
                                    f"📥 OCR top-up: {len(ocr_topup_products)} UNCHANGED products "
                                    f"need image OCR (cache cold)"
                                )
                                try:
                                    from fashion_bot.services.product_ingestion.product_attributes_store import (
                                        aget_product_attributes,
                                    )
                                    for product in ocr_topup_products:
                                        cached_attrs = await aget_product_attributes(
                                            client_id, product.id
                                        )
                                        if cached_attrs:
                                            self._apply_cached_attrs_dict(product, cached_attrs)
                                except Exception as exc:
                                    logger.warning(
                                        f"[DELTA_OCR] cached attrs load failed: {exc}"
                                    )

                        ocr_targets = (
                            list(content_changed_products)
                            + list(stock_only_products)
                            + list(ocr_topup_products)
                        )

                        # PDP size chart image extraction (independent of full OCR).
                        pdp_sc_map: Dict[str, List[str]] = {}
                        if ocr_targets and sc_ocr_enabled:
                            try:
                                sc_overrides = await aget_pdp_size_chart_overrides(client_id)
                                sc_selectors = resolve_size_chart_selectors(sc_overrides)
                                sc_storefront = (sc_overrides or {}).get("storefront_domain")
                                sc_base_url: Optional[str] = None
                                if sc_storefront:
                                    d = sc_storefront.strip()
                                    sc_base_url = d if "://" in d else f"https://{d}"
                                pdp_sc_map = await self._fetch_pdp_size_chart_urls(
                                    products=ocr_targets,
                                    selectors=sc_selectors,
                                    storefront_base_url=sc_base_url,
                                    trace_id=None,
                                )
                            except Exception as exc:
                                logger.warning(
                                    f"[DELTA_OCR] PDP size chart extraction failed: {exc}"
                                )

                        if ocr_targets:
                            applied = await self._run_image_ocr_pipeline(
                                client_id=client_id,
                                products=ocr_targets,
                                shop_content_hash=shop_content_hash,
                                shop_promo_terms=shop_promo_terms,
                                shop_image_terms=shop_image_terms,
                                trace_id=None,
                                pdp_size_chart_urls_map=pdp_sc_map,
                                size_chart_only=not full_ocr_enabled,
                            )
                            logger.info(
                                f"[DELTA_OCR] image OCR applied to {applied}/{len(ocr_targets)} products"
                            )
                except Exception as exc:
                    logger.warning(f"[DELTA_OCR] pipeline failed (non-fatal): {exc}")

                # ---- LLM attribute extraction (reads image_text_excerpt from
                # the per-product OCR texts populated above).
                if content_changed_products:
                    await _step("llm_extraction", "running",
                                f"Extracting attributes for {len(content_changed_products)} products...")
                    try:
                        from fashion_bot.services.product_ingestion.product_attribute_extractor import (
                            get_product_attribute_extractor,
                        )
                        from fashion_bot.services.product_ingestion.product_attributes_store import (
                            aupsert_product_attributes,
                            aget_manually_edited_product_ids,
                        )
                        extractor = get_product_attribute_extractor()
                        # Manual-edit overrides are a small, client-wide set —
                        # fetch once and reuse across batches. Fail-soft: a lookup
                        # error must not discard the whole extraction pass.
                        try:
                            manual_edits = await aget_manually_edited_product_ids(client_id)
                        except Exception as me_exc:
                            logger.warning(f"⚠️ Delta sync: manual-edit lookup failed: {me_exc}")
                            manual_edits = {}

                        total_cc = len(content_changed_products)
                        attr_ok = 0
                        persist_failed_exc: Optional[Exception] = None
                        await _step("attribute_persistence", "running",
                                    "Persisting attributes to Postgres...")

                        # Extract + persist one bounded batch at a time. The
                        # prompt dicts and extracted attributes for the whole
                        # window are never resident at once.
                        for _batch in _chunked(content_changed_products, batch_size):
                            product_dicts = [
                                _normalized_product_to_extractor_dict(p) for p in _batch
                            ]
                            extracted_attrs = await extractor.extract_batch_async(
                                product_dicts, max_concurrent=10, client_id=client_id
                            )
                            self._apply_extracted_attributes(_batch, extracted_attrs)
                            attr_ok += sum(1 for a in extracted_attrs if a.extraction_success)

                            try:
                                for product, attrs in zip(_batch, extracted_attrs):
                                    if product.id in manual_edits:
                                        self._apply_manual_attributes(product, manual_edits[product.id])
                                        continue
                                    if not attrs.extraction_success:
                                        continue
                                    await aupsert_product_attributes(
                                        client_id=client_id,
                                        product_id=product.id,
                                        attrs={
                                            "category": attrs.category,
                                            "subcategory": attrs.subcategory,
                                            "base_product_name": attrs.base_product_name,
                                            "product_line": attrs.product_line,
                                            "color": attrs.color,
                                            "material": attrs.material,
                                            "occasion": attrs.occasion or [],
                                            "style": attrs.style or [],
                                            "vibe": attrs.vibe or [],
                                            "pairing_tags": attrs.pairing_tags or [],
                                            "segment": attrs.segment,
                                            "color_family": attrs.color_family,
                                            "pattern": attrs.pattern,
                                            "fit": attrs.fit,
                                        },
                                        product_title=product.title,
                                        is_manually_edited=False,
                                    )
                            except Exception as persist_exc:
                                # Remember the failure; keep going so other
                                # batches still persist. Reported once below.
                                persist_failed_exc = persist_exc

                            # Release the per-batch transients before the next batch.
                            del product_dicts, extracted_attrs
                            gc.collect()

                        logger.info(f"🧠 Delta sync LLM extraction: {attr_ok}/{total_cc} succeeded")
                        llm_st = "success" if attr_ok == total_cc else "success_with_warnings"
                        await _step("llm_extraction", llm_st,
                                    f"{attr_ok}/{total_cc} succeeded",
                                    extracted=attr_ok, total=total_cc)

                        if persist_failed_exc is not None:
                            logger.warning(f"⚠️ Delta sync attribute persistence failed: {persist_failed_exc}")
                            await _step("attribute_persistence", "success_with_warnings",
                                        f"Persistence failed: {persist_failed_exc}")
                        else:
                            await _step("attribute_persistence", "success",
                                        "Attributes persisted to Postgres")

                    except Exception as e:
                        logger.warning(f"⚠️ Delta sync LLM extraction failed, proceeding without: {e}")
                        await _step("llm_extraction", "success_with_warnings",
                                    f"Extraction failed: {e}")
                        await _step("attribute_persistence", "skipped",
                                    "Skipped (LLM extraction failed)")
                else:
                    await _step("llm_extraction", "skipped",
                                f"Only stock updates ({len(products_stock_update)} products)")
                    await _step("attribute_persistence", "skipped",
                                "Only stock updates")

                # Final upsert list: content-changed + stock-only + OCR top-ups
                all_upsert_products = (
                    list(content_changed_products)
                    + list(stock_only_products)
                    + list(ocr_topup_products)
                )
                if stock_only_products:
                    logger.info(f"📦 Adding {len(stock_only_products)} stock-only updates to upsert batch (LLM skipped, using Redis cache)")

                # Bestseller flags are owned SOLELY by the monthly
                # `bestseller_refresh` cron (full 90-day Shopify-sales
                # reconciliation, including demotion). Delta sync must NOT decide
                # bestsellers. However, an Upstash upsert REPLACES the whole
                # document, so writing these docs with the default
                # `bestseller=False` would silently clear a flag the monthly job
                # set. To stay neutral, carry forward the flag already in the
                # index for products being re-upserted; products not yet indexed
                # default to False and are picked up by the next monthly refresh.
                await _step("bestseller_tagging", "skipped",
                            "Bestseller flag owned by monthly bestseller_refresh cron")
                try:
                    indexed_upsert_ids = [
                        p.id for p in all_upsert_products if p.id in existing_ids
                    ]
                    flagged: Set[str] = set()
                    # Engagement analytics (unique_sessions / units_sold /
                    # conversion_rate) are ALSO owned solely by the monthly
                    # bestseller_refresh cron. Like the flag, a delta re-upsert with
                    # their defaults (None → stripped) would silently drop them, so
                    # carry the existing values forward per product id.
                    _ANALYTICS_FIELDS = ("unique_sessions", "units_sold", "conversion_rate")
                    analytics_by_pid: Dict[str, Dict[str, Any]] = {}
                    for _i in range(0, len(indexed_upsert_ids), 100):
                        chunk = indexed_upsert_ids[_i:_i + 100]
                        for _doc in self.search_service.fetch_documents(
                            client_id, product_ids=chunk
                        ):
                            _pid = (_doc.get("metadata") or {}).get("product_id", "")
                            if not _pid:
                                continue
                            _content = _doc.get("content") or {}
                            if _content.get("bestseller"):
                                flagged.add(_pid)
                            _existing = {
                                f: _content[f]
                                for f in _ANALYTICS_FIELDS
                                if _content.get(f) is not None
                            }
                            if _existing:
                                analytics_by_pid[_pid] = _existing
                    preserved = 0
                    preserved_analytics = 0
                    for p in all_upsert_products:
                        if p.id in flagged:
                            p.bestseller = True
                            preserved += 1
                        _a = analytics_by_pid.get(p.id)
                        if _a:
                            p.unique_sessions = _a.get("unique_sessions")
                            p.units_sold = _a.get("units_sold")
                            p.conversion_rate = _a.get("conversion_rate")
                            preserved_analytics += 1
                    if preserved or preserved_analytics:
                        logger.info(
                            f"⭐ Delta sync: preserved existing bestseller flag on {preserved} "
                            f"product(s), engagement analytics on {preserved_analytics} product(s)"
                        )
                except Exception as exc:
                    logger.warning(
                        f"⚠️ Delta sync: bestseller/analytics preservation failed (left at default): {exc}"
                    )

                total_to_upsert = len(all_upsert_products)
                await _step("search_upsert", "running",
                            f"Upserting {total_to_upsert} docs...")

                if total_to_upsert:
                    from fashion_bot.services.product_ingestion.product_llm_cache import (
                        aset_cached_llm_data_bulk,
                    )

                    successful_upserts = 0
                    failed_upserts: List[Dict[str, Any]] = []
                    cached_total = 0
                    cache_failed_exc: Optional[Exception] = None

                    # Serialize → upsert → cache one bounded batch at a time so
                    # the Search documents (large dicts) for the whole window are
                    # never resident at once; release + gc between batches.
                    for _batch in _chunked(all_upsert_products, batch_size):
                        search_docs = [p.to_search_document(client_id) for p in _batch]
                        if not search_docs:
                            continue
                        upsert_result = await self.search_service.aupsert_documents(
                            search_docs, client_id=client_id
                        )
                        successful_upserts += upsert_result.get("success_count", 0)
                        _batch_failed = upsert_result.get("failed", [])
                        failed_upserts.extend(_batch_failed)
                        try:
                            cached_total += await aset_cached_llm_data_bulk(
                                client_id,
                                search_docs,
                                failed_doc_ids={
                                    f.get("product_id")
                                    for f in _batch_failed
                                    if f.get("product_id")
                                },
                            )
                        except Exception as e:
                            cache_failed_exc = e
                        del search_docs
                        gc.collect()

                    upsert_st = "success" if not failed_upserts else "success_with_warnings"
                    await _step("search_upsert", upsert_st,
                                f"{successful_upserts} upserted, {len(failed_upserts)} failed",
                                upserted=successful_upserts, failed=len(failed_upserts))

                    if cache_failed_exc is not None:
                        logger.warning(f"⚠️ Delta sync: Redis LLM cache write failed: {cache_failed_exc}")
                        await _step("redis_cache", "success_with_warnings",
                                    f"Cache write failed: {cache_failed_exc}")
                    else:
                        await _step("redis_cache", "success",
                                    f"Cached {cached_total} entries", cached=cached_total)

                    result.products_added = min(len(products_to_add), successful_upserts)
                    result.products_updated = successful_upserts - result.products_added

                    for failure in failed_upserts:
                        result.failed_details.append(
                            FailedProduct(
                                product_id=failure.get("product_id", "unknown"),
                                error=failure.get("error", "Unknown error")
                            )
                        )
                    result.failed_count = len(result.failed_details)

                    logger.info(f"✅ Upserted {successful_upserts} products "
                               f"({result.products_added} added, {result.products_updated} updated)")
                else:
                    await _step("search_upsert", "skipped", "No docs to upsert")
                    await _step("redis_cache", "skipped", "No docs to cache")
            else:
                for stage in ("llm_extraction", "attribute_persistence",
                              "bestseller_tagging", "search_upsert", "redis_cache"):
                    await _step(stage, "skipped", "All products unchanged")

            result.products_unchanged = len(products_unchanged)
            result.duration = time.time() - start_time

            self._ingestion_status[client_id] = {
                "last_ingestion": result.timestamp.isoformat(),
                "product_count": result.total_processed,
                "source_used": result.source_used,
                "duration": result.duration,
                "sync_type": "delta"
            }

            logger.info(f"✅ Delta sync complete in {result.duration:.2f}s: "
                       f"Added={result.products_added}, "
                       f"Updated={result.products_updated}, "
                       f"Deleted={result.products_deleted}, "
                       f"Unchanged={result.products_unchanged}, "
                       f"Failed={result.failed_count}")

            status = (
                ProductSyncLogger.STATUS_SUCCESS if result.failed_count == 0
                else ProductSyncLogger.STATUS_PARTIAL_FAILURE if result.products_added + result.products_updated > 0
                else ProductSyncLogger.STATUS_FAILURE
            )

            if _t:
                await _t.aupdate_counters(
                    products_added=result.products_added,
                    products_updated=result.products_updated,
                    products_deleted=result.products_deleted,
                    products_unchanged=result.products_unchanged,
                    products_failed=result.failed_count,
                )
                await _t.acomplete(status, result.duration)
            else:
                await ProductSyncLogger.alog_cron_event(
                    client_id=client_id,
                    sync_type="weekly_delta_sync",
                    status=status,
                    products_added=result.products_added,
                    products_updated=result.products_updated,
                    products_deleted=result.products_deleted,
                    products_unchanged=result.products_unchanged,
                    products_failed=result.failed_count,
                    duration_seconds=result.duration
                )

            return result

        except Exception as e:
            result.duration = time.time() - start_time
            result.failed_count = 1
            result.failed_details.append(
                FailedProduct(product_id="*", error=str(e))
            )
            logger.error(f"❌ Delta sync failed for client {client_id}: {e}")

            if _t:
                await _t.aupdate_counters(
                    products_failed=1, error_message=str(e),
                )
                await _t.acomplete("failed", result.duration)
            else:
                await ProductSyncLogger.alog_cron_event(
                    client_id=client_id,
                    sync_type="weekly_delta_sync",
                    status=ProductSyncLogger.STATUS_FAILURE,
                    products_failed=1,
                    error_message=str(e),
                    duration_seconds=result.duration
                )

            return result

    async def ingest_single_product(
        self,
        client_id: str,
        product_id: str,
        source: str = "auto",
    ) -> Dict[str, Any]:
        """Re-ingest a single product into Upstash Search.

        Fetches the product from Shopify by ID, runs LLM attribute
        extraction, and upserts the resulting search document.

        Args:
            client_id: Client UUID.
            product_id: Shopify numeric product ID.
            source: Source hint (only ``shopify`` is supported for
                single-product fetch).

        Returns:
            Dict with ``success``, ``product_id``, ``message``, and
            optional ``error`` keys.
        """
        start_time = time.time()
        logger.info(f"🚀 Single product ingest: client={client_id} product={product_id}")

        try:
            product_service = await self.source_factory.aget_service(client_id, source)

            from fashion_bot.services.product_ingestion.shopify_product_service import ShopifyProductService
            if not isinstance(product_service, ShopifyProductService):
                return {
                    "success": False,
                    "product_id": product_id,
                    "error": "Single-product re-ingest requires Shopify source",
                }

            product = await product_service.fetch_product_by_id(product_id)
            if product is None:
                return {
                    "success": False,
                    "product_id": product_id,
                    "error": "Product not found or not active in Shopify",
                }

            # Image OCR — runs BEFORE attribute extraction so the OCR'd text
            # feeds the extractor prompt as ``image_text_excerpt`` (matching
            # the order in the full-ingestion path). Always-on regardless of
            # flag state: when the flag is off (or OCR can't run for any
            # reason), the helper preserves whatever OCR data is already in
            # Upstash so a single-product re-ingest never accidentally
            # blanks prior OCR fields.
            try:
                from fashion_bot.services.product_ingestion.product_image_ocr_extractor import (
                    SINGLE_PRODUCT_API_OCR_TIMEOUT_SECONDS,
                    aapply_ocr_to_product,
                )
                # The webhook path uses the helper's default
                # (``WEBHOOK_OCR_TIMEOUT_SECONDS``) because Shopify retries
                # the call if it runs long. The /single endpoint has no
                # such budget — it's a manual REST call and a single
                # combined LLM request over ~20 images can legitimately
                # take 30–60 s, especially when the model is cold-loaded.
                # Use the wider single-product budget so we capture the
                # actual response (success or empty) instead of repeatedly
                # timing out at the doorstep.
                ocr_diag = await aapply_ocr_to_product(
                    client_id=client_id,
                    normalized_product=product,
                    timeout_seconds=SINGLE_PRODUCT_API_OCR_TIMEOUT_SECONDS,
                )
                logger.info(
                    f"🖼️ OCR helper (single): enabled={ocr_diag['enabled']} "
                    f"ran_llm={ocr_diag['ran_llm']} tier1_hit={ocr_diag['tier1_hit']} "
                    f"preserved={ocr_diag['preserved_from_upstash']} "
                    f"images={ocr_diag['images_count']} "
                    f"summary_chars={ocr_diag['summary_chars']} "
                    f"timed_out={ocr_diag.get('timed_out', False)}"
                )
            except Exception as exc:
                logger.warning(
                    f"⚠️ Single-product OCR helper failed (non-fatal): {exc}"
                )

            # PDP size chart extraction — runs independently of the main OCR
            # flag above. Fetches the storefront PDP page, extracts size chart
            # images, OCRs them, and stores the result as an HTML table in
            # metadata.size_chart. Bounded by the single-product OCR timeout.
            try:
                from fashion_bot.services.product_ingestion.image_ocr_config import (
                    ais_pdp_size_chart_ocr_enabled,
                    aget_pdp_size_chart_overrides,
                    resolve_size_chart_selectors,
                )
                if await ais_pdp_size_chart_ocr_enabled(client_id):
                    sc_overrides = await aget_pdp_size_chart_overrides(client_id)
                    sc_selectors = resolve_size_chart_selectors(sc_overrides)
                    sc_storefront = (sc_overrides or {}).get("storefront_domain")
                    sc_base_url: Optional[str] = None
                    if sc_storefront:
                        d = sc_storefront.strip()
                        sc_base_url = d if "://" in d else f"https://{d}"

                    pdp_sc_map = await self._fetch_pdp_size_chart_urls(
                        products=[product],
                        selectors=sc_selectors,
                        storefront_base_url=sc_base_url,
                    )
                    sc_urls = pdp_sc_map.get(product.id)
                    if sc_urls:
                        from fashion_bot.services.product_ingestion.product_image_ocr_extractor import (
                            collect_image_urls_for_ocr,
                            get_product_image_ocr_extractor,
                            size_chart_ocr_to_html_table,
                        )
                        ocr_targets = collect_image_urls_for_ocr(
                            image_url=None, all_images=[], all_metafields=[],
                            pdp_size_chart_urls=sc_urls,
                        )
                        if ocr_targets:
                            extractor = get_product_image_ocr_extractor()
                            sc_summary, sc_per_image, _ = await asyncio.wait_for(
                                extractor.aextract_for_product_combined(
                                    client_id=client_id,
                                    product_id=str(product.id),
                                    image_urls=ocr_targets,
                                ),
                                timeout=SINGLE_PRODUCT_API_OCR_TIMEOUT_SECONDS,
                            )
                            # Merge size chart OCR into existing image_ocr_texts.
                            if sc_per_image:
                                existing = getattr(product, "image_ocr_texts", None) or {}
                                existing.update(sc_per_image)
                                product.image_ocr_texts = existing

                            # Populate size_chart as HTML table.
                            if not getattr(product, "size_chart", None) and sc_per_image:
                                sc_texts = [t for t in sc_per_image.values() if t]
                                html_parts = [size_chart_ocr_to_html_table(t) for t in sc_texts]
                                html_combined = "\n".join(p for p in html_parts if p)
                                if html_combined:
                                    product.size_chart = {
                                        "content": html_combined,
                                        "source": "pdp_image_ocr",
                                    }
                            logger.info(
                                f"🖼️ PDP size chart (single): {len(sc_per_image)} images OCR'd"
                            )
            except asyncio.TimeoutError:
                logger.warning("⏱️ PDP size chart OCR timed out (single product)")
            except Exception as exc:
                logger.warning(f"⚠️ PDP size chart extraction failed (non-fatal): {exc}")

            # Check if manually edited attributes exist in Postgres
            try:
                from fashion_bot.services.product_ingestion.product_attributes_store import (
                    aget_product_attributes,
                )
                manual = await aget_product_attributes(client_id, product.id)
                if manual and manual.get("is_manually_edited"):
                    self._apply_manual_attributes(product, manual)
                    logger.info(f"📝 Applied manually edited attributes for {product_id}")
                else:
                    await self._extract_and_persist_attributes(client_id, [product])
            except Exception as exc:
                logger.warning(f"⚠️ Attribute handling failed, proceeding without: {exc}")

            search_doc = product.to_search_document(client_id)
            upsert_result = await self.search_service.aupsert_documents(
                [search_doc], client_id=client_id,
            )

            # Populate Redis LLM cache
            try:
                from fashion_bot.services.product_ingestion.product_llm_cache import (
                    aset_cached_llm_data_bulk,
                )
                await aset_cached_llm_data_bulk(
                    client_id,
                    [search_doc],
                    failed_doc_ids={
                        f.get("product_id")
                        for f in upsert_result.get("failed", [])
                        if f.get("product_id")
                    },
                )
            except Exception:
                pass

            duration = time.time() - start_time
            success = upsert_result.get("success_count", 0) > 0

            await ProductSyncLogger.alog_manual_api_event(
                client_id=client_id,
                sync_type="single_product_ingest",
                status=ProductSyncLogger.STATUS_SUCCESS if success else ProductSyncLogger.STATUS_FAILURE,
                products_added=1 if success else 0,
                product_id=product_id,
                product_title=product.title,
                duration_seconds=duration,
            )

            return {
                "success": success,
                "product_id": product_id,
                "title": product.title,
                "duration_seconds": round(duration, 2),
                "message": "Product ingested successfully" if success else "Upsert failed",
            }

        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"❌ Single product ingest failed: {e}")
            await ProductSyncLogger.alog_manual_api_event(
                client_id=client_id,
                sync_type="single_product_ingest",
                status=ProductSyncLogger.STATUS_FAILURE,
                products_failed=1,
                product_id=product_id,
                error_message=str(e),
                duration_seconds=duration,
            )
            return {
                "success": False,
                "product_id": product_id,
                "error": str(e),
            }

    # ------------------------------------------------------------------
    # Attribute persistence helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_manual_attributes(product, manual: Dict[str, Any]) -> None:
        """Overwrite NormalizedProduct fields from manually edited Postgres row."""
        mapping = {
            "category": "category",
            "subcategory": "subcategory",
            "base_product_name": "base_product_name",
            "product_line": "product_line",
            "color": "extracted_color",
            "material": "material",
            "segment": "segment",
            "color_family": "color_family",
            "pattern": "pattern",
            "fit": "fit_type",
        }
        for db_col, attr in mapping.items():
            val = manual.get(db_col)
            if val is not None:
                setattr(product, attr, val)

        for list_field in ("occasion", "style", "vibe", "pairing_tags"):
            val = manual.get(list_field)
            if val is not None:
                setattr(product, list_field, val if isinstance(val, list) else [])

        if manual.get("product_line"):
            product.product_line_normalized = manual["product_line"].strip().lower()

    async def _extract_and_persist_attributes(
        self,
        client_id: str,
        products: List,
    ) -> None:
        """Run LLM extraction and save results to Postgres."""
        from fashion_bot.services.product_ingestion.product_attribute_extractor import (
            get_product_attribute_extractor,
        )
        extractor = get_product_attribute_extractor()

        product_dicts = [_normalized_product_to_extractor_dict(p) for p in products]

        extracted_attrs = await extractor.extract_batch_async(
            product_dicts, max_concurrent=10, client_id=client_id,
        )
        self._apply_extracted_attributes(products, extracted_attrs)

        try:
            from fashion_bot.services.product_ingestion.product_attributes_store import (
                aupsert_product_attributes,
            )
            for product, attrs in zip(products, extracted_attrs):
                if not attrs.extraction_success:
                    continue
                await aupsert_product_attributes(
                    client_id=client_id,
                    product_id=product.id,
                    attrs={
                        "category": attrs.category,
                        "subcategory": attrs.subcategory,
                        "base_product_name": attrs.base_product_name,
                        "product_line": attrs.product_line,
                        "color": attrs.color,
                        "material": attrs.material,
                        "occasion": attrs.occasion or [],
                        "style": attrs.style or [],
                        "vibe": attrs.vibe or [],
                        "pairing_tags": attrs.pairing_tags or [],
                        "segment": attrs.segment,
                        "color_family": attrs.color_family,
                        "pattern": attrs.pattern,
                        "fit": attrs.fit,
                    },
                    product_title=product.title,
                    is_manually_edited=False,
                )
        except Exception as exc:
            logger.warning(f"⚠️ Failed to persist extracted attributes to Postgres: {exc}")

    def delta_sync_to_response(self, result: DeltaSyncResult, client_id: str) -> DeltaSyncResponse:
        """
        Convert DeltaSyncResult to API response.
        
        Args:
            result: The DeltaSyncResult
            client_id: The client ID
            
        Returns:
            DeltaSyncResponse for API
        """
        return DeltaSyncResponse(
            success=result.success,
            client_id=client_id,
            source_used=result.source_used,
            products_added=result.products_added,
            products_updated=result.products_updated,
            products_deleted=result.products_deleted,
            products_unchanged=result.products_unchanged,
            failed_count=result.failed_count,
            duration_seconds=round(result.duration, 2),
            timestamp=result.timestamp.isoformat(),
            failed_products=[
                {"product_id": f.product_id, "error": f.error}
                for f in result.failed_details
            ]
        )
