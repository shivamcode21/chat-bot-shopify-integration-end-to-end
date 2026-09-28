"""
Product Source Factory - Factory to determine the appropriate product source.

Determines whether to use Shopify Admin API or fallback to /products.json
based on client configuration.
"""

import logging
from typing import Optional

from fashion_bot.services.product_ingestion.base_product_service import BaseProductService
from fashion_bot.services.product_ingestion.models import ShopifyConfig
from fashion_bot.services.product_ingestion.shopify_product_service import ShopifyProductService
from fashion_bot.services.product_ingestion.products_json_service import ProductsJsonService

logger = logging.getLogger(__name__)


class ProductSourceFactory:
    """
    Factory to determine the appropriate product source.
    
    Priority:
    1. If source="shopify" and credentials exist -> ShopifyProductService
    2. If source="json" or no Shopify -> ProductsJsonService
    3. If source="auto" -> Try Shopify first, fallback to JSON
    """
    
    def __init__(self):
        """Initialize the factory."""
        pass
    
    async def aget_service(
        self,
        client_id: str,
        source: str = "auto"
    ) -> BaseProductService:
        """Async version of get_service — uses async postgres."""
        if source in ("shopify", "auto"):
            shopify_config = await self._aget_shopify_config(client_id)
            if shopify_config:
                website_url = await self._aget_website_url(client_id)
                logger.info(f"📦 Using ShopifyProductService for client {client_id} (async)")
                return ShopifyProductService(shopify_config, website_base_url=website_url)
            elif source == "shopify":
                raise ValueError(
                    f"Shopify credentials not found for client {client_id}. "
                    "Please configure Shopify in client settings."
                )

        if source in ("json", "auto"):
            website_url = await self._aget_website_url(client_id)
            if website_url:
                logger.info(f"📦 Using ProductsJsonService for client {client_id} (async)")
                return ProductsJsonService(website_url)

        raise ValueError(
            f"No valid product source found for client {client_id}. "
            "Please configure either Shopify credentials or website URL."
        )
    
    async def _aget_shopify_config(self, client_id: str) -> Optional[ShopifyConfig]:
        """Async version of _get_shopify_config."""
        try:
            from fashion_bot.database_manager import get_async_postgres_connection
            import json as _json

            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT config_value FROM client_configs "
                        "WHERE client_id = %s AND config_key = 'shopify_details'",
                        (client_id,),
                    )
                    result = await cur.fetchone()

            if not result:
                logger.info(f"No Shopify config found for client {client_id}")
                return None

            data = result["config_value"]
            if isinstance(data, str):
                data = _json.loads(data)

            access_token = data.get("access_token") or data.get("SHOPIFY_TOKEN", "")
            shop_url = data.get("shop_url") or data.get("SHOPIFY_DOMAIN", "")

            if access_token and shop_url:
                return ShopifyConfig(
                    shop_domain=shop_url,
                    access_token=access_token,
                    api_version=data.get("api_version", "2024-04"),
                )

            return None

        except Exception as e:
            logger.warning(f"⚠️ Error getting async Shopify config for {client_id}: {e}")
            return None

    async def _aget_website_url(self, client_id: str) -> Optional[str]:
        """Async version of _get_website_url."""
        try:
            from fashion_bot.database_manager import get_async_postgres_connection
            import json as _json

            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT config_value FROM client_configs "
                        "WHERE client_id = %s AND config_key = 'website_urls'",
                        (client_id,),
                    )
                    result = await cur.fetchone()

            if not result:
                return None

            website_config = result["config_value"]
            if isinstance(website_config, str):
                website_config = _json.loads(website_config)

            if isinstance(website_config, dict):
                return (
                    website_config.get("website_url")
                    or website_config.get("website")
                    or website_config.get("home")
                    or website_config.get("base_url")
                )
            elif isinstance(website_config, str):
                return website_config

            return None

        except Exception as e:
            logger.warning(f"⚠️ Error getting async website URL for {client_id}: {e}")
            return None
