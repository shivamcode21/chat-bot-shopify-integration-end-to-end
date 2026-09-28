import asyncio
import logging
import re
from typing import Dict, Any, Optional, List
from fashion_bot.interfaces.product import ProductInterface
from fashion_bot.utils.utils import log_with_trace_id
from fashion_bot.shopify.modules.product_handlers import (
    ashopify_get_product_by_handle_graphql,
    ashopify_get_product_by_id_graphql,
    ashopify_get_top_selling_products_rest,
    ashopify_graphql_product_search,
)
from fashion_bot.config_manager import (
    BLOOMERCE_INTEGRATION_REQUIRED_MESSAGE,
    aget_shopify_config,
    aget_config,
)
from fashion_bot.utils.product_utils import (
    aget_shopify_to_website_mapping,
    replace_shopify_url,
)
import json

logger = logging.getLogger(__name__)

class ShopifyProductAdapter(ProductInterface):
    def __init__(self, client_id: str = None):
        self.client_id = client_id
        self._config: Optional[Dict[str, Any]] = None
        self._website_url: Optional[str] = None
        self._website_url_loaded: bool = False

    async def _aget_website_url(self, state: Optional[Dict] = None) -> Optional[str]:
        """Resolve the client's canonical website URL (e.g. https://groovee.in).

        Cached per adapter instance. Used to keep the internal
        ``*.myshopify.com`` staging domain out of customer-facing product URLs.
        """
        if self._website_url_loaded:
            return self._website_url
        client_id = self.client_id
        if not client_id and state:
            client_id = state.get("client_id")
        if client_id:
            self._website_url = await aget_shopify_to_website_mapping(client_id)
        self._website_url_loaded = True
        return self._website_url

    @classmethod
    async def create(cls, client_id: str = None) -> "ShopifyProductAdapter":
        """Factory method that eagerly loads config once."""
        adapter = cls(client_id=client_id)
        adapter._config = await aget_shopify_config(client_id=client_id)
        return adapter

    async def _aget_config(self, state: Optional[Dict] = None):
        if self._config is not None:
            return self._config
        client_id = self.client_id
        if not client_id and state:
            client_id = state.get('client_id')
        self._config = await aget_shopify_config(client_id=client_id)
        return self._config

    @staticmethod
    def _ensure_shopify_config(config: Dict[str, Any], state: Optional[Dict] = None) -> None:
        access_token = config.get("access_token")
        shop_url = config.get("shop_url")
        if access_token and shop_url:
            return
        client_id = state.get("client_id") if state else None
        log_with_trace_id(
            state,
            f"[SHOPIFY_CONFIG_MISSING] op=ShopifyProductAdapter._ensure_shopify_config "
            f"client_id={client_id} reason=no shopify_details in client_configs "
            f"(access_token={'present' if access_token else 'missing'}, "
            f"shop_url={'present' if shop_url else 'missing'})",
            "error",
        )
        raise ValueError(BLOOMERCE_INTEGRATION_REQUIRED_MESSAGE)

    @staticmethod
    def _raise_sync_unavailable(method_name: str):
        raise RuntimeError(
            f"ShopifyProductAdapter.{method_name} is async-only. Use the corresponding `await a...` method."
        )

    def get_product_details_by_url(self, product_url: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("get_product_details_by_url")

    async def aget_product_details_by_url(self, product_url: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        log_with_trace_id(state, f"ShopifyAdapter: Async get product details from URL: {product_url}")
        match = re.search(r"/products/([^/?#]+)", product_url)
        if not match:
            raise ValueError("Could not extract product handle from URL")
        handle = match.group(1)
        return await self.aget_product_details_by_id(handle, state)

    def get_product_details_by_id(self, product_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        self._raise_sync_unavailable("get_product_details_by_id")

    async def aget_product_details_by_id(self, product_id: str, state: Optional[Dict] = None, id_type: str = "handle") -> Dict[str, Any]:
        config = await self._aget_config(state)
        self._ensure_shopify_config(config, state)

        access_token = config.get("access_token")
        shop_url = config.get("shop_url")

        if not access_token or not shop_url:
            log_with_trace_id(state, "Shopify configuration not found", "error")
            raise ValueError("Shopify configuration not found")

        website_url = await self._aget_website_url(state)
        is_numeric = id_type == "numeric_id" or product_id.startswith("gid://")

        if is_numeric:
            result = await ashopify_get_product_by_id_graphql(
                product_id=product_id,
                access_token=access_token,
                shop_url=shop_url,
                api_version="2024-04",
                formatted_response=True,
                website_url=website_url,
            )
        else:
            result = await ashopify_get_product_by_handle_graphql(
                handle=product_id,
                access_token=access_token,
                shop_url=shop_url,
                api_version="2024-04",
                formatted_response=True,
                website_url=website_url,
            )

        if not result.get("success"):
            # Log at DEBUG only; the exception is re-raised and the caller logs the
            # wrapped "❌ Product fetch by ID failed" message at ERROR. Keeping this at
            # ERROR would double every failed lookup in Loki (same trace_id, back to back).
            log_with_trace_id(state, f"GraphQL API error: {result.get('error', 'Unknown error')}", "debug")
            raise ValueError(f"GraphQL API error: {result.get('error', 'Unknown error')}")

        product = result.get("product", {})
        if product:
            self._ensure_urls([product], shop_url, website_url)
        return product

    def search_products(self, query: str, limit: int = 5, state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        self._raise_sync_unavailable("search_products")

    async def asearch_products(self, query: str, limit: int = 5, state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        config = await self._aget_config(state)
        self._ensure_shopify_config(config, state)
        shop_url = config.get("shop_url")
        website_url = await self._aget_website_url(state)

        result = await ashopify_graphql_product_search(
            title_query=query,
            access_token=config.get("access_token"),
            shop_url=shop_url,
            api_version="2024-04",
            first=limit,
            website_url=website_url,
        )

        products = result.get("products", [])
        self._ensure_urls(products, shop_url, website_url)
        return products

    async def asearch_products_by_name(
        self,
        product_name: str,
        limit: int = 3,
        state: Optional[Dict] = None,
        product_line: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        parsed_product_line = None
        if product_line:
            parsed_product_line = re.sub(r"\s+", " ", product_line.lower().strip())
            log_with_trace_id(state, f"🎯 Using LLM-provided product_line: '{parsed_product_line}'")

        log_with_trace_id(state, f"🔍 Upstash Search: querying '{product_name}'...")
        upstash_results = await self._aupstash_search_products(
            product_name,
            limit,
            state=state,
            product_line=parsed_product_line,
        )
        website_url = await self._aget_website_url(state)
        if upstash_results:
            config = await self._aget_config(state)
            shop_url = config.get("shop_url") if isinstance(config, dict) else ""
            log_with_trace_id(state, f"✅ Upstash Search returned {len(upstash_results)} products")
            self._ensure_urls(upstash_results, shop_url, website_url)
            return upstash_results

        log_with_trace_id(state, "🔄 Upstash Search returned 0 results, falling back to Shopify search...")
        config = await self._aget_config(state)
        self._ensure_shopify_config(config, state)
        shop_url = config.get("shop_url")
        access_token = config.get("access_token")

        synonym_map = {
            "jeans": "denim",
            "denim": "jeans",
        }
        synonym_list_map = {
            "jeans": ["jeans", "denim"],
            "denim": ["denim", "jeans"],
        }

        matches: List[Dict[str, Any]] = []

        # 1. Exact search first
        search_res = await ashopify_graphql_product_search(
            title_query=product_name,
            access_token=access_token,
            shop_url=shop_url,
            api_version="2024-04",
            first=limit,
            website_url=website_url,
        )
        matches = search_res.get("products", [])

        if matches:
            user_query_normalized = product_name.strip().lower()
            exact_matches = []
            for product in matches:
                product_title = product.get("name") or product.get("title", "")
                if product_title.strip().lower() == user_query_normalized:
                    exact_matches.append(product)
            if exact_matches:
                matches = exact_matches
                log_with_trace_id(state, f"🎯 Found {len(exact_matches)} exact matches")
            if parsed_product_line:
                matches = self._post_filter_product_line(matches, parsed_product_line, state)

        # 2. Synonym search if no matches
        if len(matches) == 0:
            log_with_trace_id(state, f"🔄 No exact match for '{product_name}', trying synonyms...")
            words = product_name.lower().split()
            for word in words:
                if word in synonym_map:
                    new_words = [synonym_map[word] if w == word else w for w in words]
                    synonym_query = " ".join(new_words)
                    synonym_search = await ashopify_graphql_product_search(
                        title_query=synonym_query,
                        access_token=access_token,
                        shop_url=shop_url,
                        api_version="2024-04",
                        first=limit,
                        website_url=website_url,
                    )
                    synonym_matches = synonym_search.get("products", [])
                    if synonym_matches:
                        if parsed_product_line:
                            synonym_matches = self._post_filter_product_line(
                                synonym_matches,
                                parsed_product_line,
                                state,
                            )
                        log_with_trace_id(state, f"✅ Found {len(synonym_matches)} products with synonym")
                        matches = synonym_matches
                        break

        # 3. Fuzzy search if still no matches
        if len(matches) == 0:
            log_with_trace_id(state, f"🔍 No matches, trying fuzzy search...")
            words = product_name.split()
            fuzzy_matches: List[Dict[str, Any]] = []
            for word in words:
                if len(word) >= 3:
                    search_terms = [word.lower()]
                    if word.lower() in synonym_list_map:
                        search_terms.extend(synonym_list_map[word.lower()])
                    for search_term in search_terms:
                        fuzzy_search = await ashopify_graphql_product_search(
                            title_query=search_term,
                            access_token=access_token,
                            shop_url=shop_url,
                            api_version="2024-04",
                            first=limit,
                            website_url=website_url,
                        )
                        fuzzy_matches.extend(fuzzy_search.get("products", []))

            seen_handles = set()
            unique_matches = []
            for product in fuzzy_matches:
                handle = product.get("handle")
                if handle and handle not in seen_handles:
                    seen_handles.add(handle)
                    unique_matches.append(product)
            matches = unique_matches[:limit]
            if parsed_product_line:
                matches = self._post_filter_product_line(matches, parsed_product_line, state)

        self._ensure_urls(matches, shop_url, website_url)
        return matches

    async def _aupstash_search_products(
        self,
        query: str,
        limit: int,
        state: Optional[Dict] = None,
        product_line: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Async wrapper around Upstash hybrid search."""
        try:
            client_id = self.client_id
            if not client_id and state:
                client_id = state.get("client_id")
            if not client_id:
                log_with_trace_id(state, "⚠️ Upstash Search: No client_id available", "warning")
                return []

            from fashion_bot.services.product_ingestion.upstash_search_service import get_upstash_search_service

            search_service = get_upstash_search_service()
            filter_parts = ["in_stock = true"]
            if product_line:
                filter_parts.append(f"product_line_normalized = '{product_line}'")
            filter_str = " AND ".join(filter_parts)

            results = await search_service.asearch(
                query=query,
                client_id=client_id,
                filter_str=filter_str,
                limit=limit,
                semantic_weight=0.7,
                reranking=True,
            )

            if not results and product_line:
                log_with_trace_id(
                    state,
                    f"🔄 Upstash Search: product_line '{product_line}' returned 0, retrying without it",
                )
                results = await search_service.asearch(
                    query=query,
                    client_id=client_id,
                    filter_str="in_stock = true",
                    limit=limit,
                    semantic_weight=0.7,
                    reranking=True,
                )

            if not results:
                return []

            products = [self._upstash_result_to_product(result) for result in results]
            log_with_trace_id(
                state,
                f"✅ Upstash Search returned {len(products)} products for '{query}' "
                f"(product_line_filter={product_line})",
            )
            return products
        except Exception as e:
            log_with_trace_id(state, f"❌ Upstash Search error: {e}", "error")
            logger.exception(f"Upstash Search failed: {e}")
            return []

    @staticmethod
    def _upstash_result_to_product(result: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a single Upstash Search result to Shopify-compatible product dict."""
        content = result.get("content", {})
        metadata = result.get("metadata", {})
        return {
            "id": metadata.get("product_id", ""),
            "handle": metadata.get("handle", ""),
            "name": content.get("title", ""),
            "title": content.get("title", ""),
            "product_type": content.get("category", ""),
            "vendor": content.get("brand", ""),
            "url": metadata.get("product_url", ""),
            "image_url": metadata.get("image_url", ""),
            "all_images": metadata.get("all_images", []),
            "price_min": content.get("price_min", 0),
            "price_max": content.get("price_max", 0),
            "compare_at_price_min": content.get("compare_at_price_min"),
            "compare_at_price_max": content.get("compare_at_price_max"),
            "in_stock": content.get("in_stock", False),
            "total_inventory": content.get("total_inventory", 0),
            "colors": content.get("colors", []),
            "sizes": content.get("sizes", []),
            "tags": content.get("tags", []),
            "description": content.get("description"),
            "fabric": content.get("material"),
            "care_instructions": metadata.get("care_instructions"),
            "fit_type": content.get("fit", ""),
            "size_chart": metadata.get("size_chart"),
            "all_metafields": metadata.get("all_metafields", []),
            "seo_title": metadata.get("seo_title"),
            "seo_description": metadata.get("seo_description"),
            "variants": metadata.get("variants", []),
            "base_product_name": content.get("base_product_name", ""),
            "product_line": content.get("product_line", ""),
            "extracted_color": "",
            "material": content.get("material", ""),
            "_search_score": result.get("score", 0),
            "_source": "upstash_search",
        }

    @staticmethod
    def _ensure_urls(
        products: List[Dict[str, Any]],
        shop_url: str,
        website_url: Optional[str] = None,
    ) -> None:
        """Ensure each product has a canonical customer-facing URL.

        Prefers the client's ``website_url`` (e.g. https://groovee.in) when
        building a missing URL, and rewrites any existing ``*.myshopify.com``
        URL bubbling up from lower layers so the internal staging domain never
        reaches the customer. Falls back to the shop domain only when no
        website URL is configured.
        """
        for product in products:
            existing = product.get("url")
            if existing:
                if website_url:
                    product["url"] = replace_shopify_url(existing, website_url)
                continue
            handle = product.get("handle")
            if not handle:
                continue
            if website_url:
                product["url"] = f"{website_url.rstrip('/')}/products/{handle}"
            elif shop_url:
                product["url"] = f"https://{shop_url}/products/{handle}"
    
    def get_top_selling_products(self, limit: int = 5, state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        self._raise_sync_unavailable("get_top_selling_products")

    async def aget_top_selling_products(self, limit: int = 5, state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        try:
            log_with_trace_id(state, f"ShopifyAdapter: Async fetching top {limit} selling products")
            config = await self._aget_config(state)
            shop_url = config.get("shop_url")
            website_url = await self._aget_website_url(state)

            result = await ashopify_get_top_selling_products_rest(
                top_n=limit,
                access_token=config.get("access_token"),
                shop_url=shop_url,
                api_version="2024-04",
                website_url=website_url,
            )

            if result.get("success"):
                products = result.get("products", [])
                self._ensure_urls(products, shop_url, website_url)
                log_with_trace_id(state, f"✅ Successfully retrieved {len(products)} top selling products")
                return products

            log_with_trace_id(state, f"❌ REST API sales analysis failed: {result.get('error')}", "error")
            return []

        except Exception as e:
            log_with_trace_id(state, f"Error in aget_top_selling_products: {str(e)}", "error")
            return []
