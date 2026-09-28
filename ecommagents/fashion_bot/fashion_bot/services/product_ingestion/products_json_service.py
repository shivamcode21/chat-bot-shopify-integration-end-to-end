"""
Products JSON Service - Fetches products from public /products.json endpoint.

This is a fallback service for Shopify storefronts without API credentials.
Works with the public /products.json endpoint available on all Shopify stores.
"""

import asyncio
import logging
import re
from typing import List, Optional
import httpx

from fashion_bot.services.product_ingestion.base_product_service import BaseProductService
from fashion_bot.services.product_ingestion.models import NormalizedProduct
from fashion_bot.services.product_ingestion.storefront_http import (
    PER_PAGE_DELAY,
    afetch_storefront_json,
)

logger = logging.getLogger(__name__)

# Shopify caps the public endpoint at 250 products per page. A page shorter
# than this is therefore the last one.
_PAGE_SIZE = 250


class ProductsJsonService(BaseProductService):
    """
    Fetches products from public /products.json endpoint.
    
    Works with Shopify storefronts without API credentials.
    Limited to 250 products per page.
    """
    
    def __init__(self, website_url: str):
        """
        Initialize Products JSON Service.
        
        Args:
            website_url: Base URL of the website (e.g., https://groovee.in)
        """
        self.base_url = website_url.rstrip("/")
    
    @property
    def source_name(self) -> str:
        return "json"
    
    async def fetch_active_products(self, max_products: int = 0) -> List[NormalizedProduct]:
        """
        Fetches products from public /products.json endpoint.

        Uses pagination to fetch all products (250 per page max). When
        *max_products* > 0, in-stock products are prioritised and the total
        is capped at that number.

        Pagination early-stop
        ---------------------
        When *max_products* > 0, pagination stops once we have fetched at
        least ``max_products * 2`` items rather than draining the entire
        catalog. This bounds the worst case for large catalogs from 25,000
        (the 100-page safety net) to roughly ``max_products * 2``. For the
        common onboarding case (``max_products=5000``) that's a hard
        ceiling of 10,000 fetched instead of up to 25,000 — a meaningful
        bandwidth/time saving on the new-client critical path.

        Why ``* 2`` and not ``* 1``?
        The final ``_cap_products`` call sorts by ``(not in_stock,
        -total_inventory)`` so the kept slice favours in-stock,
        high-inventory items even when they show up on later pages.
        Stopping at exactly ``max_products`` would skew toward whatever
        page order the storefront chose to return (often "newest first"),
        which can systematically exclude in-stock evergreens.

        ``* 2`` is a heuristic — it gives the sort enough headroom to do
        meaningful in-stock prioritisation without paying the full
        "drain everything then cap" cost. For catalogs ≤ ``max_products * 2``
        this is a no-op (we drain them anyway), so the behaviour change
        only affects catalogs notably larger than the cap.

        Args:
            max_products: Upper limit on returned products (0 = unlimited).
                          In-stock items are returned first.
        """
        # Bound how many items we are willing to fetch before short-circuiting
        # the pagination loop. ``0`` means "no early-stop" — preserves the
        # legacy "drain everything" behaviour when no cap is requested.
        early_stop_at = max_products * 2 if max_products > 0 else 0

        products: List[NormalizedProduct] = []
        page = 1

        while True:
            # Pagination early-stop. See docstring for the ``* 2``
            # rationale. The final cap still runs via _cap_products
            # below, so this is purely a fetch-side optimisation —
            # the returned slice size is unchanged for any catalog
            # whose product count fits in ``early_stop_at``.
            if early_stop_at and len(products) >= early_stop_at:
                logger.info(
                    f"📦 Early-stopping pagination at {len(products)} products "
                    f"(>= max_products*2 = {early_stop_at}); cap of "
                    f"{max_products} will be applied after sort."
                )
                break

            try:
                url = f"{self.base_url}/products.json"
                params = {"page": page, "limit": _PAGE_SIZE}

                logger.info(f"📦 Fetching products from {url} (page {page})")

                # Retries 429/503 with backoff — storefronts behind Cloudflare
                # rate-limit this endpoint per IP and onboarding fetches it
                # more than once per run.
                data = await afetch_storefront_json(url, params, timeout=60.0)
                raw_products = data.get("products", [])

                if not raw_products:
                    logger.info(f"📭 No more products on page {page}")
                    break

                logger.info(f"📦 Fetched {len(raw_products)} products from page {page}")

                for raw in raw_products:
                    try:
                        normalized = self._normalize_product(raw)
                        products.append(normalized)
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to normalize product {raw.get('id')}: {e}")

                # A short page is the last page. Without this the loop always
                # spent one extra request confirming the catalog had ended,
                # which on a rate-limited storefront could fail and take the
                # whole run down with it — products already in hand included.
                if len(raw_products) < _PAGE_SIZE:
                    logger.info(
                        f"📭 Page {page} returned {len(raw_products)} < {_PAGE_SIZE}; "
                        f"end of catalog"
                    )
                    break

                page += 1

                if page > 100:
                    logger.warning("⚠️ Reached page limit (100), stopping pagination")
                    break

                await asyncio.sleep(PER_PAGE_DELAY)

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    logger.error(f"❌ /products.json not available at {self.base_url}")
                else:
                    logger.error(f"❌ HTTP error: {e.response.status_code}")
                # Only fatal if we have nothing to show for it. Discarding a
                # successfully fetched catalog because a *later* page failed
                # turns a partial outage into a total one — and with
                # force_refresh that used to mean an emptied index.
                if products:
                    logger.warning(
                        f"⚠️ Page {page} failed after {len(products)} products were "
                        f"fetched; continuing with the partial catalog"
                    )
                    break
                raise
            except Exception as e:
                logger.error(f"❌ Error fetching products: {e}")
                if products:
                    logger.warning(
                        f"⚠️ Page {page} failed after {len(products)} products were "
                        f"fetched; continuing with the partial catalog"
                    )
                    break
                raise

        logger.info(f"✅ Total products fetched from JSON: {len(products)}")

        return self._cap_products(products, max_products)

    # ------------------------------------------------------------------
    # Public helper: normalise pre-fetched raw dicts without a network call
    # ------------------------------------------------------------------

    def normalize_raw_products(
        self,
        raw_list: List[dict],
        max_products: int = 0,
    ) -> List[NormalizedProduct]:
        """Normalise already-fetched raw Shopify product dicts.

        Useful when the caller has product JSON from a prior fetch and wants
        to skip the paginated ``/products.json`` round-trip.
        """
        products: List[NormalizedProduct] = []
        for raw in raw_list:
            try:
                products.append(self._normalize_product(raw))
            except Exception as e:
                logger.warning(
                    f"⚠️ Failed to normalize product {raw.get('id')}: {e}"
                )
        logger.info(
            f"✅ Normalized {len(products)} products from pre-fetched data"
        )
        return self._cap_products(products, max_products)

    # ------------------------------------------------------------------

    @staticmethod
    def _cap_products(
        products: List[NormalizedProduct],
        max_products: int,
    ) -> List[NormalizedProduct]:
        if max_products > 0 and len(products) > max_products:
            products.sort(key=lambda p: (not p.in_stock, -(p.total_inventory or 0)))
            logger.info(
                f"✂️ Capping from {len(products)} → {max_products} products "
                f"(in-stock first)"
            )
            products = products[:max_products]
        return products
    
    def _normalize_product(self, raw: dict) -> NormalizedProduct:
        """
        Normalize raw product from /products.json to NormalizedProduct.
        
        The /products.json format is slightly different from Admin API.
        
        Args:
            raw: Raw product data from /products.json
            
        Returns:
            NormalizedProduct instance
        """
        # Extract variants data
        variants = raw.get("variants", [])
        
        # Calculate price range from variants
        prices = []
        compare_at_prices = []
        total_inventory = 0
        variant_data = []
        
        for v in variants:
            price = v.get("price")
            if price:
                try:
                    prices.append(float(price))
                except (ValueError, TypeError):
                    pass
            
            compare_price = v.get("compare_at_price")
            if compare_price:
                try:
                    compare_at_prices.append(float(compare_price))
                except (ValueError, TypeError):
                    pass
            
            # Track inventory (if available in public endpoint)
            inv = v.get("inventory_quantity", 0)
            if inv and isinstance(inv, int):
                total_inventory += inv
            
            # Build variant data
            variant_data.append({
                "id": str(v.get("id", "")),
                "title": v.get("title", ""),
                "price": v.get("price", "0"),
                "compare_at_price": v.get("compare_at_price"),
                "available": v.get("available", False),
                "sku": v.get("sku", ""),
                "option1": v.get("option1"),
                "option2": v.get("option2"),
                "option3": v.get("option3"),
            })
        
        price_min = min(prices) if prices else 0.0
        price_max = max(prices) if prices else 0.0
        compare_at_price_min = min(compare_at_prices) if compare_at_prices else None
        compare_at_price_max = max(compare_at_prices) if compare_at_prices else None

        discount_pct = 0.0
        if compare_at_price_min and compare_at_price_min > 0 and price_min < compare_at_price_min:
            discount_pct = round((compare_at_price_min - price_min) / compare_at_price_min * 100, 1)
        
        # Build option-index-to-type map from product-level options
        options = raw.get("options", [])
        option_type_map = {}  # {1: "size", 2: "color", ...}
        for idx, option in enumerate(options):
            option_name = option.get("name", "").lower()
            pos = idx + 1
            if "size" in option_name:
                option_type_map[pos] = "size"
            elif "color" in option_name or "colour" in option_name:
                option_type_map[pos] = "color"

        # Extract sizes and colors from variant options
        sizes = set()
        colors = set()
        available_sizes = set()
        available_colors = set()
        in_stock = False
        
        for variant in variants:
            variant_available = variant.get("available", False)
            if variant_available:
                in_stock = True
            
            for i in range(1, 4):
                option_value = variant.get(f"option{i}")
                if not option_value:
                    continue
                option_value_lower = option_value.lower()
                opt_type = option_type_map.get(i)
                
                is_size = (
                    opt_type == "size"
                    or (opt_type is None and option_value_lower in [
                        'xs', 's', 'm', 'l', 'xl', 'xxl', '2xl', '3xl',
                        'one size', 'free size',
                    ])
                )
                is_color = (
                    opt_type == "color"
                    or (opt_type is None and any(
                        c in option_value_lower for c in [
                            'black', 'white', 'red', 'blue', 'green', 'yellow',
                            'pink', 'grey', 'gray', 'navy', 'brown', 'beige',
                            'cream', 'purple', 'orange', 'maroon',
                        ]
                    ))
                )
                
                if is_size:
                    sizes.add(option_value)
                    if variant_available:
                        available_sizes.add(option_value)
                elif is_color:
                    colors.add(option_value)
                    if variant_available:
                        available_colors.add(option_value)
        
        # Also add all values from product-level options for full coverage
        for option in options:
            option_name = option.get("name", "").lower()
            values = option.get("values", [])
            if "size" in option_name:
                sizes.update(values)
            elif "color" in option_name or "colour" in option_name:
                colors.update(values)
        
        # Get all images
        images = raw.get("images", [])
        all_images = []
        image_url = ""
        
        for img in images:
            if isinstance(img, dict):
                src = img.get("src", "")
            else:
                src = str(img)
            if src:
                all_images.append(src)
        
        if all_images:
            image_url = all_images[0]
        
        # Build product URL
        handle = raw.get("handle", "")
        product_url = f"{self.base_url}/products/{handle}"
        
        # Clean description (strip HTML)
        description = raw.get("body_html", "") or ""
        description = self._strip_html(description)
        
        # Parse tags
        tags = raw.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        
        variant_availability_pct = (
            round(sum(1 for v in variant_data if v.get("available")) / len(variant_data) * 100, 1)
            if variant_data else 100.0
        )

        return NormalizedProduct(
            id=str(raw.get("id")),
            title=raw.get("title", ""),
            handle=handle,
            product_type=raw.get("product_type", ""),
            vendor=raw.get("vendor", ""),
            tags=tags if isinstance(tags, list) else [],
            colors=list(colors),
            sizes=list(sizes),
            price_min=price_min,
            price_max=price_max,
            compare_at_price_min=compare_at_price_min,
            compare_at_price_max=compare_at_price_max,
            discount_pct=discount_pct,
            image_url=image_url,
            all_images=all_images,
            product_url=product_url,
            in_stock=in_stock,
            available_sizes=list(available_sizes),
            available_colors=list(available_colors),
            variant_availability_pct=variant_availability_pct,
            description=description[:500],  # Truncate to first 500 chars
            total_inventory=total_inventory,
            status=raw.get("status", "active"),
            variants=variant_data,
            # Note: Metafields are not available in public /products.json
            # These will remain None/empty
            fabric=None,
            care_instructions=None,
            fit_type=None,
            size_chart=None,
            seo_title=None,
            seo_description=None,
            created_at=raw.get("created_at"),
            updated_at=raw.get("updated_at"),
            published_at=raw.get("published_at"),
        )
    
    def _strip_html(self, text: str) -> str:
        """Strip HTML tags from text."""
        if not text:
            return ""
        
        # Simple regex to strip HTML tags
        clean = re.sub(r'<[^>]+>', ' ', text)
        # Normalize whitespace
        clean = re.sub(r'\s+', ' ', clean)
        return clean.strip()
