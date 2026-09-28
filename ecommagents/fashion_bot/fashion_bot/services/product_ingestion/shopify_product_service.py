"""
Shopify Product Service - Fetches products from Shopify Admin API using GraphQL.
"""

import logging
import os
import re
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any
import httpx

from fashion_bot.services.product_ingestion.base_product_service import BaseProductService
from fashion_bot.services.product_ingestion.models import ShopifyConfig, NormalizedProduct
from fashion_bot.utils.http_client import get_shared_async_http_client
from fashion_bot.utils.shopify_throttle import (
    is_throttled as _shopify_is_throttled,
    retry_after_seconds as _shopify_retry_after_seconds,
    throttle_backoff_seconds as _shopify_throttle_backoff_seconds,
    shopify_graphql_post,
    retry_shopify,
    raise_for_shopify_status,
    ShopifyRetryableError,
    get_shopify_throttle,
)
from fashion_bot.utils.shopify_rate_limiter import get_shopify_rate_limiter

logger = logging.getLogger(__name__)


def variant_is_in_stock(inventory_quantity: Any, inventory_policy: Any) -> bool:
    """Single source of truth for whether a variant is purchasable.

    A variant is in stock when it has positive inventory OR its policy allows
    overselling ("continue"). The policy value is case-normalized because the
    Admin GraphQL API returns the enum uppercase ("CONTINUE") while REST /
    webhook payloads return it lowercase ("continue"). Comparing without
    normalizing silently marked oversell / made-to-order variants as out of
    stock on the webhook path.
    """
    try:
        qty = int(inventory_quantity or 0)
    except (TypeError, ValueError):
        qty = 0
    policy = str(inventory_policy or "").upper()
    return qty > 0 or policy == "CONTINUE"


@dataclass
class SalesVolumeResult:
    """Result of a Shopify orders sales-volume sweep.

    ``complete`` is True only when pagination ended naturally (every order in
    the window was counted). It is False when the sweep was cut short by the
    ``max_pages`` cap, sustained throttling, or a GraphQL error — in which case
    ``sales`` holds only a partial count and callers must NOT use it to demote
    bestsellers. ``truncated_reason`` is one of ``max_pages`` | ``throttled`` |
    ``transient_error`` | ``graphql_error`` | ``None``.
    """
    sales: Dict[str, int]
    complete: bool
    pages_fetched: int
    truncated_reason: Optional[str] = None


# ShopifyQL ranks the entire window server-side in a single call; this caps how
# many top products it returns. Kept far above any bestseller top-N so the
# ranked subset the cron slices is exact.
SHOPIFYQL_SALES_LIMIT = max(1, int(os.getenv("SHOPIFYQL_SALES_LIMIT", "5000")))

# Per-product session/engagement fetch (ShopifyQL `sessions` dataset). Bounds how
# many products (ranked by sessions DESC) the engagement query returns — the set
# whose analytics fields the monthly cron writes. Same order of magnitude as the
# sales limit so the traffic window is well covered without unbounded reads.
SHOPIFYQL_ENGAGEMENT_LIMIT = max(1, int(os.getenv("SHOPIFYQL_ENGAGEMENT_LIMIT", "5000")))
# GROUP BY dimension for the sessions dataset. Defaults to product_id so the
# result joins directly to Upstash docs (keyed on product_id). Shopify's docs
# demonstrate the sessions funnel with product_title; if a store's ShopifyQL
# rejects product_id here, set SHOPIFYQL_ENGAGEMENT_GROUP_BY=product_title
# (the cron then maps titles → ids) without a code change.
SHOPIFYQL_ENGAGEMENT_GROUP_BY = os.getenv("SHOPIFYQL_ENGAGEMENT_GROUP_BY", "product_id").strip() or "product_id"


@dataclass
class EngagementResult:
    """Result of a Shopify per-product engagement (sessions) fetch.

    ``sessions`` maps a product key → unique storefront sessions in the window.
    The key is a product GID when grouped by ``product_id`` (normalised to GID
    form for drop-in join with :class:`SalesVolumeResult`), or the raw product
    title when grouped by ``product_title``. ``grouped_by`` records which so the
    caller knows how to join. ``complete`` mirrors :class:`SalesVolumeResult`:
    ShopifyQL ranks the whole window in one call, so a successful fetch is always
    complete; a failure raises rather than returning a partial map.
    """
    sessions: Dict[str, int]
    grouped_by: str
    complete: bool = True


# GraphQL query for fetching products with all details including metafields
PRODUCTS_GRAPHQL_QUERY = """
query getProducts($cursor: String) {
    products(first: 50, after: $cursor, query: "status:active published_status:published") {
        pageInfo {
            hasNextPage
            endCursor
        }
        edges {
            node {
                id
                title
                description
                descriptionHtml
                handle
                vendor
                productType
                tags
                createdAt
                updatedAt
                publishedAt
                status
                totalInventory
                seo {
                    title
                    description
                }
                metafields(first: 50) {
                    edges {
                        node {
                            namespace
                            key
                            value
                            type
                            reference {
                                ... on Metaobject {
                                    displayName
                                    handle
                                    type
                                }
                                ... on TaxonomyValue {
                                    name
                                }
                            }
                            references(first: 20) {
                                edges {
                                    node {
                                        ... on Metaobject {
                                            displayName
                                            handle
                                            type
                                        }
                                        ... on TaxonomyValue {
                                            name
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                options {
                    name
                    values
                }
                variants(first: 100) {
                    edges {
                        node {
                            id
                            title
                            sku
                            price
                            compareAtPrice
                            inventoryQuantity
                            inventoryPolicy
                            selectedOptions {
                                name
                                value
                            }
                        }
                    }
                }
                collections(first: 65) {
                    edges {
                        node {
                            title
                            handle
                        }
                    }
                }
                images(first: 10) {
                    edges {
                        node {
                            url
                            altText
                        }
                    }
                }
            }
        }
    }
}
"""

# GraphQL query for fetching products updated after a specific time.
# Must mirror PRODUCTS_GRAPHQL_QUERY so that delta-synced documents have
# identical fields to full-sync documents (collections, resolved metafields).
PRODUCTS_UPDATED_SINCE_GRAPHQL_QUERY = """
query getProductsUpdatedSince($cursor: String, $updatedSince: String!) {
    products(first: 50, after: $cursor, query: "status:active published_status:published updated_at:>$updatedSince") {
        pageInfo {
            hasNextPage
            endCursor
        }
        edges {
            node {
                id
                title
                description
                descriptionHtml
                handle
                vendor
                productType
                tags
                createdAt
                updatedAt
                publishedAt
                status
                totalInventory
                seo {
                    title
                    description
                }
                metafields(first: 50) {
                    edges {
                        node {
                            namespace
                            key
                            value
                            type
                            reference {
                                ... on Metaobject {
                                    displayName
                                    handle
                                    type
                                }
                                ... on TaxonomyValue {
                                    name
                                }
                            }
                            references(first: 20) {
                                edges {
                                    node {
                                        ... on Metaobject {
                                            displayName
                                            handle
                                            type
                                        }
                                        ... on TaxonomyValue {
                                            name
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                options {
                    name
                    values
                }
                variants(first: 100) {
                    edges {
                        node {
                            id
                            title
                            sku
                            price
                            compareAtPrice
                            inventoryQuantity
                            inventoryPolicy
                            selectedOptions {
                                name
                                value
                            }
                        }
                    }
                }
                collections(first: 65) {
                    edges {
                        node {
                            title
                            handle
                        }
                    }
                }
                images(first: 10) {
                    edges {
                        node {
                            url
                            altText
                        }
                    }
                }
            }
        }
    }
}
"""


