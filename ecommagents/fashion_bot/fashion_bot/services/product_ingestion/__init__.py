"""
Product Ingestion Service - Ingest products to Upstash Search for product discovery.

This module provides:
- ProductIngestionOrchestrator: Main orchestrator for ingestion pipeline (uses Upstash Search)
- ShopifyProductService: Fetch products from Shopify Admin API
- ProductsJsonService: Fallback to fetch from /products.json endpoint
- UpstashSearchService: Handle Upstash Search operations
- ProductSourceFactory: Factory to determine product source
- ProductAttributeExtractor: LLM-based extraction of structured product attributes
"""

from fashion_bot.services.product_ingestion.orchestrator import ProductIngestionOrchestrator
from fashion_bot.services.product_ingestion.upstash_search_service import UpstashSearchService
from fashion_bot.services.product_ingestion.shopify_product_service import ShopifyProductService
from fashion_bot.services.product_ingestion.products_json_service import ProductsJsonService
from fashion_bot.services.product_ingestion.product_source_factory import ProductSourceFactory
from fashion_bot.services.product_ingestion.product_attribute_extractor import (
    ProductAttributeExtractor,
    ExtractedAttributes,
    get_product_attribute_extractor,
)
from fashion_bot.services.product_ingestion.models import (
    ShopifyConfig,
    IngestionResult,
    ProductIngestionRequest,
    ProductIngestionResponse,
    IngestionStatusResponse,
    DeltaSyncResult,
    DeltaSyncResponse,
    RecommendationRequest,
    RecommendationResponse,
    RecommendedProduct,
)

__all__ = [
    "ProductIngestionOrchestrator",
    "UpstashSearchService",
    "ShopifyProductService",
    "ProductsJsonService",
    "ProductSourceFactory",
    # LLM attribute extraction
    "ProductAttributeExtractor",
    "ExtractedAttributes",
    "get_product_attribute_extractor",
    # Models
    "ShopifyConfig",
    "IngestionResult",
    "ProductIngestionRequest",
    "ProductIngestionResponse",
    "IngestionStatusResponse",
    "DeltaSyncResult",
    "DeltaSyncResponse",
    "RecommendationRequest",
    "RecommendationResponse",
    "RecommendedProduct",
]