class ShopifyProductService(BaseProductService):
    """
    Fetches products from Shopify Admin API using GraphQL.
    
    Uses GraphQL for comprehensive data including metafields.
    """
    
    def __init__(self, config: ShopifyConfig, website_base_url: Optional[str] = None):
        """
        Initialize Shopify Product Service.
        
        Args:
            config: ShopifyConfig with shop_domain and access_token
            website_base_url: Optional base URL for product links (e.g., https://groovee.in)
        """
        self.shop_domain = config.shop_domain.replace("https://", "").replace("http://", "").rstrip("/")
        self.access_token = config.access_token
        self.api_version = config.api_version
        self.website_base_url = website_base_url.rstrip("/") if website_base_url else None
        
        # Build base URL for Shopify Admin API
        if ".myshopify.com" not in self.shop_domain:
            self.shop_domain = f"{self.shop_domain}.myshopify.com"
        
        self.graphql_url = f"https://{self.shop_domain}/admin/api/{self.api_version}/graphql.json"

        # Key for the shared, cross-pod Shopify rate limiter. Shopify rate
        # limits are per shop, so every call this service makes paces against
        # one budget keyed by the shop domain (shared across all services/pods).
        self._rate_limit_key = self.shop_domain

    @property
    def source_name(self) -> str:
        return "shopify_graphql"
    
    async def fetch_active_products(self, max_products: int = 0) -> List[NormalizedProduct]:
        """
        Fetches all active products with variants and metafields using GraphQL.
        Uses cursor-based pagination for large catalogs.

        Args:
            max_products: Cap on the total number of products fetched
                          (0 = unlimited). Useful for a small canary sync
                          (e.g. 100) to validate documents before a full
                          re-ingest. Products are fetched in the Admin API's
                          default order; the cap is on count, not selection.
        """
        products = []
        cursor = None
        has_next_page = True

        headers = {
            "X-Shopify-Access-Token": self.access_token,
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            while has_next_page:
                try:
                    payload = {
                        "query": PRODUCTS_GRAPHQL_QUERY,
                        "variables": {"cursor": cursor}
                    }

                    # Proactive per-shop pacing + bounded retry/backoff on
                    # 429 / 5xx / network / THROTTLED; non-retryable GraphQL +
                    # 4xx errors propagate (caught below).
                    data = await shopify_graphql_post(
                        client, self.graphql_url, headers, payload,
                        rate_limit_key=self._rate_limit_key,
                    )

                    products_data = data.get("data", {}).get("products", {})
                    page_info = products_data.get("pageInfo", {})
                    edges = products_data.get("edges", [])

                    logger.info(f"📦 Fetched {len(edges)} products from Shopify GraphQL")

                    for edge in edges:
                        if max_products > 0 and len(products) >= max_products:
                            break
                        try:
                            raw = edge.get("node", {})
                            normalized = await self._normalize_product(raw)
                            products.append(normalized)
                        except Exception as e:
                            product_id = edge.get("node", {}).get("id", "unknown")
                            logger.warning(f"⚠️ Failed to normalize product {product_id}: {e}")

                    # Stop once the cap is reached, otherwise follow pagination.
                    if max_products > 0 and len(products) >= max_products:
                        logger.info(
                            f"🧪 Reached max_products cap ({max_products}); "
                            f"stopping pagination"
                        )
                        has_next_page = False
                    else:
                        has_next_page = page_info.get("hasNextPage", False)
                        cursor = page_info.get("endCursor")

                except httpx.HTTPStatusError as e:
                    logger.error(f"❌ Shopify API error: {e.response.status_code} - {e.response.text}")
                    raise
                except Exception as e:
                    logger.error(f"❌ Error fetching products from Shopify: {e}")
                    raise

        logger.info(f"✅ Total products fetched from Shopify GraphQL: {len(products)}")

        return products
    
    async def fetch_recently_updated_products(
        self, 
        updated_since: Optional[datetime] = None,
        hours: int = 24
    ) -> List[NormalizedProduct]:
        """
        Fetch products updated since a given time using GraphQL.
        
        Uses Shopify's updated_at filter to only fetch recently modified products.
        This is more efficient than fetching all products for delta sync.
        
        Args:
            updated_since: Fetch products updated after this datetime (UTC).
                          If None, uses current time minus `hours`.
            hours: Number of hours to look back (default: 24).
                   Only used if updated_since is None.
        
        Returns:
            List of NormalizedProduct objects updated in the time window
        """
        # Calculate the updated_since timestamp
        if updated_since is None:
            updated_since = datetime.now(timezone.utc) - timedelta(hours=hours)
        
        # Format as ISO 8601 for Shopify (YYYY-MM-DDTHH:MM:SSZ)
        updated_since_str = updated_since.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        logger.info(f"🔍 Fetching products updated since: {updated_since_str}")
        
        products = []
        cursor = None
        has_next_page = True
        
        headers = {
            "X-Shopify-Access-Token": self.access_token,
            "Content-Type": "application/json"
        }
        
        # Build the query with updated_at filter
        # Shopify GraphQL query syntax: "status:active published_status:published updated_at:>2024-01-01T00:00:00Z"
        query_filter = f"status:active published_status:published updated_at:>{updated_since_str}"
        
        # Use a modified query with the filter embedded
        graphql_query = f"""
        query getProductsUpdatedSince($cursor: String) {{
            products(first: 50, after: $cursor, query: "{query_filter}") {{
                pageInfo {{
                    hasNextPage
                    endCursor
                }}
                edges {{
                    node {{
                        id
                        title
                        description
                        descriptionHtml
                        handle
                        vendor
                        productType
                        tags
                        createdAt
                        updatedAt
                        publishedAt
                        status
                        totalInventory
                        seo {{
                            title
                            description
                        }}
                        metafields(first: 50) {{
                            edges {{
                                node {{
                                    namespace
                                    key
                                    value
                                    type
                                    reference {{
                                        ... on Metaobject {{
                                            displayName
                                            handle
                                            type
                                        }}
                                        ... on TaxonomyValue {{
                                            name
                                        }}
                                    }}
                                    references(first: 20) {{
                                        edges {{
                                            node {{
                                                ... on Metaobject {{
                                                    displayName
                                                    handle
                                                    type
                                                }}
                                                ... on TaxonomyValue {{
                                                    name
                                                }}
                                            }}
                                        }}
                                    }}
                                }}
                            }}
                        }}
                        options {{
                            name
                            values
                        }}
                        variants(first: 100) {{
                            edges {{
                                node {{
                                    id
                                    title
                                    sku
                                    price
                                    compareAtPrice
                                    inventoryQuantity
                                    inventoryPolicy
                                    selectedOptions {{
                                        name
                                        value
                                    }}
                                }}
                            }}
                        }}
                        collections(first: 65) {{
                            edges {{
                                node {{
                                    title
                                    handle
                                }}
                            }}
                        }}
                        images(first: 10) {{
                            edges {{
                                node {{
                                    url
                                    altText
                                }}
                            }}
                        }}
                    }}
                }}
            }}
        }}
        """
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            while has_next_page:
                try:
                    payload = {
                        "query": graphql_query,
                        "variables": {"cursor": cursor}
                    }

                    # Proactive per-shop pacing + bounded retry/backoff on
                    # 429 / 5xx / network / THROTTLED; non-retryable GraphQL +
                    # 4xx errors propagate (caught below).
                    data = await shopify_graphql_post(
                        client, self.graphql_url, headers, payload,
                        rate_limit_key=self._rate_limit_key,
                    )

                    products_data = data.get("data", {}).get("products", {})
                    page_info = products_data.get("pageInfo", {})
                    edges = products_data.get("edges", [])

                    logger.info(f"📦 Fetched {len(edges)} recently updated products from Shopify")
                    
                    for edge in edges:
                        try:
                            raw = edge.get("node", {})
                            normalized = await self._normalize_product(raw)
                            products.append(normalized)
                        except Exception as e:
                            product_id = edge.get("node", {}).get("id", "unknown")
                            logger.warning(f"⚠️ Failed to normalize product {product_id}: {e}")
                    
                    # Handle pagination
                    has_next_page = page_info.get("hasNextPage", False)
                    cursor = page_info.get("endCursor")
                    
                except httpx.HTTPStatusError as e:
                    logger.error(f"❌ Shopify API error: {e.response.status_code} - {e.response.text}")
                    raise
                except Exception as e:
                    logger.error(f"❌ Error fetching recently updated products from Shopify: {e}")
                    raise
        
        logger.info(f"✅ Total recently updated products fetched: {len(products)} (since {updated_since_str})")
        return products

    @retry_shopify
    async def fetch_product_by_id(self, product_id: str) -> Optional[NormalizedProduct]:
        """Fetch and normalise a single product by its Shopify numeric ID.

        Decorated with :func:`retry_shopify`, so the body below is just the
        happy path — the shared throttle transparently retries the whole call
        with capped backoff on HTTP 429 / 5xx / network errors / GraphQL
        ``THROTTLED`` (proactive per-shop pacing lives inside the body so it
        re-paces on every attempt), while deterministic 4xx / not-found / other
        GraphQL errors resolve on the first attempt.

        Args:
            product_id: Numeric Shopify product ID (e.g. ``"7654321"``).

        Returns:
            NormalizedProduct or None if not found / not active.
        """
        gid = (
            product_id
            if product_id.startswith("gid://")
            else f"gid://shopify/Product/{product_id}"
        )

        graphql_query = """
        query getProduct($id: ID!) {
            product(id: $id) {
                id
                title
                description
                descriptionHtml
                handle
                vendor
                productType
                tags
                createdAt
                updatedAt
                publishedAt
                status
                totalInventory
                seo { title description }
                metafields(first: 50) {
                    edges {
                        node {
                            namespace key value type
                            reference { ... on Metaobject { displayName handle type } ... on TaxonomyValue { name } }
                            references(first: 20) { edges { node { ... on Metaobject { displayName handle type } ... on TaxonomyValue { name } } } }
                        }
                    }
                }
                options { name values }
                variants(first: 100) {
                    edges {
                        node {
                            id title sku price compareAtPrice
                            inventoryQuantity inventoryPolicy
                            selectedOptions { name value }
                        }
                    }
                }
                collections(first: 65) { edges { node { title handle } } }
                images(first: 10) { edges { node { url altText } } }
            }
        }
        """

        headers = {
            "X-Shopify-Access-Token": self.access_token,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Proactive per-shop pacing before the single-product fetch; inside
            # the retried body so each attempt re-paces.
            await get_shopify_rate_limiter().acquire(self._rate_limit_key)
            resp = await client.post(
                self.graphql_url,
                headers=headers,
                json={"query": graphql_query, "variables": {"id": gid}},
            )
            # 429 / 5xx → retryable; non-429 4xx → propagate immediately.
            raise_for_shopify_status(resp)
            data = resp.json()

        if "errors" in data:
            # Throttle errors are retryable; any other GraphQL error is terminal
            # for a single-product read (treated as "not found" → None).
            if _shopify_is_throttled(data["errors"]):
                raise ShopifyRetryableError(f"Shopify GraphQL THROTTLED: {data['errors']}")
            logger.error(f"❌ GraphQL errors fetching product {product_id}: {data['errors']}")
            return None

        raw = (data.get("data") or {}).get("product")
        if not raw:
            logger.warning(f"⚠️ Product {product_id} not found in Shopify")
            return None

        if raw.get("status", "").lower() != "active":
            logger.info(f"ℹ️ Product {product_id} is not active (status={raw.get('status')})")
            return None

        if not raw.get("publishedAt"):
            logger.info(f"ℹ️ Product {product_id} is active but unpublished (publishedAt is null)")
            return None

        return await self._normalize_product(raw)

    async def fetch_top_selling_product_ids(
        self,
        top_n_per_subcategory: int = 3,
        days: int = 10,
        max_pages: int = 20,
    ) -> Optional[Dict[str, int]]:
        """Fetch product sales volume from Shopify Orders API (last *days* days).

        Backward-compatible wrapper around :meth:`afetch_sales_volume` that
        returns only the GID → quantity-sold map (or ``None`` when empty).
        Callers that must know whether the sweep was *complete* (e.g. the
        monthly bestseller refresh, which must never demote on partial data)
        should call :meth:`afetch_sales_volume` directly.

        *max_pages* caps how many 50-order pages are paginated (default 20 ≈
        1k orders, preserving prior behaviour).
        """
        result = await self.afetch_sales_volume(days=days, max_pages=max_pages)
        return result.sales or None

    # Thin shims over the shared throttle helpers in utils.shopify_throttle so
    # the orders sweep below keeps its bespoke completeness/truncation loop
    # while the retry math lives in one place (AGENTS.md: shared utilities).
    @staticmethod
    def _is_throttled(errors: Any) -> bool:
        """True when a Shopify GraphQL ``errors`` array signals cost throttling."""
        return _shopify_is_throttled(errors)

    @staticmethod
    def _retry_after_seconds(resp: httpx.Response) -> Optional[float]:
        """Parse a Retry-After header (seconds), capped at 30s."""
        return _shopify_retry_after_seconds(resp)

    @staticmethod
    def _throttle_backoff_seconds(data: Optional[Dict[str, Any]], attempt: int) -> float:
        """Backoff for a throttled page: honour Shopify's cost ``throttleStatus``
        when present (wait until enough cost points restore), else exponential."""
        return _shopify_throttle_backoff_seconds(data, attempt)

    async def afetch_sales_volume(
        self,
        days: int = 10,
        max_pages: int = 20,
        limit: Optional[int] = None,
    ) -> SalesVolumeResult:
        """Rank product sales volume sold in the last *days* days.

        Default path queries Shopify's pre-aggregated analytics via ShopifyQL
        (``shopifyqlQuery`` — one request, server-side ranked, returns-adjusted
        ``net_items_sold``). This avoids paginating thousands of orders and
        removes the old ~10k-order (``max_pages``) ceiling that silently
        undercounted high-volume stores. Requires the ``read_reports`` scope.

        Falls back to the legacy Orders-API sweep
        (:meth:`_afetch_sales_volume_via_orders`) when ShopifyQL is disabled via
        ``BESTSELLER_USE_SHOPIFYQL=false`` or when the ShopifyQL call errors
        (e.g. the token lacks ``read_reports``), so a missing scope degrades
        gracefully instead of failing the refresh.

        Both paths return a :class:`SalesVolumeResult` keyed by product GID, so
        callers (and the bestseller reconciliation) are unaffected by which ran.
        ``max_pages`` applies only to the fallback Orders sweep.
        """
        use_shopifyql = os.getenv("BESTSELLER_USE_SHOPIFYQL", "true").strip().lower() not in (
            "0", "false", "no", "off",
        )
        if use_shopifyql:
            try:
                return await self._afetch_sales_volume_via_shopifyql(
                    days=days, limit=limit or SHOPIFYQL_SALES_LIMIT,
                )
            except Exception as e:
                # Scope missing / query rejected / transient API error: degrade to
                # the Orders sweep rather than failing the client's refresh.
                logger.warning(
                    f"⚠️ ShopifyQL sales fetch failed ({e}); falling back to the "
                    f"Orders-API sweep"
                )
        return await self._afetch_sales_volume_via_orders(days=days, max_pages=max_pages)

    async def _afetch_sales_volume_via_shopifyql(
        self,
        days: int,
        limit: int,
    ) -> SalesVolumeResult:
        """Rank product sales over the last *days* days via a single ShopifyQL query.

        ShopifyQL aggregates server-side, so this is one GraphQL request rather
        than an orders sweep. ``net_items_sold`` is returns-adjusted (refunded
        units are netted out). The whole window is ranked in one shot, so the
        result is always ``complete`` — there is no partial-sweep truncation to
        guard against. Product ids come back numeric and are normalised to GID
        form so the result is drop-in compatible with the Orders-API path.
        """
        shopifyql = (
            f"FROM sales SHOW net_items_sold "
            f"GROUP BY product_id "
            f"ORDER BY net_items_sold DESC "
            f"LIMIT {int(limit)} "
            f"SINCE -{int(days)}d UNTIL today"
        )
        graphql_query = """
        query bestsellerSales($q: String!) {
            shopifyqlQuery(query: $q) {
                parseErrors
                tableData {
                    columns { name }
                    rows
                }
            }
        }
        """
        headers = {
            "X-Shopify-Access-Token": self.access_token,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            # shopify_graphql_post paces per-shop and retries 429 / 5xx / network /
            # THROTTLED; non-retryable GraphQL + 4xx errors propagate (→ fallback).
            data = await shopify_graphql_post(
                client, self.graphql_url, headers,
                {"query": graphql_query, "variables": {"q": shopifyql}},
                rate_limit_key=self._rate_limit_key,
            )

        result = (data.get("data") or {}).get("shopifyqlQuery") or {}
        parse_errors = result.get("parseErrors") or []
        if parse_errors:
            # A malformed query is a deterministic bug, not transient — surface it
            # so the caller falls back to the Orders sweep instead of mistaking the
            # store for one with no sales (which would never demote a bestseller).
            raise RuntimeError(f"ShopifyQL parse errors: {parse_errors}")

        table = result.get("tableData") or {}
        columns = {c.get("name") for c in (table.get("columns") or [])}
        if "product_id" not in columns or "net_items_sold" not in columns:
            raise RuntimeError(f"ShopifyQL response missing expected columns: {columns}")

        sales: Dict[str, int] = {}
        for row in (table.get("rows") or []):
            raw_id = str((row.get("product_id") if isinstance(row, dict) else None) or "").strip()
            if not raw_id:
                continue
            try:
                raw_qty = row.get("net_items_sold") if isinstance(row, dict) else None
                qty = int(float(raw_qty))
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue  # net returns ≥ sales — not a seller in this window
            # ShopifyQL returns the numeric id; rebuild the GID so downstream
            # reconciliation (which normalises GID → numeric) is unaffected.
            sales[f"gid://shopify/Product/{raw_id.split('/')[-1]}"] = qty

        logger.info(
            f"📊 ShopifyQL ranked {len(sales)} products by net_items_sold "
            f"(last {days} days, limit {limit})"
        )
        return SalesVolumeResult(
            sales=sales, complete=True, pages_fetched=1, truncated_reason=None,
        )

    _ENGAGEMENT_FALLBACK_CHAIN = ["product_title", "landing_page_path"]

    async def afetch_product_engagement(
        self,
        days: int,
        limit: int = SHOPIFYQL_ENGAGEMENT_LIMIT,
        _fallback_group_by: Optional[str] = None,
        _tried: Optional[set] = None,
    ) -> EngagementResult:
        """Fetch unique storefront sessions per product over the last *days* days.

        Uses ShopifyQL's ``sessions`` dataset — one server-side-ranked GraphQL
        call, exactly like the sales fetch. Products are ranked by ``sessions``
        DESC and capped at *limit*: the traffic window whose analytics fields the
        monthly cron writes. There is no partial-sweep concept (the whole window
        is ranked in one shot), so a successful fetch is always complete and any
        error raises rather than returning a partial/empty map — the caller then
        skips engagement for that client instead of persisting bad data.

        Grouped by ``SHOPIFYQL_ENGAGEMENT_GROUP_BY`` (default ``product_id``,
        normalised to GID form so the map joins directly to the sales map).
        When the configured group-by column is rejected by the store's sessions
        dataset, the method retries with fallback dimensions in order:
        ``product_title`` → ``landing_page_path``.
        """
        group_by = _fallback_group_by or SHOPIFYQL_ENGAGEMENT_GROUP_BY
        tried = (_tried or set()) | {group_by}
        shopifyql = (
            f"FROM sessions SHOW sessions "
            f"GROUP BY {group_by} "
            f"ORDER BY sessions DESC "
            f"LIMIT {int(limit)} "
            f"SINCE -{int(days)}d UNTIL today"
        )
        graphql_query = """
        query productEngagement($q: String!) {
            shopifyqlQuery(query: $q) {
                parseErrors
                tableData {
                    columns { name }
                    rows
                }
            }
        }
        """
        headers = {
            "X-Shopify-Access-Token": self.access_token,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            data = await shopify_graphql_post(
                client, self.graphql_url, headers,
                {"query": graphql_query, "variables": {"q": shopifyql}},
                rate_limit_key=self._rate_limit_key,
            )

        result = (data.get("data") or {}).get("shopifyqlQuery") or {}
        parse_errors = result.get("parseErrors") or []
        if parse_errors:
            error_texts = [str(e) for e in parse_errors]
            column_not_found = any(
                "column not found" in t.lower() and group_by in t
                for t in error_texts
            )
            if column_not_found:
                next_fallback = next(
                    (fb for fb in self._ENGAGEMENT_FALLBACK_CHAIN if fb not in tried),
                    None,
                )
                if next_fallback:
                    logger.info(
                        f"📈 ShopifyQL sessions: GROUP BY {group_by!r} not supported "
                        f"— retrying with {next_fallback!r} fallback"
                    )
                    return await self.afetch_product_engagement(
                        days=days, limit=limit,
                        _fallback_group_by=next_fallback, _tried=tried,
                    )
            raise RuntimeError(f"ShopifyQL sessions parse errors: {parse_errors}")

        table = result.get("tableData") or {}
        columns = {c.get("name") for c in (table.get("columns") or [])}
        if group_by not in columns or "sessions" not in columns:
            raise RuntimeError(
                f"ShopifyQL sessions response missing expected columns: {columns}"
            )

        grouped_by_id = group_by == "product_id"
        grouped_by_path = group_by == "landing_page_path"
        sessions: Dict[str, int] = {}
        for row in (table.get("rows") or []):
            if not isinstance(row, dict):
                continue
            raw_key = str(row.get(group_by) or "").strip()
            if not raw_key:
                continue
            try:
                count = int(float(row.get("sessions")))
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            if grouped_by_id:
                key = f"gid://shopify/Product/{raw_key.split('/')[-1]}"
            elif grouped_by_path:
                # /products/<handle> or /products/<handle>?variant=... → extract handle.
                # Skip non-product paths (collections, pages, homepage).
                path = raw_key.split("?")[0].rstrip("/")
                if not path.startswith("/products/"):
                    continue
                key = path.split("/products/", 1)[1]
                if not key or "/" in key:
                    continue
            else:
                key = raw_key
            sessions[key] = sessions.get(key, 0) + count

        effective_group_by = "landing_page_path" if grouped_by_path else group_by
        logger.info(
            f"📈 ShopifyQL fetched sessions for {len(sessions)} products "
            f"(last {days} days, group_by={effective_group_by}, limit {limit})"
        )
        return EngagementResult(sessions=sessions, grouped_by=group_by, complete=True)

    async def afetch_campaign_sessions(
        self,
        days: int,
        limit: int = SHOPIFYQL_ENGAGEMENT_LIMIT,
    ) -> Dict[str, int]:
        """Fetch storefront sessions grouped by ``utm_campaign``.

        Supplements the per-product engagement fetch for stores whose sessions
        dataset lacks product-level dimensions (``product_id``, ``product_title``).
        Campaign names often correspond to product names, so the caller can
        fuzzy-match them to products for a richer session signal.

        Returns ``{campaign_name: session_count}``; entries with null/empty
        campaign names are excluded.  Raises on ShopifyQL parse errors (the
        caller should catch and treat as best-effort).
        """
        shopifyql = (
            f"FROM sessions SHOW sessions "
            f"GROUP BY utm_campaign "
            f"ORDER BY sessions DESC "
            f"LIMIT {int(limit)} "
            f"SINCE -{int(days)}d UNTIL today"
        )
        graphql_query = """
        query campaignSessions($q: String!) {
            shopifyqlQuery(query: $q) {
                parseErrors
                tableData {
                    columns { name }
                    rows
                }
            }
        }
        """
        headers = {
            "X-Shopify-Access-Token": self.access_token,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            data = await shopify_graphql_post(
                client, self.graphql_url, headers,
                {"query": graphql_query, "variables": {"q": shopifyql}},
                rate_limit_key=self._rate_limit_key,
            )

        result = (data.get("data") or {}).get("shopifyqlQuery") or {}
        parse_errors = result.get("parseErrors") or []
        if parse_errors:
            raise RuntimeError(f"ShopifyQL campaign sessions parse errors: {parse_errors}")

        table = result.get("tableData") or {}
        campaigns: Dict[str, int] = {}
        for row in (table.get("rows") or []):
            if not isinstance(row, dict):
                continue
            name = row.get("utm_campaign")
            if not name or not str(name).strip():
                continue
            try:
                count = int(float(row.get("sessions", 0)))
            except (TypeError, ValueError):
                continue
            if count > 0:
                campaigns[str(name).strip()] = count

        logger.info(
            f"📈 ShopifyQL fetched campaign sessions for {len(campaigns)} campaigns "
            f"(last {days} days, limit {limit})"
        )
        return campaigns

    async def _afetch_sales_volume_via_orders(
        self,
        days: int = 10,
        max_pages: int = 20,
    ) -> SalesVolumeResult:
        """Aggregate product sales volume from the Shopify Orders API.

        Paginates the GraphQL ``orders`` query (50 orders/page) over the last
        *days* days, counting line-item quantities per product GID. Each page is
        retried with bounded exponential backoff on transient failures:
          * HTTP 429 / GraphQL ``THROTTLED`` — Shopify's cost rate limiter.
          * HTTP 5xx — server hiccups.
          * network errors / timeouts (``httpx.TransportError``).
        Deterministic failures are NOT retried: non-429 4xx (auth/permission)
        propagate to the caller, and non-throttle GraphQL errors stop the sweep.

        Returns a :class:`SalesVolumeResult`; ``complete`` is False when the
        sweep is cut short (``max_pages`` / sustained ``throttled`` /
        ``transient_error`` / ``graphql_error``), so partial counts are never
        mistaken for the full picture.
        """
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        query = """
        query getOrders($cursor: String, $queryFilter: String) {
            orders(first: 50, after: $cursor, query: $queryFilter) {
                pageInfo {
                    hasNextPage
                    endCursor
                }
                edges {
                    node {
                        lineItems(first: 100) {
                            edges {
                                node {
                                    quantity
                                    product {
                                        id
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        """

        headers = {
            "X-Shopify-Access-Token": self.access_token,
            "Content-Type": "application/json",
        }

        sales: Dict[str, int] = {}
        cursor: Optional[str] = None
        has_next = True
        pages_fetched = 0
        truncated_reason: Optional[str] = None
        query_filter = f"created_at:>={since}"

        # Reuse the shared retry policy (ShopifyThrottle.attempts()) for the
        # per-page retry mechanics — max-attempts, capped exponential backoff,
        # Retry-After — instead of a hand-rolled loop, while keeping this sweep's
        # own per-page cursor handling and partial-completeness reporting. (The
        # @retry_shopify decorator can't be used here: it retries the WHOLE call,
        # which would restart the sweep from page 1 and discard accumulated
        # sales, and it is all-or-nothing so it can't return a partial result.)
        throttle = get_shopify_throttle()

        async with httpx.AsyncClient(timeout=30.0) as client:
            while has_next and pages_fetched < max_pages:
                variables: Dict[str, Any] = {"queryFilter": query_filter}
                if cursor:
                    variables["cursor"] = cursor

                # Per-page fetch. The cursor is not advanced until a page
                # succeeds, so retries re-request the same page (no gaps, no
                # double counting). 429 / 5xx / GraphQL THROTTLED / network are
                # retried by the shared throttle; on exhaustion or a terminal
                # error we stop and mark the sweep truncated so the caller never
                # demotes a bestseller on partial data.
                page_data: Optional[Dict[str, Any]] = None
                retryable_reason: Optional[str] = None  # classify the last retry
                try:
                    async for attempt in throttle.attempts():
                        with attempt:
                            # Proactive per-shop pacing inside each attempt so a
                            # retry re-paces and this sweep never starves
                            # interactive Shopify calls running elsewhere.
                            await get_shopify_rate_limiter().acquire(self._rate_limit_key)
                            resp = await client.post(
                                self.graphql_url,
                                headers=headers,
                                json={"query": query, "variables": variables},
                            )

                            # 429 / 5xx → retryable (honour Retry-After if present).
                            if resp.status_code == 429:
                                retryable_reason = "throttled"
                                raise ShopifyRetryableError(
                                    "Shopify orders HTTP 429",
                                    retry_after=self._retry_after_seconds(resp),
                                )
                            if resp.status_code >= 500:
                                retryable_reason = "transient_error"
                                raise ShopifyRetryableError(
                                    f"Shopify orders HTTP {resp.status_code}",
                                    retry_after=self._retry_after_seconds(resp),
                                )
                            # Non-429 4xx → deterministic auth/client error; propagate.
                            resp.raise_for_status()

                            data = resp.json()
                            errors = data.get("errors")
                            if errors:
                                if self._is_throttled(errors):
                                    # Honour Shopify's cost throttleStatus for the
                                    # backoff when present (else exponential).
                                    retryable_reason = "throttled"
                                    raise ShopifyRetryableError(
                                        f"Shopify GraphQL THROTTLED: {errors}",
                                        retry_after=self._throttle_backoff_seconds(
                                            data, attempt.retry_state.attempt_number
                                        ),
                                    )
                                # Non-throttle GraphQL error → terminal for this
                                # sweep; stop with whatever we have so far.
                                truncated_reason = "graphql_error"
                                logger.warning(f"⚠️ GraphQL errors fetching orders: {errors}")
                                break

                            page_data = data
                            break  # page fetched successfully
                except httpx.TransportError as e:
                    # Network/timeout retries exhausted.
                    truncated_reason = "transient_error"
                    logger.warning(
                        f"⚠️ Shopify orders network error after retries "
                        f"(page {pages_fetched + 1}): {e}"
                    )
                except ShopifyRetryableError as e:
                    # 429 / 5xx / THROTTLED retries exhausted; retryable_reason
                    # carries the exact cause for truncated_reason.
                    truncated_reason = retryable_reason or "throttled"
                    logger.warning(
                        f"⚠️ Shopify orders transient failure after retries "
                        f"(page {pages_fetched + 1}): {e}"
                    )

                if page_data is None:
                    # Throttled-out, transient-exhausted, or GraphQL error →
                    # stop and mark truncated.
                    if truncated_reason is None:
                        truncated_reason = "transient_error"
                    break

                orders_data = (page_data.get("data") or {}).get("orders", {})
                page_info = orders_data.get("pageInfo", {})
                has_next = page_info.get("hasNextPage", False)
                cursor = page_info.get("endCursor")

                for edge in orders_data.get("edges", []):
                    order = edge.get("node", {})
                    for li_edge in (
                        order.get("lineItems", {}).get("edges", [])
                    ):
                        li = li_edge.get("node", {})
                        product = li.get("product")
                        if not product:
                            continue
                        product_gid = product.get("id", "")
                        if product_gid:
                            sales[product_gid] = sales.get(
                                product_gid, 0
                            ) + (li.get("quantity") or 0)

                pages_fetched += 1

        # Completeness: only a natural pagination end counts as complete. Hitting
        # max_pages while more pages remain is a (safe-to-add, unsafe-to-remove)
        # truncation.
        if truncated_reason is None and has_next and pages_fetched >= max_pages:
            truncated_reason = "max_pages"
        complete = truncated_reason is None

        if not sales:
            logger.info(f"📊 No orders found in the last {days} days")
            return SalesVolumeResult(
                sales={}, complete=complete,
                pages_fetched=pages_fetched, truncated_reason=truncated_reason,
            )

        logger.info(
            f"📊 Aggregated sales for {len(sales)} products from {pages_fetched} "
            f"order pages (last {days} days, complete={complete}"
            + (f", truncated={truncated_reason}" if truncated_reason else "") + ")"
        )
        return SalesVolumeResult(
            sales=sales, complete=complete,
            pages_fetched=pages_fetched, truncated_reason=truncated_reason,
        )

    async def _normalize_product(self, raw: dict) -> NormalizedProduct:
        """
        Normalize raw Shopify GraphQL product to NormalizedProduct.
        
        Args:
            raw: Raw product data from Shopify GraphQL API
            
        Returns:
            NormalizedProduct instance
        """
        # Extract product ID (remove gid:// prefix)
        raw_id = raw.get("id", "")
        product_id = raw_id.split("/")[-1] if "/" in raw_id else raw_id
        
        # Extract variants data
        variant_edges = raw.get("variants", {}).get("edges", [])
        variants = [edge.get("node", {}) for edge in variant_edges]
        
        # Calculate price range from variants
        prices = []
        compare_prices = []
        for v in variants:
            price_str = v.get("price")
            if price_str:
                try:
                    prices.append(float(price_str))
                except (ValueError, TypeError):
                    pass
            compare_str = v.get("compareAtPrice")
            if compare_str:
                try:
                    compare_prices.append(float(compare_str))
                except (ValueError, TypeError):
                    pass
        
        price_min = min(prices) if prices else 0.0
        price_max = max(prices) if prices else 0.0
        compare_at_price_min = min(compare_prices) if compare_prices else None
        compare_at_price_max = max(compare_prices) if compare_prices else None

        discount_pct = 0.0
        if compare_at_price_min and compare_at_price_min > 0 and price_min < compare_at_price_min:
            discount_pct = round((compare_at_price_min - price_min) / compare_at_price_min * 100, 1)
        
        # Extract sizes and colors from variant options
        sizes = set()
        colors = set()
        available_sizes = set()
        available_colors = set()
        in_stock = False
        
        for variant in variants:
            inv_qty = variant.get("inventoryQuantity", 0)
            inv_policy = variant.get("inventoryPolicy", "")
            variant_in_stock = variant_is_in_stock(inv_qty, inv_policy)
            if variant_in_stock:
                in_stock = True
            
            for opt in variant.get("selectedOptions", []):
                opt_name = opt.get("name", "").lower()
                opt_value = opt.get("value", "")
                
                if "size" in opt_name:
                    sizes.add(opt_value)
                    if variant_in_stock:
                        available_sizes.add(opt_value)
                elif "color" in opt_name or "colour" in opt_name:
                    colors.add(opt_value)
                    if variant_in_stock:
                        available_colors.add(opt_value)
        
        # Also check product options for better coverage
        options = raw.get("options", [])
        for option in options:
            option_name = option.get("name", "").lower()
            values = option.get("values", [])
            
            if "size" in option_name:
                sizes.update(values)
            elif "color" in option_name or "colour" in option_name:
                colors.update(values)
        
        # Extract images
        image_edges = raw.get("images", {}).get("edges", [])
        all_images = [edge.get("node", {}).get("url", "") for edge in image_edges if edge.get("node", {}).get("url")]
        image_url = all_images[0] if all_images else ""
        
        # Build product URL
        handle = raw.get("handle", "")
        if self.website_base_url:
            product_url = f"{self.website_base_url}/products/{handle}"
        else:
            product_url = f"https://{self.shop_domain}/products/{handle}"
        
        # Clean description — always prefer descriptionHtml → _strip_html()
        # so that both GraphQL and webhook paths produce identical text.
        # GraphQL's plain-text `description` strips tags without adding spaces,
        # while _strip_html() adds spaces, causing hash mismatches.
        description_html = raw.get("descriptionHtml", "") or ""
        description = self._strip_html(description_html) if description_html else (raw.get("description", "") or "")
        
        # Parse tags
        tags = raw.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        
        # Extract collections
        collection_edges = raw.get("collections", {}).get("edges", [])
        collections = [edge.get("node", {}).get("title", "") for edge in collection_edges if edge.get("node", {}).get("title")]
        
        # Extract metafields — store both by full key (namespace.key) and by bare key
        # Also resolve Metaobject GID references inline.
        # Defensive ``or {}`` / ``or []`` because Shopify may return the keys
        # with ``null`` values (not just omitted), and dict.get's default only
        # fires on missing keys.
        metafield_edges = (raw.get("metafields") or {}).get("edges") or []
        metafields = {}
        metafields_by_key = {}
        for edge in metafield_edges:
            if not edge:
                continue
            node = edge.get("node") or {}
            if not node:
                continue
            ns = node.get("namespace", "")
            k = node.get("key", "")
            full_key = f"{ns}.{k}"
            mtype = node.get("type", "")
            raw_value = node.get("value")
            
            # Resolve Metaobject / TaxonomyValue references to display names
            resolved_value = raw_value
            if "metaobject_reference" in mtype or "taxonomy_value" in mtype:
                resolved_value = self._resolve_metaobject_references(node)
            
            entry = {"value": resolved_value, "type": mtype}
            metafields[full_key] = entry
            bare = k.lower().replace("-", "_").replace(" ", "_")
            metafields_by_key[bare] = entry
        
        logger.info(f"📋 Metafields for '{raw.get('title', '')}': {list(metafields.keys())}")
        
        # Extract specific metafields (try full key first, then bare key fallback)
        fabric = self._extract_metafield(metafields, ["custom.fabric", "product.fabric", "custom.material", "product.material"]) \
            or self._extract_metafield(metafields_by_key, ["fabric", "material"])
        care_instructions = self._extract_metafield(metafields, ["custom.care_instructions", "product.care", "custom.care", "product.care_instructions"]) \
            or self._extract_metafield(metafields_by_key, ["care_instructions", "care"])
        fit_type = self._extract_metafield(metafields, ["custom.fit", "product.fit", "custom.fit_type", "product.fit_type"]) \
            or self._extract_metafield(metafields_by_key, ["fit", "fit_type"])
        size_chart = await self._extract_size_chart(metafields) or await self._extract_size_chart(metafields_by_key)
        
        # Category-level color (Shopify taxonomy uses various key names).
        # Always merge into colors so category metafield colors are never lost.
        _COLOR_KEYS = {"color_pattern", "color", "colour", "colors", "colours"}
        for ck in _COLOR_KEYS:
            cat_colors = self._extract_metafield_list(metafields_by_key, [ck])
            if cat_colors:
                colors.update(cat_colors)
        
        # Build cleaned all_metafields list AND generic metafield_attributes
        # dict using resolved values. Business-agnostic: every metafield the
        # merchant has configured is captured, whether it's "neckline" for
        # fashion or "ingredients" for cosmetics.
        skip_namespaces = {"judgeme"}
        # Keys already extracted as top-level NormalizedProduct fields — don't
        # duplicate into metafield_attributes.
        _ALREADY_EXTRACTED_KEYS = {
            "fabric", "material", "care_instructions", "care",
            "fit", "fit_type", "size_chart", "sizing", "size_guide",
        } | _COLOR_KEYS
        all_metafields_list = []
        metafield_attributes: Dict[str, str] = {}
        for edge in metafield_edges:
            node = edge.get("node", {})
            ns = node.get("namespace", "")
            if ns in skip_namespaces:
                continue
            key = node.get("key", "")
            bare = key.lower().replace("-", "_").replace(" ", "_")
            mtype = node.get("type", "")
            if "metaobject_reference" in mtype or "taxonomy_value" in mtype:
                value = self._resolve_metaobject_references(node)
            else:
                value = node.get("value", "")
            if isinstance(value, str) and value.startswith("gid://"):
                continue
            if isinstance(value, str) and value.startswith('["gid://'):
                continue
            if value and str(value).strip():
                all_metafields_list.append({"key": key, "value": value})
                if bare not in _ALREADY_EXTRACTED_KEYS:
                    # Flatten JSON list values to comma-separated strings
                    str_val = str(value)
                    try:
                        parsed = json.loads(str_val)
                        if isinstance(parsed, list):
                            clean = [str(v) for v in parsed if v and not str(v).startswith("gid://")]
                            str_val = ", ".join(clean) if clean else ""
                    except (json.JSONDecodeError, TypeError):
                        pass
                    if str_val:
                        metafield_attributes[bare] = str_val

        # Brand: prefer metafield "brand" over Shopify vendor
        brand = self._extract_metafield(metafields_by_key, ["brand"])
        
        # SEO
        seo = raw.get("seo", {})
        seo_title = seo.get("title")
        seo_description = seo.get("description")
        
        # Build variant list for storage
        variant_list = []
        for v in variants:
            variant_id = v.get("id", "")
            if "/" in variant_id:
                variant_id = variant_id.split("/")[-1]

            inv_qty = v.get("inventoryQuantity", 0)
            inv_policy = v.get("inventoryPolicy", "")
            sel_opts = v.get("selectedOptions", [])

            variant_list.append({
                "id": variant_id,
                "title": v.get("title", ""),
                "sku": v.get("sku", ""),
                "price": v.get("price"),
                "compare_at_price": v.get("compareAtPrice"),
                "inventory_quantity": inv_qty,
                "available": variant_is_in_stock(inv_qty, inv_policy),
                "selected_options": sel_opts,
                "option1": sel_opts[0]["value"] if len(sel_opts) > 0 else None,
                "option2": sel_opts[1]["value"] if len(sel_opts) > 1 else None,
                "option3": sel_opts[2]["value"] if len(sel_opts) > 2 else None,
            })
        
        # Share of variants currently purchasable — feeds the search layer's
        # availability ranking/filter. Single source of truth is the per-variant
        # `available` flag computed above via variant_is_in_stock.
        variant_availability_pct = (
            round(sum(1 for v in variant_list if v.get("available")) / len(variant_list) * 100, 1)
            if variant_list else 100.0
        )

        return NormalizedProduct(
            id=product_id,
            title=raw.get("title", ""),
            handle=handle,
            product_type=raw.get("productType", ""),
            vendor=raw.get("vendor", ""),
            tags=tags,
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
            total_inventory=raw.get("totalInventory", 0),
            available_sizes=list(available_sizes),
            available_colors=list(available_colors),
            variant_availability_pct=variant_availability_pct,
            status=raw.get("status", "active").lower(),
            description=description,
            fabric=fabric,
            care_instructions=care_instructions,
            fit_type=fit_type,
            size_chart=size_chart,
            seo_title=seo_title,
            seo_description=seo_description,
            variants=variant_list,
            created_at=raw.get("createdAt"),
            updated_at=raw.get("updatedAt"),
            published_at=raw.get("publishedAt"),
            collections=collections,
            brand=brand,
            metafield_attributes=metafield_attributes,
            all_metafields=all_metafields_list,
        )
    
    def _resolve_metaobject_references(self, metafield_node: Dict) -> str:
        """Resolve Metaobject / TaxonomyValue GID references to display names.

        Uses the inline ``reference`` (single) or ``references`` (list) data
        returned by the GraphQL query.

        Note: Shopify returns ``"node": null`` (not omitted) when a metafield
        references a deleted/dangling Metaobject or TaxonomyValue. ``dict.get(k, {})``
        only uses the default when the key is *missing*, not when the value is
        *explicitly None*, so we coerce with ``or {}`` everywhere to avoid
        ``AttributeError: 'NoneType' object has no attribute 'get'``.
        """
        mtype = metafield_node.get("type", "")

        is_list = mtype.startswith("list.")

        if is_list:
            refs_container = metafield_node.get("references") or {}
            refs_data = refs_container.get("edges") or []
            names = []
            for ref_edge in refs_data:
                if not ref_edge:
                    continue
                ref_node = ref_edge.get("node") or {}
                name = (
                    ref_node.get("name")            # TaxonomyValue
                    or ref_node.get("displayName")   # Metaobject
                    or ref_node.get("handle", "")
                )
                if name:
                    names.append(name)
            if names:
                return json.dumps(names)
        else:
            ref_data = metafield_node.get("reference") or {}
            if ref_data:
                name = (
                    ref_data.get("name")            # TaxonomyValue
                    or ref_data.get("displayName")   # Metaobject
                    or ref_data.get("handle", "")
                )
                if name:
                    return name

        return metafield_node.get("value", "")
    
    def _extract_metafield(self, metafields: Dict[str, Any], keys: List[str]) -> Optional[str]:
        """
        Extract a metafield value by trying multiple possible keys.
        Handles resolved Metaobject references (JSON arrays → joined string).
        """
        for key in keys:
            if key in metafields:
                value = metafields[key].get("value")
                if value:
                    # Try parsing as JSON (handles resolved metaobject lists like '["Hooded"]')
                    try:
                        parsed = json.loads(value)
                        if isinstance(parsed, list):
                            flat = [str(v) for v in parsed if v and not str(v).startswith("gid://")]
                            return ", ".join(flat) if flat else None
                        elif isinstance(parsed, str):
                            if parsed.startswith("gid://"):
                                return None
                            return parsed
                        elif isinstance(parsed, dict):
                            return json.dumps(parsed)
                    except (json.JSONDecodeError, TypeError):
                        pass
                    s = str(value)
                    if s.startswith("gid://"):
                        return None
                    return s
        return None
    
    def _extract_metafield_list(self, metafields: Dict[str, Any], keys: List[str]) -> Optional[List[str]]:
        """
        Extract a list-type metafield value by trying multiple possible keys.
        Handles JSON arrays, comma-separated strings, and resolved Metaobject references.
        Filters out unresolved GID references.
        """
        for key in keys:
            if key in metafields:
                value = metafields[key].get("value")
                if value:
                    try:
                        parsed = json.loads(value)
                        if isinstance(parsed, list):
                            clean = [str(v) for v in parsed if v and not str(v).startswith("gid://")]
                            return clean if clean else None
                    except (json.JSONDecodeError, TypeError):
                        pass
                    if isinstance(value, str) and "," in value:
                        items = [v.strip() for v in value.split(",") if v.strip() and not v.strip().startswith("gid://")]
                        return items if items else None
                    s = str(value)
                    if not s.startswith("gid://"):
                        return [s]
        return None
    
    async def _extract_size_chart(self, metafields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Extract size chart from metafields.
        
        Common patterns:
        - custom.size_chart
        - product.size_chart
        - custom.sizing
        
        If the value is a GID reference (e.g., gid://shopify/Page/123), 
        resolves it to fetch actual page content.
        
        Args:
            metafields: Dictionary of metafields
            
        Returns:
            Size chart dictionary if found, None otherwise
        """
        size_chart_keys = [
            "custom.size_chart",
            "product.size_chart",
            "custom.sizing",
            "product.sizing",
            "custom.size_guide",
            "product.size_guide"
        ]
        
        for key in size_chart_keys:
            if key in metafields:
                value = metafields[key].get("value")
                if value:
                    # Check if this is a GID reference to a Page
                    if isinstance(value, str) and value.startswith("gid://shopify/"):
                        # Resolve the GID to actual page content
                        page_content = await self._fetch_page_content_from_gid(value)
                        if page_content:
                            return {"content": page_content, "source_gid": value}
                        else:
                            # Fallback to storing raw GID if fetch fails
                            return {"raw": value}
                    
                    try:
                        # Try to parse as JSON
                        return json.loads(value)
                    except:
                        # If not JSON, return as simple dict with content
                        return {"content": value}
        
        return None
    
    async def _fetch_page_content_from_gid(self, gid: str) -> Optional[str]:
        """
        Fetch page content from Shopify using a GID reference.
        
        Args:
            gid: The Shopify GID (e.g., "gid://shopify/Page/148405027138")
            
        Returns:
            The page body content as string, or None if failed
        """
        if not gid or not gid.startswith("gid://shopify/"):
            return None
        
        # GraphQL query to fetch page content
        query = """
        query getPage($id: ID!) {
            page(id: $id) {
                id
                title
                handle
                body
                bodySummary
            }
        }
        """
        
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": self.access_token
        }
        
        payload = {
            "query": query,
            "variables": {"id": gid}
        }
        
        try:
            client = await get_shared_async_http_client()
            response = await client.post(
                self.graphql_url, 
                headers=headers, 
                json=payload, 
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                
                if "errors" in data:
                    errors = data.get("errors") or []
                    resource_not_found = any(
                        isinstance(error, dict)
                        and error.get("extensions", {}).get("code") == "RESOURCE_NOT_FOUND"
                        for error in errors
                    )
                    if resource_not_found:
                        logger.info(f"Shopify page GID not found while resolving size chart {gid}; skipping page content")
                    else:
                        logger.warning(f"⚠️ GraphQL errors fetching page {gid}: {errors}")
                    return None
                
                page_data = data.get("data", {}).get("page")
                
                if page_data:
                    # Return the body content (HTML with size chart table)
                    body_content = page_data.get("body") or page_data.get("bodySummary") or ""
                    if body_content:
                        logger.info(f"✅ Resolved size chart GID {gid} -> {len(body_content)} chars")
                        return body_content
                    else:
                        logger.warning(f"⚠️ Page {gid} has no body content")
                        return None
                else:
                    logger.warning(f"⚠️ No page data returned for GID: {gid}")
                    return None
            else:
                logger.error(f"❌ HTTP {response.status_code} fetching page {gid}")
                return None
                
        except Exception as e:
            logger.error(f"❌ Exception fetching page content for {gid}: {str(e)}")
            return None
    
    def _strip_html(self, text: str) -> str:
        """Strip HTML tags from text."""
        if not text:
            return ""
        
        # Simple regex to strip HTML tags
        clean = re.sub(r'<[^>]+>', ' ', text)
        # Normalize whitespace
        clean = re.sub(r'\s+', ' ', clean)
        return clean.strip()
