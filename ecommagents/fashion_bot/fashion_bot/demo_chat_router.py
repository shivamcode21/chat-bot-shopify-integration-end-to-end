"""
Demo Chat Router - Enhanced with Shopify Runtime Data Fetching

This router provides intelligent product assistance by:
1. Detecting user intent (product search, pricing, availability, policies)
2. Fetching real-time data from Shopify public APIs
3. Injecting fresh data into LLM for accurate responses
4. Semantic search via Upstash Vector for better product recommendations

Works on ANY Shopify store without backend integration!

Usage:
    from fashion_bot.demo_chat_router import router as demo_router
    app.include_router(demo_router, prefix="/demo", tags=["Demo Chat"])
"""

import os
import sys
import json
import logging
import re
import asyncio
import hashlib
import httpx
import time
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta
from functools import lru_cache
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from openai import OpenAI, AsyncOpenAI

from fashion_bot.utils.client_location import extract_client_ip, resolve_client_location_for_demo

_LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-4o")
_SMALLER_LLM_MODEL = os.getenv("SMALLER_LLM_MODEL", "openai/gpt-4o-mini")


# ==================== DEBUG UTILITY ====================

class DebugLogger:
    """
    Centralized debug logging for demo chat.
    Set DEBUG_MODE=true env var to enable verbose console output.
    """
    
    # Control debug output via environment variable
    DEBUG_MODE = True
    
    @classmethod
    def enable(cls):
        """Enable debug mode at runtime."""
        cls.DEBUG_MODE = True
        print("🔧 Debug mode ENABLED for demo_chat_router", flush=True)
    
    @classmethod
    def disable(cls):
        """Disable debug mode at runtime."""
        cls.DEBUG_MODE = False
        print("🔧 Debug mode DISABLED for demo_chat_router", flush=True)
    
    @classmethod
    def log(cls, title: str, content: str = "", max_length: int = 5000):
        """Log debug info with formatted output."""
        if not cls.DEBUG_MODE:
            return
        
        print("\n" + "="*80, flush=True)
        print(f"🔍 {title}", flush=True)
        print("="*80, flush=True)
        
        if content:
            if len(content) > max_length:
                print(content[:max_length], flush=True)
                print(f"\n... (truncated, showing first {max_length} of {len(content)} chars) ...", flush=True)
            else:
                print(content, flush=True)
        
        print("="*80 + "\n", flush=True)
        sys.stdout.flush()
    
    @classmethod
    def log_request(cls, message: str, context: Any, shopify_data: Dict = None):
        """Log incoming chat request details."""
        if not cls.DEBUG_MODE:
            return
        
        lines = [
            f"Message: {message}",
            f"Domain: {getattr(context, 'domain', 'N/A')}",
            f"URL: {getattr(context, 'url', 'N/A')[:80]}...",
            f"Product: {context.product.name if hasattr(context, 'product') and context.product else 'None'}",
            f"Page text length: {len(context.pageText) if hasattr(context, 'pageText') and context.pageText else 0}",
            f"Shopify data keys: {list(shopify_data.keys()) if shopify_data else 'None'}"
        ]
        cls.log("CHAT REQUEST", "\n".join(lines))
    
    @classmethod
    def log_llm_call(cls, system_prompt: str, messages: List[Dict], user_message: str):
        """Log LLM API call details."""
        if not cls.DEBUG_MODE:
            return
        
        lines = [f"User message: {user_message}", f"System prompt length: {len(system_prompt)} chars", ""]
        
        for i, msg in enumerate(messages):
            role = msg["role"]
            content = msg["content"]
            lines.append(f"[{i+1}] Role: {role} | Length: {len(content)} chars")
            if role != "system":
                lines.append(f"    Content: {content[:200]}...")
        
        cls.log("LLM API CALL", "\n".join(lines))
    
    @classmethod
    def log_page_text(cls, page_text: str):
        """Log page text being sent to LLM."""
        if not cls.DEBUG_MODE:
            return
        
        cls.log("PAGE TEXT FOR LLM", page_text, max_length=5000)
        
        # Also show last portion if truncated
        if len(page_text) > 5000:
            cls.log("PAGE TEXT (LAST 1000 CHARS)", page_text[-1000:], max_length=1000)

# Import Upstash Vector Service for semantic search (legacy)
try:
    from fashion_bot.services.product_ingestion.upstash_vector_service import UpstashVectorService
    from fashion_bot.services.product_ingestion.models import VectorData
    VECTOR_SERVICE_AVAILABLE = True
except ImportError:
    VECTOR_SERVICE_AVAILABLE = False

# Import Upstash Search pipeline (primary search backend)
UPSTASH_SEARCH_AVAILABLE = False
try:
    from fashion_bot.services.recommendation.recommendation_service import search_products_pipeline
    from fashion_bot.utils.product_utils import format_products_for_llm, match_products_to_reply
    UPSTASH_SEARCH_AVAILABLE = True
except ImportError as _e:
    logging.getLogger(__name__).warning(f"Upstash Search modules not available: {_e}")

logger = logging.getLogger("demo_chat")

router = APIRouter()

# ==================== CACHE ====================

class SimpleCache:
    """Simple in-memory cache with TTL"""
    def __init__(self):
        self._cache: Dict[str, Tuple[Any, datetime]] = {}
        self._ttl = timedelta(seconds=120)  # 2 minute cache
    
    def get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            value, timestamp = self._cache[key]
            if datetime.now() - timestamp < self._ttl:
                return value
            del self._cache[key]
        return None
    
    def set(self, key: str, value: Any):
        self._cache[key] = (value, datetime.now())
    
    def clear(self):
        self._cache.clear()

cache = SimpleCache()

# ==================== VECTOR SERVICE ====================

# Initialize vector service (singleton)
_vector_service: Optional[UpstashVectorService] = None

def get_vector_service() -> Optional[UpstashVectorService]:
    """Get or create vector service singleton."""
    global _vector_service
    if not VECTOR_SERVICE_AVAILABLE:
        logger.warning("Vector service not available - upstash_vector package not installed")
        return None
    
    if _vector_service is None:
        try:
            _vector_service = UpstashVectorService()
            logger.info("✅ Vector service initialized successfully")
        except Exception as e:
            logger.warning(f"⚠️ Failed to initialize vector service: {e}")
            return None

    return _vector_service

# In-memory tracking of ingested domains (cleared on restart)
# Format: {"domain_hash": {"ingested_at": datetime, "product_count": int}}
INGESTED_DOMAINS: Dict[str, Dict[str, Any]] = {}

def get_client_id_for_domain(domain: str) -> str:
    """
    Generate deterministic client_id from domain using SHA256 hash.
    
    Uses first 16 characters of hex hash for readability while maintaining uniqueness.
    Same domain always produces same client_id (no persistence needed).
    
    Args:
        domain: Store domain (e.g., "store.myshopify.com")
        
    Returns:
        16-character hex string client_id
    """
    normalized = _normalize_domain(domain)
    
    # Generate SHA256 hash and take first 16 characters
    hash_object = hashlib.sha256(normalized.encode('utf-8'))
    client_id = hash_object.hexdigest()[:16]
    
    logger.debug(f"🔑 Generated client_id for {domain}: {client_id}")
    return client_id


def _normalize_domain(domain: str) -> str:
    """Canonical bare domain: lowercase, no scheme, no www, no trailing slash."""
    d = domain.lower().replace("https://", "").replace("http://", "")
    d = d.replace("www.", "").rstrip("/")
    return d


async def aget_pg_client_id_for_domain(domain: str) -> Optional[str]:
    """Resolve website domain to client_id via unified tiered cache."""
    from fashion_bot.utils.client_identity_cache import aget_client_id_by_website_domain
    return await aget_client_id_by_website_domain(domain)


# Common color keywords for extraction
COLOR_KEYWORDS = {
    'black', 'white', 'red', 'blue', 'green', 'yellow', 'pink', 'grey', 'gray', 
    'brown', 'navy', 'beige', 'cream', 'orange', 'purple', 'maroon', 'olive', 
    'teal', 'burgundy', 'gold', 'silver', 'coral', 'mint', 'lavender', 'indigo',
    'charcoal', 'khaki', 'tan', 'ivory', 'peach', 'rose', 'wine', 'mustard',
    'sage', 'rust', 'plum', 'aqua', 'turquoise', 'magenta', 'crimson', 'violet'
}


def extract_color_from_text(text: str) -> Optional[str]:
    """
    Extract color from product title, URL, or description.
    
    Args:
        text: Text to extract color from (e.g., product name, URL)
        
    Returns:
        Color string if found, None otherwise
    """
    if not text:
        return None
    
    text_lower = text.lower()
    
    # Check for color keywords
    for color in COLOR_KEYWORDS:
        # Use word boundary matching to avoid partial matches
        # e.g., "black" should match but not "blackberry" 
        pattern = rf'\b{color}\b'
        if re.search(pattern, text_lower):
            return color
    
    return None


def extract_color_from_context(context: 'PageContext') -> Optional[str]:
    """
    Extract color from page context (product name, URL, description).
    
    Args:
        context: Page context with product info
        
    Returns:
        Color string if found, None otherwise
    """
    # Try product name first
    if context.product and context.product.name:
        color = extract_color_from_text(context.product.name)
        if color:
            return color
    
    # Try URL (often contains color like "jerdo-mens-shirt-black")
    if context.url:
        color = extract_color_from_text(context.url)
        if color:
            return color
    
    # Try page title
    if context.title:
        color = extract_color_from_text(context.title)
        if color:
            return color
    
    # Try product attributes
    if context.product and context.product.attributes:
        color_attr = context.product.attributes.get('color') or context.product.attributes.get('Color')
        if color_attr:
            return color_attr.lower()
    
    return None


def extract_colors_and_sizes(product: Dict) -> Tuple[List[str], List[str]]:
    """
    Extract colors and sizes from product variants.
    
    Shopify stores these as option1, option2, option3 values.
    Typically: option1=Size, option2=Color, but order varies.
    
    Returns:
        Tuple of (colors list, sizes list)
    """
    colors = set()
    sizes = set()
    
    # Common size patterns
    size_patterns = {'XS', 'S', 'M', 'L', 'XL', 'XXL', 'XXXL', '2XL', '3XL', '4XL', '5XL',
                     '26', '28', '30', '32', '34', '36', '38', '40', '42', '44', '46', '48',
                     'SMALL', 'MEDIUM', 'LARGE', 'EXTRA LARGE', 'FREE SIZE', 'FREE', 'ONE SIZE'}
    
    # Common color keywords
    color_keywords = {'black', 'white', 'red', 'blue', 'green', 'yellow', 'pink', 'grey',
                      'gray', 'brown', 'navy', 'beige', 'cream', 'orange', 'purple', 'maroon',
                      'olive', 'teal', 'burgundy', 'gold', 'silver', 'coral', 'mint', 'lavender'}
    
    variants = product.get("variants", [])
    for variant in variants:
        for opt_key in ['option1', 'option2', 'option3']:
            opt_value = variant.get(opt_key, '')
            if not opt_value or opt_value.lower() == 'default title':
                continue
            
            opt_upper = opt_value.upper().strip()
            opt_lower = opt_value.lower().strip()
            
            # Check if it's a size
            if opt_upper in size_patterns or opt_upper.replace(' ', '') in size_patterns:
                sizes.add(opt_value.strip())
            # Check if it's a color
            elif any(color in opt_lower for color in color_keywords):
                colors.add(opt_value.strip())
            # Heuristic: if short (1-4 chars) and alphanumeric, likely size
            elif len(opt_value.strip()) <= 4 and opt_value.strip().isalnum():
                sizes.add(opt_value.strip())
            # Otherwise, assume color
            else:
                colors.add(opt_value.strip())
    
    return list(colors), list(sizes)

def build_searchable_text(product: Dict, extracted_attrs: Optional[Dict] = None) -> str:
    """
    Build rich searchable text for embedding.
    
    Uses LLM-extracted structured attributes when available for better search accuracy.
    Falls back to raw product fields when extracted_attrs is not provided.
    
    Args:
        product: Shopify product dict
        extracted_attrs: Optional dict with LLM-extracted attributes
            (base_product_name, product_line, color, material, size)
        
    Returns:
        Searchable text string optimized for embedding
    """
    parts = []
    
    # Use structured fields if available (from LLM extraction)
    if extracted_attrs and extracted_attrs.get("base_product_name"):
        parts.append(f"Product: {extracted_attrs['base_product_name']}")
        if extracted_attrs.get("product_line"):
            parts.append(f"Model: {extracted_attrs['product_line']}")
        if extracted_attrs.get("color"):
            parts.append(f"Color: {extracted_attrs['color']}")
        if extracted_attrs.get("material"):
            parts.append(f"Material: {extracted_attrs['material']}")
    else:
        # Fallback: use raw title
        title = product.get("title", "")
        if title:
            parts.append(f"Product: {title}")
    
    # Product type
    product_type = product.get("product_type", "")
    if product_type and product_type.lower() not in ['variable', 'simple', '']:
        parts.append(f"Category: {product_type}")
    
    # Vendor/Brand
    vendor = product.get("vendor", "")
    if vendor:
        parts.append(f"Brand: {vendor}")
    
    # Tags
    tags = product.get("tags", [])
    if tags:
        # Handle both string and list formats
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(',')]
        meaningful_tags = [t for t in tags if len(t) > 2 and not t.isdigit()]
        if meaningful_tags:
            parts.append(f"Tags: {', '.join(meaningful_tags[:10])}")
    
    # Colors and Sizes
    colors, sizes = extract_colors_and_sizes(product)
    if colors:
        parts.append(f"Colors: {', '.join(colors[:10])}")
    if sizes:
        parts.append(f"Sizes: {', '.join(sizes[:10])}")
    
    # Price info
    variants = product.get("variants", [])
    if variants:
        prices = [float(v.get("price", 0)) for v in variants if v.get("price")]
        if prices:
            min_price = min(prices)
            max_price = max(prices)
            if min_price == max_price:
                parts.append(f"Price: Rs {int(min_price)}")
            else:
                parts.append(f"Price range: Rs {int(min_price)} to Rs {int(max_price)}")
    
    # Description (truncated)
    description = product.get("body_html", "")
    if description:
        # Strip HTML tags
        clean_desc = re.sub(r'<[^>]+>', ' ', description)
        clean_desc = re.sub(r'\s+', ' ', clean_desc).strip()
        if clean_desc:
            parts.append(f"Description: {clean_desc[:500]}")
    
    return " | ".join(parts)


def extract_segment_from_product(product: Dict) -> Optional[str]:
    """
    Extract segment (men/women/kids) from product data.
    
    Checks title, tags, and product_type for segment indicators.
    
    Args:
        product: Shopify product dict
        
    Returns:
        Segment string ('men', 'women', 'kids') or None
    """
    # Segment patterns
    segment_patterns = {
        'men': [r'\bmen\'?s?\b', r'\bmale\b', r'\bgents?\b', r'\bboy\'?s?\b'],
        'women': [r'\bwomen\'?s?\b', r'\bwoman\'?s?\b', r'\bfemale\b', r'\bladies\b', r'\bgirl\'?s?\b'],
        'kids': [r'\bkids?\b', r'\bchildren\'?s?\b', r'\bchild\'?s?\b', r'\bjunior\b', r'\binfant\b', r'\btoddler\b']
    }
    
    def check_text(text: str) -> Optional[str]:
        if not text:
            return None
        text_lower = text.lower()
        for segment, patterns in segment_patterns.items():
            for pattern in patterns:
                if re.search(pattern, text_lower):
                    return segment
        return None
    
    # Check title
    title = product.get("title", "")
    segment = check_text(title)
    if segment:
        return segment
    
    # Check product_type
    product_type = product.get("product_type", "")
    segment = check_text(product_type)
    if segment:
        return segment
    
    # Check tags
    tags = product.get("tags", [])
    if isinstance(tags, list):
        for tag in tags:
            segment = check_text(tag)
            if segment:
                return segment
    
    return None


def product_to_vector_data(
    product: Dict,
    client_id: str,
    store_url: str,
    extracted_attrs: Optional[Dict] = None,
) -> VectorData:
    """
    Convert Shopify product to VectorData for upserting.
    
    Args:
        product: Shopify product dict
        client_id: Generated client ID for the domain
        store_url: Full store URL for product links
        extracted_attrs: Optional LLM-extracted attributes dict with keys:
            base_product_name, product_line, product_line_normalized, color, size, material
        
    Returns:
        VectorData object ready for upsert
    """
    product_id = str(product.get("id", ""))
    handle = product.get("handle", "")
    
    # Build searchable text (with extracted attributes if available)
    searchable_text = build_searchable_text(product, extracted_attrs)
    
    # Extract price range
    variants = product.get("variants", [])
    prices = [float(v.get("price", 0)) for v in variants if v.get("price")]
    price_min = min(prices) if prices else 0
    price_max = max(prices) if prices else 0
    
    # Compare at prices (for discounts)
    compare_prices = [float(v.get("compare_at_price", 0)) for v in variants if v.get("compare_at_price")]
    compare_at_price_min = min(compare_prices) if compare_prices else None
    compare_at_price_max = max(compare_prices) if compare_prices else None
    
    # Images
    images = product.get("images", [])
    image_url = images[0].get("src", "") if images else ""
    all_images = [img.get("src", "") for img in images[:5]]  # Limit to 5 images
    
    # Colors and sizes
    colors, sizes = extract_colors_and_sizes(product)
    
    # In stock check
    in_stock = any(v.get("available", True) for v in variants) if variants else True
    total_inventory = sum(v.get("inventory_quantity", 0) for v in variants if v.get("inventory_quantity"))
    
    # Variant info for metadata
    variant_info = []
    for v in variants[:10]:  # Limit variants
        variant_info.append({
            "id": v.get("id"),
            "title": v.get("title", ""),
            "price": v.get("price", ""),
            "available": v.get("available", True)
        })
    
    # Extract segment (men/women/kids)
    segment = extract_segment_from_product(product) or ""
    
    # Build metadata
    metadata = {
        "client_id": client_id,
        "product_id": product_id,
        "title": product.get("title", ""),
        "handle": handle,
        "product_type": product.get("product_type", ""),
        "segment": segment,  # Added: men, women, kids, or empty
        "vendor": product.get("vendor", ""),
        "price_min": price_min,
        "price_max": price_max,
        "compare_at_price_min": compare_at_price_min,
        "compare_at_price_max": compare_at_price_max,
        "image_url": image_url,
        "all_images": all_images,
        "product_url": f"{store_url}/products/{handle}",
        "in_stock": in_stock,
        "total_inventory": total_inventory,
        "colors": colors,
        "sizes": sizes,
        "tags": product.get("tags", [])[:20],  # Limit tags
        "variants": variant_info,
        "created_at": product.get("created_at", ""),
        "updated_at": product.get("updated_at", ""),
        # LLM-extracted structured attributes for search filtering
        "base_product_name": (extracted_attrs or {}).get("base_product_name", ""),
        "product_line": (extracted_attrs or {}).get("product_line", ""),
        "product_line_normalized": (extracted_attrs or {}).get("product_line_normalized", ""),
        "extracted_color": (extracted_attrs or {}).get("color", ""),
        "material": (extracted_attrs or {}).get("material", ""),
    }
    
    # Composite ID: client_id + product_id
    vector_id = f"{client_id}_{product_id}"
    
    return VectorData(
        id=vector_id,
        searchable_text=searchable_text,
        metadata=metadata
    )

def extract_price_filters(message: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Extract price filters from user message.
    
    Handles patterns like:
    - "under 2000", "below 2000", "less than 2000" -> max_price=2000
    - "above 1000", "over 1000", "more than 1000" -> min_price=1000
    - "between 1000 and 2000", "1000 to 2000" -> min_price=1000, max_price=2000
    - "around 1500", "about 1500" -> min_price=1000, max_price=2000 (±30%)
    
    Args:
        message: User message
        
    Returns:
        Tuple of (min_price, max_price) - None if not specified
    """
    message_lower = message.lower()
    
    # Pattern: under/below/less than X
    under_match = re.search(r'(?:under|below|less than|upto|up to|max|within)\s*(?:rs\.?|₹|inr|rupees?)?\s*(\d+(?:,\d+)?)', message_lower)
    if under_match:
        max_price = float(under_match.group(1).replace(',', ''))
        return None, max_price
    
    # Pattern: above/over/more than X
    above_match = re.search(r'(?:above|over|more than|min|minimum|starting)\s*(?:rs\.?|₹|inr|rupees?)?\s*(\d+(?:,\d+)?)', message_lower)
    if above_match:
        min_price = float(above_match.group(1).replace(',', ''))
        return min_price, None
    
    # Pattern: between X and Y, X to Y
    range_match = re.search(r'(?:between|from)?\s*(?:rs\.?|₹|inr|rupees?)?\s*(\d+(?:,\d+)?)\s*(?:to|and|-)\s*(?:rs\.?|₹|inr|rupees?)?\s*(\d+(?:,\d+)?)', message_lower)
    if range_match:
        min_price = float(range_match.group(1).replace(',', ''))
        max_price = float(range_match.group(2).replace(',', ''))
        return min_price, max_price
    
    # Pattern: around/about X (±30%)
    around_match = re.search(r'(?:around|about|approximately|approx)\s*(?:rs\.?|₹|inr|rupees?)?\s*(\d+(?:,\d+)?)', message_lower)
    if around_match:
        price = float(around_match.group(1).replace(',', ''))
        return price * 0.7, price * 1.3
    
    return None, None


def extract_segment_from_context(context: 'PageContext') -> Optional[str]:
    """
    Extract segment (men/women/kids) from product context.
    
    Tries to detect segment from:
    1. Product name (e.g., "Men's Shirt", "Women's Dress")
    2. URL path (e.g., "/collections/mens-shirts", "/products/womens-top")
    3. Structured data (product type, category)
    
    Args:
        context: PageContext with product info, URL, and structured data
        
    Returns:
        Segment string ('men', 'women', 'kids') or None if not detected
    """
    # Segment keywords to look for
    segment_patterns = {
        'men': [r'\bmen\'?s?\b', r'\bmale\b', r'\bgents?\b', r'\bboy\'?s?\b'],
        'women': [r'\bwomen\'?s?\b', r'\bwoman\'?s?\b', r'\bfemale\b', r'\bladies\b', r'\bgirl\'?s?\b'],
        'kids': [r'\bkids?\b', r'\bchildren\'?s?\b', r'\bchild\'?s?\b', r'\bjunior\b', r'\binfant\b', r'\btoddler\b']
    }
    
    def check_text_for_segment(text: str) -> Optional[str]:
        """Check text for segment patterns."""
        if not text:
            return None
        text_lower = text.lower()
        for segment, patterns in segment_patterns.items():
            for pattern in patterns:
                if re.search(pattern, text_lower):
                    return segment
        return None
    
    # 1. Check product name
    if context.product and context.product.name:
        segment = check_text_for_segment(context.product.name)
        if segment:
            logger.debug(f"Segment '{segment}' detected from product name: {context.product.name}")
            return segment
    
    # 2. Check URL
    if context.url:
        segment = check_text_for_segment(context.url)
        if segment:
            logger.debug(f"Segment '{segment}' detected from URL: {context.url}")
            return segment
    
    # 3. Check structured data
    if context.structuredData:
        for data in context.structuredData:
            if isinstance(data, dict):
                # Check product type/category
                for field in ['category', 'productType', 'product_type', 'name', 'description']:
                    if field in data:
                        segment = check_text_for_segment(str(data[field]))
                        if segment:
                            logger.debug(f"Segment '{segment}' detected from structuredData.{field}")
                            return segment
    
    # 4. Check page title
    if context.title:
        segment = check_text_for_segment(context.title)
        if segment:
            logger.debug(f"Segment '{segment}' detected from page title: {context.title}")
            return segment
    
    return None

async def vector_search_products(
    domain: str,
    query: str,
    top_k: int = 10,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    segment: Optional[str] = None,
    in_stock_only: bool = False
) -> Optional[List[Dict[str, Any]]]:
    """
    Search products using Upstash Vector semantic search.
    
    Automatically parses the query to extract product_line (device model)
    for exact-match filtering. Color and material are handled semantically.
    
    When no product_line is detected (e.g., "show me leather cases"), returns
    top results by vector score across all product lines.
    
    Args:
        domain: Store domain
        query: Search query (user's message)
        top_k: Number of results to return
        min_price: Optional minimum price filter
        max_price: Optional maximum price filter
        segment: Optional segment filter (men, women, kids)
        in_stock_only: Whether to only return in-stock products
        
    Returns:
        List of search results or None if search fails
    """
    vector_service = get_vector_service()
    if not vector_service:
        logger.warning("Vector service not available for search")
        return None
    
    client_id = get_client_id_for_domain(domain)
    
    try:
        results = await vector_service.asearch(
            query=query,
            client_id=client_id,
            top_k=top_k,
            min_price=min_price,
            max_price=max_price,
            product_line=None,  # No query_parser — product_line extraction is handled by the LLM agent
            segment=segment,
            in_stock_only=in_stock_only
        )
        
        if results:
            logger.info(
                f"🔍 Vector search for '{query}' (segment={segment}) "
                f"returned {len(results)} results"
            )
            # Update INGESTED_DOMAINS if we got results (domain is indexed)
            if client_id not in INGESTED_DOMAINS:
                INGESTED_DOMAINS[client_id] = {
                    "domain": domain,
                    "discovered_at": datetime.now().isoformat(),
                    "product_count": len(results)  # Approximate
                }
        else:
            logger.info(f"🔍 Vector search for '{query}' returned no results (domain may not be ingested)")
        
        return results
        
    except Exception as e:
        logger.error(f"❌ Vector search failed: {e}")
        return None


async def ingest_products_to_vector_db(
    products: List[Dict],
    domain: str,
    store_url: str
) -> Dict[str, Any]:
    """
    Ingest products to Upstash Vector DB.
    
    Uses LLM (Gemini 2.5 Flash Lite) to extract structured product attributes
    (base_product_name, product_line, color, material) for accurate search filtering.
    
    Args:
        products: List of Shopify product dicts
        domain: Store domain
        store_url: Full store URL
        
    Returns:
        Ingestion result dict with success_count, failed_count, etc.
    """
    vector_service = get_vector_service()
    if not vector_service:
        return {"success": False, "error": "Vector service not available"}
    
    client_id = get_client_id_for_domain(domain)
    
    try:
        # Step 1: Extract structured attributes using LLM (batch)
        extracted_attrs_list = []
        try:
            from fashion_bot.services.product_ingestion.product_attribute_extractor import (
                get_product_attribute_extractor,
            )
            extractor = get_product_attribute_extractor()
            
            logger.info(f"🤖 Extracting product attributes using LLM for {len(products)} products...")
            
            # Use async batch extraction for performance
            raw_results = await extractor.extract_batch_async(products, max_concurrent=10)
            
            # Convert ExtractedAttributes to dicts for product_to_vector_data
            for attrs in raw_results:
                extracted_attrs_list.append({
                    "base_product_name": attrs.base_product_name,
                    "product_line": attrs.product_line,
                    "product_line_normalized": attrs.product_line_normalized,
                    "color": attrs.color,
                    "size": attrs.size,
                    "material": attrs.material,
                } if attrs.extraction_success else None)
            
            success_count = sum(1 for a in raw_results if a.extraction_success)
            logger.info(f"✅ LLM extraction complete: {success_count}/{len(products)} products extracted")
            
        except Exception as e:
            logger.warning(f"⚠️ LLM attribute extraction failed, proceeding without: {e}")
            extracted_attrs_list = [None] * len(products)
        
        # Step 2: Convert products to VectorData (with extracted attributes)
        vectors = []
        for i, product in enumerate(products):
            try:
                attrs = extracted_attrs_list[i] if i < len(extracted_attrs_list) else None
                vector_data = product_to_vector_data(product, client_id, store_url, attrs)
                vectors.append(vector_data)
            except Exception as e:
                logger.warning(f"Failed to convert product {product.get('id')}: {e}")
        
        if not vectors:
            return {"success": False, "error": "No valid products to ingest"}
        
        # Step 3: Upsert to Upstash (using client-specific namespace)
        result = vector_service.upsert_batch(vectors, client_id=client_id, batch_size=100)
        
        # Track ingested domain
        INGESTED_DOMAINS[client_id] = {
            "domain": domain,
            "ingested_at": datetime.now().isoformat(),
            "product_count": result["success_count"],
            "namespace": result.get("namespace", f"client_{client_id}")
        }
        
        logger.info(f"✅ Ingested {result['success_count']} products for {domain} (namespace: {result.get('namespace')})")
        
        return {
            "success": True,
            "client_id": client_id,
            "namespace": result.get("namespace"),
            "success_count": result["success_count"],
            "failed_count": len(result.get("failed", []))
        }
        
    except Exception as e:
        logger.error(f"❌ Ingestion failed for {domain}: {e}")
        return {"success": False, "error": str(e)}

# ==================== ORDER STORAGE ====================

class OrderStore:
    """In-memory order storage for demo purposes"""
    def __init__(self):
        self._orders: Dict[str, Dict] = {}
        self._pending_orders: Dict[str, Dict] = {}  # Session-based pending orders
    
    def create_pending_order(self, session_id: str, product_info: Dict) -> str:
        """Create a pending order waiting for customer details"""
        self._pending_orders[session_id] = {
            "product": product_info,
            "status": "awaiting_details",
            "created_at": datetime.now().isoformat()
        }
        return session_id
    
    def get_pending_order(self, session_id: str) -> Optional[Dict]:
        """Get pending order for a session"""
        return self._pending_orders.get(session_id)
    
    def complete_order(self, session_id: str, customer_info: Dict) -> Optional[Dict]:
        """Complete a pending order with customer details"""
        pending = self._pending_orders.pop(session_id, None)
        if not pending:
            return None
        
        # Generate order ID
        order_id = f"DEMO-{datetime.now().strftime('%Y%m%d%H%M%S')}-{len(self._orders) + 1:03d}"
        
        order = {
            "order_id": order_id,
            "product": pending["product"],
            "customer": customer_info,
            "status": "confirmed",
            "status_history": [
                {"status": "confirmed", "timestamp": datetime.now().isoformat(), "message": "Order confirmed"}
            ],
            "created_at": datetime.now().isoformat(),
            "estimated_delivery": (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
        }
        
        self._orders[order_id] = order
        # Also store by phone for lookup
        if customer_info.get("phone"):
            self._orders[f"phone:{customer_info['phone']}"] = order_id
        
        return order
    
    def get_order(self, order_id: str) -> Optional[Dict]:
        """Get order by ID"""
        return self._orders.get(order_id)
    
    def get_order_by_phone(self, phone: str) -> Optional[Dict]:
        """Get latest order by phone number"""
        order_id = self._orders.get(f"phone:{phone}")
        if order_id:
            return self._orders.get(order_id)
        return None
    
    def update_order_status(self, order_id: str, status: str, message: str = "") -> bool:
        """Update order status"""
        order = self._orders.get(order_id)
        if order:
            order["status"] = status
            order["status_history"].append({
                "status": status,
                "timestamp": datetime.now().isoformat(),
                "message": message
            })
            return True
        return False
    
    def get_all_orders(self) -> List[Dict]:
        """Get all orders (for debug)"""
        return [o for k, o in self._orders.items() if not k.startswith("phone:")]

order_store = OrderStore()

# ==================== MODELS ====================

class ProductInfo(BaseModel):
    name: Optional[str] = None
    price: Optional[str] = None
    originalPrice: Optional[str] = None
    discount: Optional[str] = None
    description: Optional[str] = None
    image: Optional[str] = None
    imageAlt: Optional[str] = None
    imageTitle: Optional[str] = None
    imageDescriptions: Optional[List[str]] = None  # From alt/title of multiple images
    attributes: Optional[Dict[str, str]] = None

class PageContext(BaseModel):
    url: str
    domain: str
    title: Optional[str] = None
    metaDescription: Optional[str] = None
    product: Optional[ProductInfo] = None
    pageText: Optional[str] = None
    structuredData: Optional[List[Dict]] = None
    openGraph: Optional[Dict[str, str]] = None
    timestamp: Optional[str] = None

class ChatMessage(BaseModel):
    role: str
    content: str

class RecentProduct(BaseModel):
    title: Optional[str] = None
    price: Optional[str] = None
    url: Optional[str] = None

class ClientLocation(BaseModel):
    permissionStatus: Optional[str] = None
    publicIp: Optional[str] = None
    publicCity: Optional[str] = None
    publicPincode: Optional[str] = None
    publicRegion: Optional[str] = None
    publicCountry: Optional[str] = None
    publicCountryCode: Optional[str] = None
    publicLatitude: Optional[float] = None
    publicLongitude: Optional[float] = None
    publicTimezone: Optional[str] = None
    publicIsp: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy: Optional[float] = None
    timezone: Optional[str] = None
    locale: Optional[str] = None
    capturedAt: Optional[str] = None
    errorCode: Optional[int] = None
    errorMessage: Optional[str] = None

class ChatRequest(BaseModel):
    message: str
    context: PageContext
    history: Optional[List[ChatMessage]] = None
    clientId: Optional[str] = None
    recentProducts: Optional[List[RecentProduct]] = None
    clientLocation: Optional[ClientLocation] = None

DEFAULT_DEMO_CLIENT_ID = "c3ffcb1b-afb9-4ca4-8746-a06698bec870"

class ChatResponse(BaseModel):
    reply: str
    action: Optional[str] = None
    metadata: Optional[Dict] = None
    products: Optional[List[Dict]] = None  # For product cards
    order_form: Optional[Dict] = None  # For order form display
    order_info: Optional[Dict] = None  # For order confirmation/status

class CustomerInfo(BaseModel):
    name: str
    phone: str
    address: str
    city: Optional[str] = None
    pincode: Optional[str] = None

class OrderRequest(BaseModel):
    session_id: str
    customer_info: CustomerInfo
    
class OrderStatusRequest(BaseModel):
    order_id: Optional[str] = None
    phone: Optional[str] = None

# ==================== SHOPIFY DATA FETCHER ====================

class ShopifyFetcher:
    """Fetches real-time data from Shopify public APIs"""
    
    TIMEOUT = 5.0  # 5 second timeout
    
    @staticmethod
    def get_store_url(domain: str) -> str:
        """Extract clean store URL from domain with domain normalization"""
        domain = _normalize_domain(domain)
        
        # Domain alias mapping (old domain -> correct domain)
        domain_aliases = {
            'samandmarshalleyewear.in': 'samandmarshall.com',
            'www.samandmarshalleyewear.in': 'samandmarshall.com',
        }
        
        # Normalize domain
        normalized = domain_aliases.get(domain.lower(), domain)
        
        # Add protocol
        if not normalized.startswith('http'):
            normalized = f"https://{normalized}"
        
        return normalized
    
    @staticmethod
    def is_shopify_store(domain: str) -> bool:
        """Check if domain is likely a Shopify store"""
        # Common Shopify indicators
        shopify_indicators = ['.myshopify.com', 'cdn.shopify.com']
        return any(ind in domain.lower() for ind in shopify_indicators)
    
    @classmethod
    async def fetch_products(cls, store_url: str, limit: int = 20) -> Optional[List[Dict]]:
        """Fetch products from Shopify store"""
        cache_key = f"products:{store_url}"
        cached = cache.get(cache_key)
        if cached:
            logger.info(f"Cache hit for products: {store_url}")
            return cached
        
        try:
            async with httpx.AsyncClient(timeout=cls.TIMEOUT) as client:
                response = await client.get(
                    f"{store_url}/products.json",
                    params={"limit": limit},
                    follow_redirects=True
                )
                if response.status_code == 200:
                    data = response.json()
                    products = data.get("products", [])
                    cache.set(cache_key, products)
                    logger.info(f"Fetched {len(products)} products from {store_url}")
                    return products
        except Exception as e:
            logger.warning(f"Failed to fetch products from {store_url}: {e}")
        return None
    
    @classmethod
    async def fetch_single_product(cls, store_url: str, handle: str) -> Optional[Dict]:
        """Fetch single product by handle"""
        cache_key = f"product:{store_url}:{handle}"
        cached = cache.get(cache_key)
        if cached:
            return cached
        
        try:
            async with httpx.AsyncClient(timeout=cls.TIMEOUT) as client:
                response = await client.get(
                    f"{store_url}/products/{handle}.json",
                    follow_redirects=True
                )
                if response.status_code == 200:
                    data = response.json()
                    product = data.get("product")
                    if product:
                        cache.set(cache_key, product)
                        return product
        except Exception as e:
            logger.warning(f"Failed to fetch product {handle}: {e}")
        return None
    
    @classmethod
    def _is_valid_collection_title(cls, title: str) -> bool:
        """Check if collection title is meaningful (not just numbers/codes)"""
        if not title:
            return False
        # Remove commas, spaces, dashes and check if only digits remain
        cleaned = title.replace(',', '').replace(' ', '').replace('-', '').replace('.', '')
        if cleaned.isdigit():
            return False
        # Also filter out very short titles that look like codes
        if len(title) <= 2 and title.isalnum():
            return False
        # Filter out generic collection names that don't provide value
        generic_names = ["all", "all products", "products", "home", "frontpage", "homepage"]
        if title.lower().strip() in generic_names:
            return False
        return True
    
    @classmethod
    async def fetch_collections(cls, store_url: str) -> Optional[List[Dict]]:
        """Fetch collections from store, filtering out numeric/code titles"""
        cache_key = f"collections:{store_url}"
        cached = cache.get(cache_key)
        if cached:
            return cached
        
        try:
            async with httpx.AsyncClient(timeout=cls.TIMEOUT) as client:
                response = await client.get(
                    f"{store_url}/collections.json",
                    follow_redirects=True
                )
                if response.status_code == 200:
                    data = response.json()
                    all_collections = data.get("collections", [])
                    
                    # Filter out collections with numeric-only or code-like titles
                    valid_collections = [
                        c for c in all_collections 
                        if cls._is_valid_collection_title(c.get("title", ""))
                    ]
                    
                    # If no valid collections, return all (some stores might only have coded ones)
                    collections = valid_collections if valid_collections else all_collections
                    
                    cache.set(cache_key, collections)
                    return collections
        except Exception as e:
            logger.warning(f"Failed to fetch collections: {e}")
        return None
    
    @classmethod
    async def extract_product_categories(cls, store_url: str) -> Optional[List[str]]:
        """Extract unique product types, tags, and title-based categories"""
        products = await cls.fetch_products(store_url, limit=50)
        if not products:
            return None
        
        categories = set()
        
        # Common fashion category keywords to look for in titles
        category_keywords = [
            "jacket", "jackets", "t-shirt", "tshirt", "shirt", "shirts",
            "pants", "pant", "jeans", "denim", "trousers", "shorts",
            "hoodie", "hoodies", "sweater", "sweatshirt", "pullover",
            "dress", "dresses", "skirt", "top", "tops", "blouse",
            "coat", "blazer", "cardigan", "vest",
            "shoes", "sneakers", "boots", "sandals",
            "bag", "bags", "backpack", "wallet",
            "hat", "cap", "beanie", "scarf", "belt",
            "watch", "watches", "jewelry", "jewellery",
            "sunglasses", "eyeglasses", "glasses",
            "kurta", "saree", "lehenga", "sherwani",
            "co-ord", "coord", "set", "jumpsuit", "romper"
        ]
        
        for product in products:
            # Add product_type if meaningful (not "variable", "simple", etc.)
            product_type = product.get("product_type", "").strip()
            skip_types = ["variable", "simple", "grouped", "external", ""]
            if product_type.lower() not in skip_types and cls._is_valid_collection_title(product_type):
                categories.add(product_type.title())
            
            # Add meaningful tags
            for tag in product.get("tags", []):
                if cls._is_valid_collection_title(tag) and len(tag) > 3:
                    skip_tags = ["new", "sale", "featured", "homepage", "best", "hot", "variable"]
                    if tag.lower() not in skip_tags:
                        categories.add(tag.title())
            
            # Extract category from product title
            title = product.get("title", "").lower()
            for keyword in category_keywords:
                if keyword in title:
                    # Normalize the category name
                    # Handle special cases first
                    normalization_map = {
                        "t-shirt": "T-Shirts", "tshirt": "T-Shirts", "t shirt": "T-Shirts",
                        "pants": "Pants", "pant": "Pants",
                        "jeans": "Jeans", "jean": "Jeans",
                        "jackets": "Jackets", "jacket": "Jackets",
                        "hoodies": "Hoodies", "hoodie": "Hoodies",
                        "shirts": "Shirts", "shirt": "Shirts",
                        "shorts": "Shorts", "short": "Shorts",
                        "dresses": "Dresses", "dress": "Dresses",
                        "sweaters": "Sweaters", "sweater": "Sweaters",
                        "sweatshirt": "Sweatshirts", "sweatshirts": "Sweatshirts",
                    }
                    normalized = normalization_map.get(keyword, keyword.title())
                    categories.add(normalized)
                    break  # Only take first matching category per product
        
        return sorted(list(categories))[:5] if categories else None
    
    @classmethod
    async def fetch_collection_products(cls, store_url: str, collection_handle: str) -> Optional[List[Dict]]:
        """Fetch products from a specific collection"""
        cache_key = f"collection:{store_url}:{collection_handle}"
        cached = cache.get(cache_key)
        if cached:
            return cached
        
        try:
            async with httpx.AsyncClient(timeout=cls.TIMEOUT) as client:
                response = await client.get(
                    f"{store_url}/collections/{collection_handle}/products.json",
                    follow_redirects=True
                )
                if response.status_code == 200:
                    data = response.json()
                    products = data.get("products", [])
                    cache.set(cache_key, products)
                    return products
        except Exception as e:
            logger.warning(f"Failed to fetch collection products: {e}")
        return None
    
    @classmethod
    async def fetch_policies(cls, store_url: str) -> Optional[Dict]:
        """Fetch store policies (refund, shipping, privacy, terms)"""
        cache_key = f"policies:{store_url}"
        cached = cache.get(cache_key)
        if cached:
            return cached
        
        policies = {}
        policy_pages = ['refund-policy', 'shipping-policy', 'privacy-policy', 'terms-of-service']
        
        try:
            async with httpx.AsyncClient(timeout=cls.TIMEOUT) as client:
                for policy in policy_pages:
                    try:
                        response = await client.get(
                            f"{store_url}/policies/{policy}",
                            follow_redirects=True
                        )
                        if response.status_code == 200:
                            # Extract text content (simplified)
                            text = response.text
                            # Try to find policy content
                            if '<div class="shopify-policy__body">' in text:
                                start = text.find('<div class="shopify-policy__body">') + len('<div class="shopify-policy__body">')
                                end = text.find('</div>', start)
                                policies[policy] = text[start:end][:2000]  # Limit size
                            else:
                                policies[policy] = "Policy available on website"
                    except:
                        pass
                
                if policies:
                    cache.set(cache_key, policies)
                    return policies
        except Exception as e:
            logger.warning(f"Failed to fetch policies: {e}")
        return None
    
    @classmethod
    async def search_products(cls, store_url: str, query: str) -> Optional[List[Dict]]:
        """Search products using Shopify search"""
        cache_key = f"search:{store_url}:{query}"
        cached = cache.get(cache_key)
        if cached:
            return cached
        
        try:
            async with httpx.AsyncClient(timeout=cls.TIMEOUT) as client:
                response = await client.get(
                    f"{store_url}/search/suggest.json",
                    params={"q": query, "resources[type]": "product", "resources[limit]": 10},
                    follow_redirects=True
                )
                if response.status_code == 200:
                    data = response.json()
                    products = data.get("resources", {}).get("results", {}).get("products", [])
                    cache.set(cache_key, products)
                    return products
        except Exception as e:
            logger.warning(f"Search failed: {e}")
        return None

# ==================== LLM-BASED MESSAGE ANALYZER ====================

class LLMMessageAnalyzer:
    """
    LLM-based message analysis for intent detection, entity extraction, and semantic understanding.
    Replaces keyword-based detection with a single LLM call for better accuracy across all product categories.
    """
    
    # Cache for analysis results to avoid repeated LLM calls
    _analysis_cache: Dict[str, Dict] = {}
    _cache_ttl = 300  # 5 minutes
    
    ANALYSIS_PROMPT = """Analyze this e-commerce chat message and extract structured information.

MESSAGE: "{message}"

CONTEXT (if viewing a product page):
- Current Product: {product_name}
- Product URL: {product_url}
- Page Title: {page_title}

Respond with a JSON object containing:
{{
    "intent": "<one of: product_search, price_check, availability, product_details, policy, order, order_status, collection, recommendation, deals, new_arrivals, general>",
    "entities": {{
        "collection": "<category/collection name if mentioned, e.g., 'shirts', 'electronics', 'sunglasses', null if none>",
        "product_type": "<specific product type if mentioned, e.g., 'formal shirt', 'running shoes', null if none>",
        "color": "<color if mentioned, null if none>",
        "size": "<size if mentioned (S/M/L/XL/38/40 etc), null if none>",
        "min_price": <minimum price as number if mentioned, null if none>,
        "max_price": <maximum price as number if mentioned, null if none>,
        "brand": "<brand name if mentioned, null if none>"
    }},
    "segment": "<one of: men, women, kids, unisex, null> - the target demographic if detectable from message or context",
    "is_recommendation_query": <true if user is asking for similar/related/recommended products, false otherwise>,
    "search_query": "<the core product search terms to use for semantic search, null if not a search query>"
}}

IMPORTANT RULES:
1. For "intent": 
   - "product_search" = user looking for products, browsing, searching
   - "recommendation" = user wants similar/related products to what they're viewing
   - "product_details" = user asking about a specific product (including ordinal references like "second one", "first product", "#2", "that one" from a previously shown list)
   - "price_check" = user asking about price of current product
   - "availability" = user asking about stock/sizes
   - "collection" = user asking for a category/collection (shirts, electronics, etc.)
   - "order" = user wants to buy/purchase
   - "order_status" = user asking about existing order
   - "policy" = shipping/returns/payment questions
   - "deals" = user looking for discounts/offers
   - "new_arrivals" = user wants newest products
   - "general" = greeting, thanks, other non-product queries

2. For "segment": Detect from context - e.g., "Men's Formal Shirt" → "men", "Women's Kurta" → "women"

3. For "is_recommendation_query": true for queries like "show me similar", "more like this", "alternatives", "what else", "recommend something"
   IMPORTANT: Ordinal/positional references like "second one", "first one", "the third product", "#2", "tell me more about the 2nd", "that one" are NOT recommendation queries. They refer to a product already listed in the conversation. Set intent to "product_details", is_recommendation_query to false, and search_query to null.

4. For "search_query": Extract the key terms for vector search. E.g., "show me blue formal shirts under 2000" → "blue formal shirts"
   Set to null when the user is referring to a previously listed product (ordinal references).

5. Be GENERIC - this should work for fashion, electronics, home goods, any e-commerce category.

Respond ONLY with the JSON object, no explanation."""

    @classmethod
    async def analyze_message(
        cls,
        message: str,
        context: Optional['PageContext'] = None,
        use_cache: bool = True
    ) -> Dict[str, Any]:
        """
        Analyze a chat message using LLM for intent, entities, and semantic understanding.
        
        Args:
            message: User's chat message
            context: Optional page context (product info, URL, etc.)
            use_cache: Whether to use cached results
            
        Returns:
            Dict with intent, entities, segment, is_recommendation_query, search_query
        """
        # Build cache key
        cache_key = f"{message}:{context.product.name if context and context.product else ''}".lower()
        
        # Check cache
        if use_cache and cache_key in cls._analysis_cache:
            cached = cls._analysis_cache[cache_key]
            if cached.get("_timestamp", 0) > time.time() - cls._cache_ttl:
                logger.debug(f"🧠 LLM analysis cache hit for: {message[:50]}...")
                return cached
        
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            logger.warning("⚠️ No OpenRouter API key - falling back to keyword detection")
            return cls._fallback_analysis(message, context)
        
        try:
            client = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
            
            # Build context info
            product_name = context.product.name if context and context.product else "N/A"
            product_url = context.url if context else "N/A"
            page_title = context.title if context else "N/A"
            
            # Format prompt
            prompt = cls.ANALYSIS_PROMPT.format(
                message=message,
                product_name=product_name,
                product_url=product_url,
                page_title=page_title
            )
            
            response = client.chat.completions.create(
                model=_SMALLER_LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                temperature=0.1,
                response_format={"type": "json_object"}
            )

            result_text = response.choices[0].message.content
            result = json.loads(result_text)
            
            result["_timestamp"] = time.time()
            cls._analysis_cache[cache_key] = result
            
            logger.info(f"🧠 LLM analysis: intent={result.get('intent')}, segment={result.get('segment')}, "
                       f"is_rec={result.get('is_recommendation_query')}")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ LLM analysis failed: {e}")
            return cls._fallback_analysis(message, context)
    
    @classmethod
    def _fallback_analysis(cls, message: str, context: Optional['PageContext'] = None) -> Dict[str, Any]:
        """
        Fallback to keyword-based analysis when LLM is unavailable.
        Uses the existing IntentDetector logic.
        """
        # Use existing IntentDetector as fallback
        intent, entities = IntentDetector.detect(message)
        
        # Try to extract segment from context
        segment = None
        if context:
            segment = extract_segment_from_context(context)
        
        # Check for recommendation keywords
        recommendation_keywords = ["similar", "like this", "recommend", "related", "more like", "alternatives"]
        is_recommendation = any(kw in message.lower() for kw in recommendation_keywords)
        
        return {
            "intent": intent,
            "entities": {
                "collection": entities.get("collection"),
                "product_type": entities.get("product_type"),
                "color": entities.get("color"),
                "size": entities.get("size"),
                "min_price": None,
                "max_price": entities.get("max_price"),
                "brand": None
            },
            "segment": segment,
            "is_recommendation_query": is_recommendation,
            "search_query": message,  # Use full message as fallback
            "_timestamp": time.time(),
            "_fallback": True
        }
    
    @classmethod
    def clear_cache(cls):
        """Clear the analysis cache."""
        cls._analysis_cache.clear()

# ==================== INTENT DETECTION (LEGACY - KEPT FOR FALLBACK) ====================

class IntentDetector:
    """Detects user intent from message"""
    
    INTENTS = {
        "product_search": [
            "show me", "find", "looking for", "search", "browse", 
            "what products", "products you have", "products you got",
            "show products", "all products", "your products", "list products",
            "do you have", "recommend", "suggest", "similar",
            "what do you sell", "what you sell", "what's available", "show all",
            "products", "items", "catalog", "inventory", "stock"
        ],
        "price_check": [
            "price", "cost", "how much", "rate", "pricing", "expensive", "cheap",
            "discount", "offer", "sale", "deal", "coupon", "promo"
        ],
        "availability": [
            "available", "in stock", "size", "stock", "have it", "can i get",
            "out of stock", "when available", "restock"
        ],
        "product_details": [
            "tell me about", "details", "specifications", "specs", "material",
            "fabric", "made of", "features", "description", "info about"
        ],
        "policy": [
            "return", "refund", "exchange", "shipping", "delivery", "policy",
            "how long", "cancel", "warranty"
        ],
        "order": [
            "order", "buy", "purchase", "add to cart", "checkout", "want this",
            "i want", "get this", "book", "place order", "order now"
        ],
        "order_status": [
            "order status", "track order", "where is my order", "order tracking",
            "my order", "check order", "delivery status", "when will", "track my",
            "order update", "shipping status"
        ],
        "collection": [
            "collection", "category", "all", "men", "women", "new arrivals",
            "best sellers", "trending", "eyeglasses", "sunglasses", "jewellery",
            "jewelry", "eyewear", "categories"
        ],
        "recommendation": [
            "similar", "like this", "more like", "recommend", "suggestion",
            "other options", "alternatives", "you may like", "related",
            "same style", "same type", "in this range"
        ],
        "deals": [
            "deals", "offers", "discount", "sale", "best deals", "on sale",
            "discounted", "offers today", "hot deals"
        ],
        "new_arrivals": [
            "new arrivals", "latest", "new products", "just in", "new collection",
            "fresh", "recently added", "what's new", "newest"
        ]
    }
    
    # Collection name mappings (common terms -> likely collection handles)
    COLLECTION_MAPPINGS = {
        "eyeglasses": ["eyeglasses", "eyeglass", "glasses", "spectacles"],
        "sunglasses": ["sunglasses", "sunglass", "sun glasses"],
        "jewellery": ["jewellery", "jewelry", "jewels"],
        "men": ["men", "mens", "male"],
        "women": ["women", "womens", "female"],
        # Clothing categories
        "shirts": ["shirt", "shirts"],
        "trousers": ["trouser", "trousers", "pants", "pant", "baggy trouser", "baggy trousers"],
        "jeans": ["jeans", "denim"],
        "shorts": ["shorts", "short"],
        "hoodies": ["hoodie", "hoodies", "sweatshirt", "sweatshirts"],
        "t-shirts": ["t-shirt", "tshirt", "tee", "t shirt"],
        "jackets": ["jacket", "jackets"],
        "dresses": ["dress", "dresses"],
        "sweaters": ["sweater", "sweaters", "pullover"],
        "cargos": ["cargo", "cargos"],
        "co-ords": ["co-ord", "coord", "co ord", "coordinate", "co-ords"],
        "kurtas": ["kurta", "kurtas"],
        "overshirts": ["overshirt", "overshirts", "shacket", "shackets"],
        "polo": ["polo", "polos"],
        # Generic categories
        "bottoms": ["bottom", "bottoms"],
        "tops": ["top", "tops"],
        "denim": ["denim", "denims"],
        "varsity": ["varsity", "varsities"],
        "new-arrivals": ["new arrivals", "new arrival", "new in", "fresh drop"],
        "bestsellers": ["bestseller", "bestsellers", "best seller", "best sellers", "top sellers"],
    }
    
    @classmethod
    def detect(cls, message: str) -> Tuple[str, Dict]:
        """Detect intent and extract entities"""
        message_lower = message.lower()
        
        # Detect intent
        detected_intent = "general"
        max_matches = 0
        
        for intent, keywords in cls.INTENTS.items():
            matches = sum(1 for kw in keywords if kw in message_lower)
            if matches > max_matches:
                max_matches = matches
                detected_intent = intent
        
        # Extract entities
        entities = {}
        
        # Extract collection name (check before product type to prioritize collections)
        # Sort keywords by length (longer first) to match more specific terms first
        all_mappings = []
        for collection_handle, keywords in cls.COLLECTION_MAPPINGS.items():
            for keyword in keywords:
                all_mappings.append((collection_handle, keyword))
        # Sort by keyword length descending (longer matches first)
        all_mappings.sort(key=lambda x: len(x[1]), reverse=True)
        
        for collection_handle, keyword in all_mappings:
            if keyword in message_lower:
                entities["collection"] = keyword  # Use the matched keyword, not the handle
                entities["collection_handle_hint"] = collection_handle  # Keep handle as hint
                # If collection detected, set intent to collection
                if detected_intent != "collection":
                    detected_intent = "collection"
                break
        
        # Extract size
        size_match = re.search(r'\b(xs|s|m|l|xl|xxl|xxxl|\d{2})\b', message_lower)
        if size_match:
            entities["size"] = size_match.group(1).upper()
        
        # Extract color
        colors = ["black", "white", "red", "blue", "green", "yellow", "pink", "grey", "gray", "brown", "navy", "beige"]
        for color in colors:
            if color in message_lower:
                entities["color"] = color
                break
        
        # Extract product type (only if collection not detected)
        # Most product types should already be captured as collections above
        if "collection" not in entities:
            product_types = ["shoes", "footwear", "sneakers", "boots", "accessories", "bags", "watch", "watches", "belt", "belts"]
            for ptype in product_types:
                if ptype in message_lower:
                    entities["product_type"] = ptype
                    break
        
        # Extract price range
        price_match = re.search(r'under\s*(?:rs\.?|₹|inr)?\s*(\d+)', message_lower)
        if price_match:
            entities["max_price"] = int(price_match.group(1))
        
        return detected_intent, entities

# ==================== PRODUCT FORMATTER ====================

class ProductFormatter:
    """Formats product data for responses"""
    
    @staticmethod
    def format_product_card(product: Dict) -> Dict:
        """Format product for card display"""
        variants = product.get("variants", [{}])
        first_variant = variants[0] if variants else {}
        images = product.get("images", [{}])
        first_image = images[0] if images else {}
        
        price = first_variant.get("price", "0")
        compare_price = first_variant.get("compare_at_price")
        
        # Calculate discount
        discount = None
        if compare_price and float(compare_price) > float(price):
            discount_pct = int(((float(compare_price) - float(price)) / float(compare_price)) * 100)
            discount = f"{discount_pct}% OFF"
        
        # Check if ANY variant is available (not just the first one)
        variants = product.get("variants", [])
        is_any_available = any(v.get("available", True) for v in variants) if variants else True
        
        return {
            "id": product.get("id"),
            "title": product.get("title"),
            "handle": product.get("handle"),
            "price": f"₹{price}",
            "originalPrice": f"₹{compare_price}" if compare_price else None,
            "discount": discount,
            "image": first_image.get("src"),
            "available": is_any_available,  # True if ANY size/variant is in stock
            "url": f"/products/{product.get('handle')}"
        }
    
    @staticmethod
    def format_product_summary(product: Dict, store_url: str = "") -> str:
        """Format product as text summary with proper spacing"""
        title = product.get("title", "Unknown Product")
        handle = product.get("handle", "")
        variants = product.get("variants", [{}])
        first_variant = variants[0] if variants else {}
        
        price = first_variant.get("price", "N/A")
        compare_price = first_variant.get("compare_at_price")
        
        # Get available sizes (variants that are in stock)
        available_variants = [v for v in variants if v.get("available", True)]
        sizes = [v.get("title", v.get("option1", "")) for v in available_variants]
        sizes_str = ", ".join(sizes[:5]) if sizes else "Check website"
        
        # Product is available if ANY variant is in stock
        is_available = len(available_variants) > 0
        
        # Cleaner format with better spacing
        summary = f"**{title}**"
        summary += f"\n   💰 ₹{price}"
        
        if compare_price:
            try:
                if float(compare_price) > float(price):
                    discount_pct = int(((float(compare_price) - float(price)) / float(compare_price)) * 100)
                    summary += f" (was ₹{compare_price}, {discount_pct}% OFF)"
            except (ValueError, TypeError):
                pass
        
        # Only show stock status if out of stock; don't clutter with "In Stock" for every item
        if not is_available:
            summary += f"\n   📦 ❌ Out of Stock"
        elif sizes:
            summary += f"\n   📏 Available in: {sizes_str}"
        
        # Add product link if handle and store_url are available
        if handle and store_url:
            product_url = f"{store_url}/products/{handle}"
            summary += f"\n   🔗 {product_url}"
        
        return summary
    
    @staticmethod
    def check_size_availability(product: Dict, size: str) -> Tuple[bool, str]:
        """Check if specific size is available"""
        variants = product.get("variants", [])
        
        for variant in variants:
            variant_size = variant.get("title", variant.get("option1", "")).upper()
            if size.upper() in variant_size:
                if variant.get("available", True):
                    price = variant.get("price", "N/A")
                    return True, f"Yes! Size {size} is available at ₹{price}"
                else:
                    return False, f"Sorry, size {size} is currently out of stock"
        
        return False, f"Size {size} is not available for this product"
    
    @staticmethod
    def filter_by_price(products: List[Dict], max_price: float) -> List[Dict]:
        """Filter products by maximum price"""
        filtered = []
        for product in products:
            variants = product.get("variants", [])
            if not variants:
                continue
            
            # Get the minimum price from all variants
            min_price = None
            for variant in variants:
                try:
                    price = float(variant.get("price", "0"))
                    if min_price is None or price < min_price:
                        min_price = price
                except (ValueError, TypeError):
                    continue
            
            # Include product if minimum price is within budget
            if min_price is not None and min_price <= max_price:
                filtered.append(product)
        
        return filtered
    
    @staticmethod
    def get_product_min_price(product: Dict) -> float:
        """Get the minimum price from all variants of a product"""
        variants = product.get("variants", [])
        if not variants:
            return float('inf')
        
        min_price = float('inf')
        for variant in variants:
            try:
                price = float(variant.get("price", "0"))
                if price < min_price:
                    min_price = price
            except (ValueError, TypeError):
                continue
        
        return min_price if min_price != float('inf') else 0

# ==================== RECOMMENDATION ENGINE ====================

class RecommendationEngine:
    """Smart product recommendations based on various strategies"""
    
    @staticmethod
    def _extract_category_keywords(title: str) -> set:
        """Extract category keywords from product title"""
        # Common fashion category words
        category_words = {
            'shirt', 'shirts', 't-shirt', 'tshirt',
            'trouser', 'trousers', 'pant', 'pants', 'jeans',
            'kurta', 'kurtas', 'kurti', 'kurtis',
            'saree', 'sarees', 'sari', 'saris',
            'dress', 'dresses', 'gown', 'gowns',
            'jacket', 'jackets', 'blazer', 'blazers', 'coat', 'coats',
            'palazzo', 'palazzos', 'legging', 'leggings',
            'shorts', 'skirt', 'skirts',
            'sweater', 'sweaters', 'cardigan', 'cardigans',
            'top', 'tops', 'blouse', 'blouses',
            'suit', 'suits', 'sherwani', 'sherwanis',
            'sneaker', 'sneakers', 'shoe', 'shoes', 'sandal', 'sandals',
            'bag', 'bags', 'wallet', 'wallets',
            'watch', 'watches', 'accessory', 'accessories'
        }
        title_words = set(title.lower().split())
        return title_words & category_words
    
    @staticmethod
    def _extract_segment(title: str, tags: list = None) -> str:
        """Extract target segment (kids, men, women) from product title and tags"""
        title_lower = title.lower()
        tags_lower = [t.lower() for t in (tags or [])]
        all_text = title_lower + " " + " ".join(tags_lower)
        
        # Kids/Children segment (HIGHEST PRIORITY - must match exactly)
        kids_keywords = ['kids', 'kid', 'children', 'child', 'boy', 'boys', 'girl', 'girls', 
                         'baby', 'babies', 'infant', 'toddler', 'junior', 'young']
        for keyword in kids_keywords:
            if keyword in all_text:
                return 'kids'
        
        # Women segment
        women_keywords = ['women', 'woman', 'ladies', 'lady', 'female', 'her', 'girls']
        # Exclude if it's kids (already checked above)
        for keyword in women_keywords:
            if keyword in all_text:
                return 'women'
        
        # Men segment
        men_keywords = ['men', 'man', 'male', 'his', 'boys', 'gents', 'gentleman']
        for keyword in men_keywords:
            if keyword in all_text:
                return 'men'
        
        return 'unisex'  # Default
    
    @staticmethod
    def get_category_recommendations(
        current_product: Dict, 
        all_products: List[Dict], 
        limit: int = 5
    ) -> List[Dict]:
        """
        Category-based recommendations (most reliable)
        - MUST match segment (kids/men/women) - CRITICAL!
        - Same collection/product_type
        - Same category keyword in title (e.g., "Palazzos")
        - Different product ID
        - Sorted by score, then price
        """
        current_id = current_product.get("id")
        current_type = current_product.get("product_type", "").lower()
        current_tags = list(current_product.get("tags", []))
        current_tags_set = set(t.lower() for t in current_tags)
        current_title = current_product.get("title", "")
        current_category_words = RecommendationEngine._extract_category_keywords(current_title)
        
        # CRITICAL: Extract segment (kids, men, women, unisex)
        current_segment = RecommendationEngine._extract_segment(current_title, current_tags)
        logger.info(f"🎯 Current product segment: {current_segment} | Title: {current_title[:50]}")
        
        scored_recommendations = []
        for product in all_products:
            if product.get("id") == current_id:
                continue
            
            product_title = product.get("title", "")
            product_tags = list(product.get("tags", []))
            product_segment = RecommendationEngine._extract_segment(product_title, product_tags)
            
            # CRITICAL: Kids products MUST only show kids products
            # Men products MUST only show men products
            # Women products MUST only show women products
            if current_segment == 'kids' and product_segment != 'kids':
                continue  # Skip non-kids products for kids search
            if current_segment == 'men' and product_segment not in ('men', 'unisex'):
                continue
            if current_segment == 'women' and product_segment not in ('women', 'unisex'):
                continue
            
            # Match by product_type or overlapping tags
            product_type = product.get("product_type", "").lower()
            product_tags_set = set(t.lower() for t in product_tags)
            product_category_words = RecommendationEngine._extract_category_keywords(product_title)
            
            score = 0
            
            # Segment match bonus (same segment gets extra points)
            if current_segment == product_segment:
                score += 5
            
            # Highest priority: exact product_type match
            if current_type and current_type == product_type:
                score += 10
            
            # High priority: same category word in title (e.g., both have "Palazzos")
            if current_category_words and current_category_words & product_category_words:
                score += 8
            
            # Medium priority: tag overlap
            tag_overlap = len(current_tags_set & product_tags_set)
            if tag_overlap >= 2:
                score += tag_overlap
            
            if score > 0:
                scored_recommendations.append((product, score))
        
        # Sort by score (highest first), then by price
        scored_recommendations.sort(key=lambda x: (-x[1], ProductFormatter.get_product_min_price(x[0])))
        
        logger.info(f"🎯 Found {len(scored_recommendations)} same-segment recommendations for '{current_segment}'")
        return [p[0] for p in scored_recommendations[:limit]]
    
    @staticmethod
    def get_tag_similarity_recommendations(
        current_product: Dict, 
        all_products: List[Dict], 
        limit: int = 5
    ) -> List[Dict]:
        """
        Tag similarity (content-based)
        - MUST match segment (kids/men/women)
        - Overlapping tags
        - Same product_type
        - Ranked by tag overlap score
        """
        current_id = current_product.get("id")
        current_tags = list(current_product.get("tags", []))
        current_tags_set = set(t.lower() for t in current_tags)
        current_type = current_product.get("product_type", "").lower()
        current_title = current_product.get("title", "")
        current_segment = RecommendationEngine._extract_segment(current_title, current_tags)
        
        scored_products = []
        for product in all_products:
            if product.get("id") == current_id:
                continue
            
            product_title = product.get("title", "")
            product_tags = list(product.get("tags", []))
            product_segment = RecommendationEngine._extract_segment(product_title, product_tags)
            
            # CRITICAL: Segment must match
            if current_segment == 'kids' and product_segment != 'kids':
                continue
            if current_segment == 'men' and product_segment not in ('men', 'unisex'):
                continue
            if current_segment == 'women' and product_segment not in ('women', 'unisex'):
                continue
            
            product_tags_set = set(t.lower() for t in product_tags)
            product_type = product.get("product_type", "").lower()
            
            # Calculate similarity score
            tag_overlap = len(current_tags_set & product_tags_set)
            type_match = 2 if current_type and current_type == product_type else 0
            score = tag_overlap + type_match
            
            if score > 0:
                scored_products.append((product, score))
        
        # Sort by score (highest first)
        scored_products.sort(key=lambda x: x[1], reverse=True)
        return [p[0] for p in scored_products[:limit]]
    
    @staticmethod
    def get_price_band_recommendations(
        current_product: Dict, 
        all_products: List[Dict], 
        price_tolerance: float = 0.2,  # ±20%
        limit: int = 5
    ) -> List[Dict]:
        """
        Price-band recommendations
        - MUST match segment (kids/men/women)
        - Price within ±20% of current product
        - Same category preferred
        """
        current_id = current_product.get("id")
        current_price = ProductFormatter.get_product_min_price(current_product)
        current_type = current_product.get("product_type", "").lower()
        current_title = current_product.get("title", "")
        current_tags = list(current_product.get("tags", []))
        current_segment = RecommendationEngine._extract_segment(current_title, current_tags)
        
        if current_price <= 0:
            return []
        
        min_price = current_price * (1 - price_tolerance)
        max_price = current_price * (1 + price_tolerance)
        
        recommendations = []
        for product in all_products:
            if product.get("id") == current_id:
                continue
            
            product_title = product.get("title", "")
            product_tags = list(product.get("tags", []))
            product_segment = RecommendationEngine._extract_segment(product_title, product_tags)
            
            # CRITICAL: Segment must match
            if current_segment == 'kids' and product_segment != 'kids':
                continue
            if current_segment == 'men' and product_segment not in ('men', 'unisex'):
                continue
            if current_segment == 'women' and product_segment not in ('women', 'unisex'):
                continue
            
            product_price = ProductFormatter.get_product_min_price(product)
            if min_price <= product_price <= max_price:
                # Boost score if same category
                product_type = product.get("product_type", "").lower()
                same_category = current_type and current_type == product_type
                recommendations.append((product, same_category))
        
        # Sort: same category first, then by price
        recommendations.sort(key=lambda x: (not x[1], ProductFormatter.get_product_min_price(x[0])))
        return [p[0] for p in recommendations[:limit]]
    
    @staticmethod
    def get_discount_recommendations(
        all_products: List[Dict], 
        limit: int = 5
    ) -> List[Dict]:
        """
        Discount-driven recommendations
        - Products with compare_at_price (on sale)
        - Sorted by discount percentage (highest first)
        """
        discounted_products = []
        
        for product in all_products:
            variants = product.get("variants", [])
            if not variants:
                continue
            
            first_variant = variants[0]
            price = float(first_variant.get("price", "0") or "0")
            compare_price = first_variant.get("compare_at_price")
            
            if compare_price:
                try:
                    compare_price = float(compare_price)
                    if compare_price > price > 0:
                        discount_pct = ((compare_price - price) / compare_price) * 100
                        discounted_products.append((product, discount_pct))
                except (ValueError, TypeError):
                    continue
        
        # Sort by discount percentage (highest first)
        discounted_products.sort(key=lambda x: x[1], reverse=True)
        return [p[0] for p in discounted_products[:limit]]
    
    @staticmethod
    def get_new_arrivals(
        all_products: List[Dict], 
        limit: int = 5
    ) -> List[Dict]:
        """
        Freshness-based recommendations
        - Sorted by created_at / published_at (newest first)
        """
        dated_products = []
        
        for product in all_products:
            # Try created_at first, then published_at
            date_str = product.get("created_at") or product.get("published_at")
            if date_str:
                try:
                    # Parse ISO date format
                    from datetime import datetime
                    date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                    dated_products.append((product, date))
                except (ValueError, TypeError):
                    continue
        
        # Sort by date (newest first)
        dated_products.sort(key=lambda x: x[1], reverse=True)
        return [p[0] for p in dated_products[:limit]]
    
    @staticmethod
    def get_collection_popularity_recommendations(
        collections: List[Dict],
        collection_products: Dict[str, List[Dict]],
        limit: int = 5
    ) -> List[Dict]:
        """
        Collection popularity proxy
        - Prioritize frontpage/homepage collections
        - Higher product count collections
        - Recently updated products
        """
        # Priority collections (likely popular)
        priority_handles = ["frontpage", "homepage", "bestsellers", "best-sellers", "featured", "trending"]
        
        scored_collections = []
        for col in collections:
            handle = col.get("handle", "").lower()
            products_count = col.get("products_count", 0)
            
            # Calculate score
            score = products_count
            if handle in priority_handles:
                score += 100  # Big boost for priority collections
            if "new" in handle or "arrival" in handle:
                score += 50
            if "sale" in handle or "deal" in handle:
                score += 30
            
            scored_collections.append((col, score))
        
        # Get products from top collections
        scored_collections.sort(key=lambda x: x[1], reverse=True)
        
        recommendations = []
        for col, _ in scored_collections[:3]:
            handle = col.get("handle")
            if handle and handle in collection_products:
                recommendations.extend(collection_products[handle][:3])
        
        # Remove duplicates while preserving order
        seen_ids = set()
        unique_recommendations = []
        for product in recommendations:
            pid = product.get("id")
            if pid not in seen_ids:
                seen_ids.add(pid)
                unique_recommendations.append(product)
        
        return unique_recommendations[:limit]
    
    @classmethod
    def get_smart_recommendations(
        cls,
        current_product: Optional[Dict],
        all_products: List[Dict],
        recommendation_type: str = "auto",
        limit: int = 5
    ) -> Tuple[List[Dict], str]:
        """
        Get smart recommendations based on context
        
        Returns: (products, recommendation_label)
        """
        if not all_products:
            return [], ""
        
        if recommendation_type == "auto" and current_product:
            # Try different strategies in order
            recs = cls.get_category_recommendations(current_product, all_products, limit)
            if recs:
                product_type = current_product.get("product_type", "products")
                return recs, f"More {product_type.lower()} you may like"
            
            recs = cls.get_tag_similarity_recommendations(current_product, all_products, limit)
            if recs:
                return recs, "Similar styles"
            
            recs = cls.get_price_band_recommendations(current_product, all_products, limit=limit)
            if recs:
                price = ProductFormatter.get_product_min_price(current_product)
                return recs, f"More options around ₹{int(price)}"
        
        elif recommendation_type == "category" and current_product:
            recs = cls.get_category_recommendations(current_product, all_products, limit)
            product_type = current_product.get("product_type", "products")
            return recs, f"More {product_type.lower()} you may like"
        
        elif recommendation_type == "similar" and current_product:
            recs = cls.get_tag_similarity_recommendations(current_product, all_products, limit)
            return recs, "Similar styles"
        
        elif recommendation_type == "price_band" and current_product:
            recs = cls.get_price_band_recommendations(current_product, all_products, limit=limit)
            price = ProductFormatter.get_product_min_price(current_product)
            return recs, f"More options around ₹{int(price)}"
        
        elif recommendation_type == "deals":
            recs = cls.get_discount_recommendations(all_products, limit)
            return recs, "Best deals right now 🔥"
        
        elif recommendation_type == "new_arrivals":
            recs = cls.get_new_arrivals(all_products, limit)
            return recs, "New arrivals ✨"
        
        # Fallback for "auto" when current_product exists but no category match
        # Try to find ANYTHING in same category first, don't just show random products
        if current_product:
            # Try price band but same category only
            recs = cls.get_price_band_recommendations(current_product, all_products, price_tolerance=0.5, limit=limit)
            if recs:
                # Filter to same product_type if available
                current_type = current_product.get("product_type", "").lower()
                if current_type:
                    same_type_recs = [p for p in recs if p.get("product_type", "").lower() == current_type]
                    if same_type_recs:
                        return same_type_recs[:limit], f"More {current_type.lower()} options"
                
                # Otherwise return price band results
                price = ProductFormatter.get_product_min_price(current_product)
                return recs, f"More options around ₹{int(price)}"
        
        # Only show deals/new arrivals if explicitly requested or no current product context
        if not current_product:
            recs = cls.get_discount_recommendations(all_products, limit)
            if recs:
                return recs, "Best deals right now 🔥"
            
            recs = cls.get_new_arrivals(all_products, limit)
            return recs, "You might also like"
        
        # If we have a current product but couldn't find similar, return empty
        # Better to show nothing than show unrelated products
        return [], ""

# ==================== JSON-LD PRODUCT PARSER ====================

def _build_current_product_from_jsonld(context: "PageContext") -> Optional[Dict]:
    """Build a current-product dict from the page's JSON-LD structured data.

    Uses ``context.structuredData`` (full, un-truncated JSON-LD parsed by the
    browser extension) as the primary source.  Falls back to regex-parsing the
    JSON-LD block embedded in ``context.pageText``.

    Returns a dict shaped like a Shopify product.json response (title, variants
    with sku/price/available/title, images, vendor) or ``None``.
    """
    product_data = _find_jsonld_product(context.structuredData, context.pageText)
    if not product_data:
        return None

    offers = product_data.get("offers", [])
    if isinstance(offers, dict):
        offers = [offers]

    # When JSON-LD contains only a single offer without a per-variant SKU,
    # it's an aggregate "product is in stock" signal — not per-size data.
    # In that case we still return product metadata (title, images, brand)
    # but leave variants empty so the LLM answers from the page text.
    is_aggregate = (
        len(offers) <= 1
        and not any(o.get("sku") for o in offers)
    )

    variants: List[Dict] = []
    if not is_aggregate:
        # Try to extract size labels from page text so variant titles are
        # human-readable (e.g. "38 / Green") instead of raw SKUs.
        size_labels = _extract_size_labels_from_page(context.pageText) if context.pageText else []

        for idx, offer in enumerate(offers):
            sku = offer.get("sku", "")
            avail_str = offer.get("availability", "")
            available = "InStock" in avail_str

            title = size_labels[idx] if idx < len(size_labels) else sku
            price = str(offer.get("price", "0"))

            variants.append({
                "sku": sku,
                "title": title,
                "price": price,
                "available": available,
            })

    images_raw = product_data.get("image", [])
    if isinstance(images_raw, str):
        images_raw = [images_raw]

    brand = product_data.get("brand", {})
    if isinstance(brand, dict):
        brand = brand.get("name", "")

    return {
        "title": product_data.get("name", ""),
        "description": product_data.get("description", ""),
        "variants": variants,
        "images": [{"src": img} for img in images_raw],
        "vendor": brand,
    }


def _find_jsonld_product(
    structured_data: Optional[List[Dict]], page_text: Optional[str]
) -> Optional[Dict]:
    """Locate the Product JSON-LD object from structuredData or pageText."""
    # 1. Try context.structuredData (full, un-truncated)
    if structured_data:
        for item in structured_data:
            if not isinstance(item, dict):
                continue
            if item.get("@type") == "Product":
                return item
            for graph_item in (item.get("@graph") or []):
                if isinstance(graph_item, dict) and graph_item.get("@type") == "Product":
                    return graph_item

    # 2. Fallback: parse from pageText (may be truncated to 3 000 chars)
    if page_text:
        match = re.search(
            r'=== PRODUCT STRUCTURED DATA \(JSON-LD\) ===\s*(\{.*)',
            page_text,
            re.DOTALL,
        )
        if match:
            raw = match.group(1).strip()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                balanced = raw
                open_b = balanced.count("{") - balanced.count("}")
                open_a = balanced.count("[") - balanced.count("]")
                if open_a > 0:
                    balanced += "]" * open_a
                if open_b > 0:
                    balanced += "}" * open_b
                try:
                    return json.loads(balanced)
                except json.JSONDecodeError:
                    pass
    return None


def _extract_size_labels_from_page(page_text: str) -> List[str]:
    """Best-effort extraction of ordered size labels from scraped page text.

    Looks for the pattern common on Shopify product pages where each size is
    immediately followed by ``Variant sold out or unavailable`` or appears in
    a "Select Your Size" block.
    """
    labels: List[str] = []

    # Pattern 1: "38Variant sold out or unavailable" (Shopify default theme)
    matches = re.findall(r'(\S+?)Variant sold out or unavailable', page_text)
    if matches:
        return matches

    # Pattern 2: explicit "Size:" header followed by lines of size values
    size_block = re.search(r'Size:\s*(?:Size Guide\s*)?((?:\n?.+)+)', page_text)
    if size_block:
        raw = size_block.group(1)
        for line in raw.split('\n'):
            token = line.strip()
            if not token or len(token) > 20:
                break
            if re.match(r'^[A-Z0-9/\s\-]+$', token, re.IGNORECASE):
                labels.append(token)
        if labels:
            return labels

    return labels


# ==================== PRODUCT SEARCH SERVICE ====================

class ProductSearchService:
    """Wraps search_products_pipeline with domain-based client_id resolution."""

    SEARCH_TOOL_SCHEMA = {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": (
                "Search the store's product catalog for products. "
                "Use when the user asks for product recommendations, similar or complementary products, "
                "wants to browse/explore products, or searches for specific items by name, category, "
                "style, color, or price range."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Natural language search query describing what the user wants "
                            "(e.g. 'black hoodies under 2000', 'casual T-shirts', 'party dresses')"
                        ),
                    }
                },
                "required": ["query"],
            },
        },
    }

    @staticmethod
    async def is_available(client_id: Optional[str] = None, domain: Optional[str] = None) -> bool:
        if not UPSTASH_SEARCH_AVAILABLE:
            return False
        return bool(client_id or await aget_pg_client_id_for_domain(domain or "") or DEFAULT_DEMO_CLIENT_ID)

    @staticmethod
    async def search(
        query: str,
        client_id: Optional[str] = None,
        domain: Optional[str] = None,
        product_context=None,
        history: Optional[List] = None,
        exclude_handles: Optional[List[str]] = None,
    ) -> Tuple[Optional[str], List[Dict]]:
        """Run QU → Upstash Search → Reranker. Returns (formatted_text, merged_products)."""
        effective_id = client_id or await aget_pg_client_id_for_domain(domain or "") or DEFAULT_DEMO_CLIENT_ID
        if not UPSTASH_SEARCH_AVAILABLE or not effective_id:
            return None, []

        try:
            conv_history = []
            if history:
                for msg in history[-20:]:
                    conv_history.append({"role": msg.role, "content": msg.content[:300]})

            exclude_handle = None
            focal_product_context = None
            if product_context and product_context.name:
                url = getattr(product_context, "url", "") or ""
                handle_match = re.search(r'/products/([^/?]+)', url)
                if handle_match:
                    exclude_handle = handle_match.group(1)
                attrs = product_context.attributes or {}
                focal_product_context = {
                    "name": product_context.name,
                    "subcategory": attrs.get("subcategory") or attrs.get("product_type") or attrs.get("type"),
                    "category": attrs.get("category"),
                    "color": attrs.get("color"),
                    "style": attrs.get("style"),
                    "price": product_context.price,
                }
                focal_product_context = {k: v for k, v in focal_product_context.items() if v}

            pipeline = await search_products_pipeline(
                query=query,
                client_id=effective_id,
                conversation_history=conv_history,
                user_profile={},
                exclude_handle=exclude_handle,
                exclude_handles=exclude_handles,
                max_results=5,
                product_context=focal_product_context,
            )

            if not pipeline.products:
                return None, []

            products = []
            for p in pipeline.products:
                merged = {}
                merged.update(p.get("content", {}))
                merged.update(p.get("metadata", {}))
                products.append(merged)

            formatted_text = format_products_for_llm(products, pipeline.follow_up)
            return formatted_text, products

        except Exception as e:
            logger.error(f"ProductSearchService.search error: {e}", exc_info=True)
            return None, []


product_search_svc = ProductSearchService()


# ==================== NEAREST STORE SERVICE (demo) ====================

_STORE_LOCATIONS_AVAILABLE = False
try:
    from fashion_bot.utils.store_locations import afind_nearest_store, aget_all_stores
    _STORE_LOCATIONS_AVAILABLE = True
except ImportError:
    pass

NEAREST_STORE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_nearest_store",
        "description": (
            "Find the nearest physical/offline store for this brand based on a "
            "6-digit Indian pincode. Use when: a product/variant is "
            "out of stock, the customer is confused about size/fit/material and "
            "might benefit from trying in-store, or the customer asks about "
            "store/office/warehouse location or brand authenticity."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pincode": {
                    "type": "string",
                    "description": "Customer's 6-digit Indian pincode (e.g. '411038')",
                }
            },
            "required": ["pincode"],
        },
    },
}


async def _resolve_store_client_id(
    client_id: Optional[str] = None, domain: Optional[str] = None,
) -> Optional[str]:
    """Resolve the real Postgres client_id for store lookups.

    The demo router may receive a hash-based client_id or a real one.
    Stores are keyed by real client_id, so try the PG lookup first.
    """
    if client_id:
        stores = await aget_all_stores(client_id)
        if stores:
            logger.info(f"🏬 [store_resolve] found {len(stores)} stores for client_id={client_id[:8]}...")
            return client_id
        else:
            logger.debug(f"🏬 [store_resolve] no stores for client_id={client_id[:8]}...")
    if domain:
        pg_id = await aget_pg_client_id_for_domain(domain)
        if pg_id:
            stores = await aget_all_stores(pg_id)
            if stores:
                logger.info(f"🏬 [store_resolve] found {len(stores)} stores via domain={domain} → pg_id={pg_id[:8]}...")
                return pg_id
            else:
                logger.debug(f"🏬 [store_resolve] domain={domain} → pg_id={pg_id[:8]}... but no stores")
        else:
            logger.debug(f"🏬 [store_resolve] domain={domain} → no PG client_id found")
    return None


async def _demo_find_nearest_store(
    pincode: str,
    client_id: Optional[str] = None,
    domain: Optional[str] = None,
    history: Optional[List[Any]] = None,
    product_name: Optional[str] = None,
    product_url: Optional[str] = None,
) -> str:
    """Thin wrapper for demo chat — returns JSON string for the LLM tool response."""
    if not _STORE_LOCATIONS_AVAILABLE:
        return json.dumps({"success": False, "message": "Store locations not available."})

    cid = await _resolve_store_client_id(client_id, domain)
    if not cid:
        return json.dumps({"success": False, "message": "Store locations not available."})

    val = pincode.strip()
    results = await afind_nearest_store(
        cid,
        city=None,
        pincode=val,
        limit=2,
    )
    if not results:
        return json.dumps({"success": True, "message": "No stores found within 30 km of this location."})

    nearest = results[0]
    try:
        from fashion_bot.tool_factory import _anotify_agent_store_visit
        demo_state = {"client_id": cid, "phone_number": "917503014404"}
        recent_msgs = [
            m.content for m in (history or [])
            if getattr(m, "role", "") == "user"
        ][-3:]
        await _anotify_agent_store_visit(demo_state, cid, nearest, val, recent_user_messages=recent_msgs, product_name=product_name, product_url=product_url)
    except Exception as exc:
        logger.warning(f"🏬 [demo] store-visit WhatsApp notification failed: {exc}")

    return json.dumps({
        "success": True,
        "nearest_store": nearest,
        "other_stores": results[1:] if len(results) > 1 else [],
        "presentation_hint": (
            "ALWAYS include the google_maps_url link in your response "
            "so the customer can navigate to the store directly."
        ),
    }, default=str)


async def _demo_stores_available(client_id: Optional[str] = None, domain: Optional[str] = None) -> bool:
    if not _STORE_LOCATIONS_AVAILABLE:
        logger.info("🏬 [store_avail] store_locations module not imported")
        return False
    cid = await _resolve_store_client_id(client_id, domain)
    available = bool(cid)
    logger.info(f"🏬 [store_avail] client_id={client_id}, domain={domain}, resolved={cid}, available={available}")
    return available


MAX_PRODUCT_CARDS = 3


def _extract_shown_handles_from_history(history: Optional[List] = None) -> List[str]:
    """Extract product handles from URLs mentioned in previous assistant messages.

    Scans the chat history for ``/products/<handle>`` patterns in assistant
    replies so that "show more" queries can exclude already-shown products.
    """
    if not history:
        return []
    handles: list[str] = []
    seen: set[str] = set()
    for msg in history:
        if getattr(msg, "role", None) != "assistant":
            continue
        for m in re.finditer(r"/products/([A-Za-z0-9_-]+)", msg.content or ""):
            h = m.group(1)
            if h not in seen:
                seen.add(h)
                handles.append(h)
    return handles


def _format_product_for_chrome(product: Dict) -> Dict:
    """Convert a merged Upstash doc to the Chrome extension's card schema."""
    price = product.get("price_min") or product.get("price")
    compare_at = product.get("compare_at_price_min")

    discount = None
    if compare_at and price:
        try:
            p_val, c_val = float(price), float(compare_at)
            if c_val > p_val > 0:
                discount = f"{int(((c_val - p_val) / c_val) * 100)}% OFF"
        except (ValueError, TypeError):
            pass

    formatted_price = None
    if price:
        try:
            formatted_price = f"₹{int(float(price))}"
        except (ValueError, TypeError):
            formatted_price = str(price)

    formatted_compare = None
    if compare_at and discount:
        try:
            formatted_compare = f"₹{int(float(compare_at))}"
        except (ValueError, TypeError):
            pass

    return {
        "title": product.get("title", "Product"),
        "handle": product.get("handle", ""),
        "price": formatted_price,
        "originalPrice": formatted_compare,
        "discount": discount,
        "image": product.get("image_url") or product.get("image", ""),
        "available": product.get("in_stock", True),
        "url": product.get("product_url") or product.get("url", ""),
    }


# ==================== LLM SERVICE ====================

class EnhancedLLMService:
    """Enhanced LLM service with Shopify data injection and function calling"""
    
    def __init__(self):
        self.client = None
        self.async_client = None
        self.api_key = os.getenv("OPENROUTER_API_KEY")
        if self.api_key:
            self.client = OpenAI(api_key=self.api_key, base_url="https://openrouter.ai/api/v1")
            self.async_client = AsyncOpenAI(api_key=self.api_key, base_url="https://openrouter.ai/api/v1")
    
    def build_prompt_with_data(self, context: PageContext, shopify_data: Dict,
                               recent_products: Optional[List[RecentProduct]] = None) -> str:
        """Build system prompt with page context, variant data, and policies.

        Product discovery/search is handled via function calling (search_products
        tool) so search results are NOT injected into the prompt.
        """
        store_name = context.domain.replace('.com', '').replace('.in', '').replace('www.', '').title()
        is_on_product_page = context.product and context.product.name

        prompt = f"""You are a friendly shopping assistant for {store_name}. You answer product questions from page context and help discover products via the search_products tool.

QUERY TYPES:
A) PRODUCT DETAILS — user asks about a SPECIFIC product already on the page (e.g., "what's the price?", "what sizes?", "tell me about this product"). Answer from PAGE CONTEXT below. Do NOT call search_products.
B) DISCOVERY / RECOMMENDATIONS — user wants to BROWSE, EXPLORE, or see product options (e.g., "show me X", "I want X", "recommend X", "any X available?"). MUST call search_products with the user's raw query. Never answer from page context for these — even if the page lists similar products. Never invent products or URLs.

CRITICAL: If the user's message is a product TYPE or CATEGORY request (like "show me bodysuits", "I need bras", "jeans", "shirts"), it is ALWAYS Type B. ALWAYS call search_products. Do NOT use page text to answer product browsing requests.

PRODUCT DETAIL RULES:
- Answer only what was asked, concisely.
- State info directly ("Delivery takes 3-5 days"), never reference the page.
- No discount or save ₹0 → "No active discounts right now." Never say "you save ₹0".
- Unknown info → share what you DO know positively. Never say "I don't have that information".
- Never invent specs. Use only what's in the context.
- Always mention prices in ₹ (Indian Rupees) format.

SEARCH / RECOMMENDATION RULES:
- Pass the user's query as-is to search_products. The tool handles query understanding internally.
- Curate and present the TOP 3 most relevant products from the tool results.
- Only show products from the same type family the user asked for. Discard mismatches.
  Type families: T-shirts/Tees/Polos | Shirts/Formal/Casual | Hoodies/Sweatshirts | Jackets/Blazers/Outerwear | Jeans/Denim | Trousers/Pants/Chinos/Joggers | Shorts/Jorts | Dresses | Co-ord Sets
- If none match, tell the user and offer what was found.
- Climate awareness: tropical→lightweight; cold→layers; formal→trousers+shirts; party→statement pieces.
- If the tool returns a suggested follow-up question, ask it to the user at the end of your response.

COLOR VARIANTS:
When the user asks for "more colors", "other colors", "different color", or "does this come in [color]?" for a product, call search_products with the product's key attributes (type, fit, material, occasion) but WITHOUT the current color. This finds the same product in other colors.
Example: User is viewing "Men Blue Slim Fit Solid Cotton Full Sleeve Shirt" and asks "more colors" → search_products(query="slim fit solid cotton full sleeve shirt")
From the results, EXCLUDE the product in the color the user already has and present the remaining options.

SIMILAR PRODUCTS — ATTRIBUTE DISCOVERY:
When the user asks for "similar products", "products like this", "more like this", "show me similar", "more shirts/jeans/etc. like this":
- Call search_products with a BROAD query using the product's type/subcategory ONLY (e.g., "casual shirt", "jeans"). Do NOT include all attributes (color, fit, pattern, material) from the current product — this over-constrains results and returns near-identical items instead of a diverse similar set.
- Present the initial results.
- Ask a follow-up referencing the current product's ACTUAL attributes: "Would you like me to find [product type] with a similar color ([actual color]), fit ([actual fit]), pattern ([actual pattern]), or material ([actual material])?"
- When the user responds with specific preferences, refine the search with ONLY those attributes.
  Example flow:
  User (viewing Blue Slim Fit Cotton Casual Shirt): "Show me similar products"
  → search_products(query="casual shirt") → show 3 results
  → "Would you like me to find shirts with a similar color (blue), fit (slim fit), pattern (solid), or material (cotton)?"
  User: "Same fit and material"
  → search_products(query="slim fit cotton casual shirt") → show refined results

POST-RETRIEVAL ATTRIBUTE FILTERING (CRITICAL):
After receiving results from search_products, review EACH product for attribute relevance before presenting:
- If the user asked for a specific attribute (e.g., "padded bras", "slim fit jeans", "cotton shirts", "formal trousers"), verify each returned product actually has that attribute in its title, description, or metadata.
- EXCLUDE products that contradict the user's specified attributes (e.g., non-padded bras when user asked for padded, regular fit when user asked for slim fit, polyester when user asked for cotton).
- Only present products that genuinely match ALL of the user's stated requirements.
- If fewer than 3 products remain after filtering, present what you have rather than including irrelevant ones.
- If NO products match after filtering, inform the user that exact matches weren't found and suggest refining the search.

"SHOW MORE" vs REFINEMENT — DEDUPLICATION:
- When the user says "show more", "more options", "other products", "what else" → pick DIFFERENT products from the search results that were NOT already shown in the conversation. Avoid repeating the same product names.
- When the user REFINES a previous search (e.g., adds "slim fit", "under 2000", "in black") → it is OK to show products that were previously shown IF they match the refined criteria. The user is narrowing down, not asking for new options.

PRODUCT SELECTION — PERSONALIZED PITCH:
When the user narrows down on a specific product after seeing recommendations (e.g., "tell me more about the first one", "I like that suit", "what about the Park Avenue one?"), respond with a short personalized pitch (under 50 words) explaining why this product is perfect for THEM — referencing their stated occasion, preferences, style, or context from the conversation. Do NOT just repeat specs. Connect the product's attributes to what the user actually asked for.
Use the product URL from the RECENTLY SHOWN PRODUCTS section (if present) or from conversation history. NEVER output placeholder text like "[Insert Product URL]" — if you don't have the URL, simply omit it.

ENGAGEMENT NUDGE — PROACTIVE FOLLOW-UP:
When the user sends a short acknowledgment message (e.g., "ok", "thanks", "thank you", "got it", "cool", "alright", "great", "nice", "sure", "bye") AND the recent conversation involved product recommendations:

SCENARIO A — User showed interest in ONE specific product before acknowledging. This includes: asking about its price, discount, sizes, material, details, or saying "I like that", "tell me more about the first one", "what about the Park Avenue one?", etc.
- Give a short, enthusiastic closing pitch (under 40 words) explaining why this product is a great fit for them based on their preferences from the conversation.
- Include the product URL.
- Gently nudge to purchase: "Ready to make it yours?" or "Grab it before it's gone!"
- Do NOT call search_products.

SCENARIO B — User saw product recommendations but did NOT ask about or focus on any single product, then acknowledged:
- MUST call search_products with query: "bestselling [subcategory ONLY — no occasion/style qualifiers]"
  Example: if user was browsing casual shirts → search_products(query="bestselling shirts") (NOT "bestselling casual shirts")
  Example: if user was browsing slim fit jeans → search_products(query="bestselling jeans") (NOT "bestselling slim fit jeans")
  The bestseller pool is small — qualifiers over-constrain results.
- Present the results with a generic intro: "Before you go, check out our bestsellers that you may like!" Do NOT mention the specific occasion or style (e.g., do NOT say "best-selling casual shirts" or "best-selling formal shirts").
- Show products with names, prices, and URLs.

IMPORTANT:
- Only nudge when the conversation recently involved product browsing or recommendations.
- Do NOT nudge after non-product conversations (order status, delivery, returns, policies).
- Do NOT nudge if there is no product context in the conversation yet.
- "bestselling" is a NUDGE-ONLY concept. When the user responds to a nudge with a follow-up query (e.g., "slim fit", "show me more", "under 2000"), treat it as a NORMAL product search. Do NOT carry "bestselling" into subsequent queries — drop it entirely from the search query.

NEAREST STORE RULES (only when get_nearest_store tool is available):
- When a product/variant is out of stock, size/fit/material is confusing, or the customer asks about a physical store/office/warehouse/authenticity, ask for their pincode and call get_nearest_store.
- Only ask for pincode (6-digit), not city.
- Present the store with: name, address, phone, manager contact, hours, and the google_maps_url from the tool result.
- CRITICAL: You MUST always include the google_maps_url link from the tool result so the customer can navigate directly. Never omit this link or say you don't have it.
- If no store is found within range, say so and offer to help online instead.

OUTPUT: concise, conversational, plain URLs (no markdown links), numbered lists for multiple products, emojis sparingly. Never fabricate data. NEVER include image references or markdown image syntax (e.g., ![alt](url) or !ProductName) — images are handled separately by the system.
"""
        
        if is_on_product_page:
            prompt += f"""
CONTEXT: The user is viewing a SPECIFIC PRODUCT: "{context.product.name}"
When they ask questions about this product, answer from PAGE CONTEXT.
- "What's the price?" → Use the CURRENT PAGE PRODUCT section below (the displayed selling price). NOT the per-variant prices from VARIANT DATA.
- "Any discount?" → Check CURRENT PAGE PRODUCT for Original Price / Discount. Also check the PAGE TEXT for MRP, discount %, "OFF" mentions. If the product has an Original Price higher than the current Price, it IS on sale — mention the discount.
- "Tell me about this" → Describe THIS product
- "What sizes?" → Use the PRODUCT VARIANT DATA below (source of truth for availability)
Only call search_products when the user explicitly asks for other/similar/new products.
"""

        current_product_json = shopify_data.get("current_product")
        if current_product_json and is_on_product_page:
            title = current_product_json.get("title", "")
            variants = current_product_json.get("variants", [])

            if variants:
                available_variants = [v for v in variants if v.get("available")]
                unavailable_variants = [v for v in variants if not v.get("available")]

                prompt += f"\n--- PRODUCT VARIANT DATA (source of truth for sizes & stock) ---\n"
                prompt += f"Product: {title}\n"

                if available_variants:
                    in_stock_titles = [v.get("title", v.get("option1", "")) for v in available_variants]
                    prompt += f"In-stock sizes: {', '.join(in_stock_titles)}\n"
                else:
                    prompt += "All variants are currently OUT OF STOCK.\n"

                if unavailable_variants:
                    out_titles = [v.get("title", v.get("option1", "")) for v in unavailable_variants]
                    prompt += f"Out-of-stock sizes: {', '.join(out_titles)}\n"

                prompt += "Use this variant data ONLY for size/availability questions. "
                prompt += "Do NOT use any prices from this section. "
                prompt += "Ignore page text for stock status (Shopify buttons can be misleading).\n\n"

        if context.product and context.product.name:
            prompt += f"\n--- CURRENT PAGE PRODUCT (source of truth for price & discount) ---\n"
            prompt += f"Name: {context.product.name}\n"

            product_price = context.product.price
            original_price = context.product.originalPrice
            discount_text = context.product.discount
            logger.info(
                f"Product price fields from extension: "
                f"price={product_price}, originalPrice={original_price}, discount={discount_text}"
            )

            if context.pageText:
                combo = re.search(
                    r'₹\s*([\d,]+)\s*MRP\s*₹?\s*([\d,]+)\s*(\d+%)\s*OFF',
                    context.pageText, re.IGNORECASE,
                )
                if combo:
                    product_price = f"₹{combo.group(1)}"
                    original_price = f"₹{combo.group(2)}"
                    discount_text = combo.group(3) + " OFF"
                else:
                    mrp_match = re.search(r'MRP[:\s]*₹?\s*([\d,]+)', context.pageText)
                    off_match = re.search(r'(\d+%)\s*OFF', context.pageText, re.IGNORECASE)
                    if mrp_match:
                        original_price = f"₹{mrp_match.group(1)}"
                    if off_match:
                        discount_text = off_match.group(1) + " OFF"

            logger.info(
                f"Final price fields for prompt: "
                f"price={product_price}, originalPrice={original_price}, discount={discount_text}"
            )

            if product_price:
                prompt += f"Selling Price: {product_price}\n"
            if original_price:
                prompt += f"Original Price (MRP): {original_price}\n"
            if discount_text:
                prompt += f"Discount: {discount_text}\n"
            if original_price and not discount_text and product_price:
                prompt += f"Note: This product IS on sale (selling below MRP).\n"

            if context.product.description:
                prompt += f"Description: {context.product.description[:500]}\n"
            if context.product.imageAlt:
                prompt += f"Image Context: {context.product.imageAlt}\n"
            if context.product.imageDescriptions:
                prompt += f"Additional Image Info: {', '.join(context.product.imageDescriptions)}\n"
            if context.product.attributes:
                prompt += f"Attributes: {json.dumps(context.product.attributes, indent=2)}\n"
        
        if context.pageText:
            page_text = context.pageText[:30000]
            page_text = re.sub(
                r'=== PRODUCT STRUCTURED DATA \(JSON-LD\) ===.*',
                '', page_text, flags=re.DOTALL,
            ).strip()
            prompt += f"\n--- PAGE TEXT ---\n{page_text}\n--- END PAGE TEXT ---\n"
            logger.info(f"Page text: {len(context.pageText)} raw -> {len(page_text)} sent")

        if shopify_data.get("policies"):
            prompt += f"\n--- STORE POLICIES ---\n"
            for policy_name, policy_text in shopify_data["policies"].items():
                prompt += f"\n{policy_name.replace('-', ' ').title()}:\n{policy_text[:500]}\n"

        if recent_products:
            prompt += "\n--- RECENTLY SHOWN PRODUCTS (from last recommendation) ---\n"
            for i, rp in enumerate(recent_products, 1):
                line = f"{i}. {rp.title or 'Unknown'}"
                if rp.price:
                    line += f" — {rp.price}"
                if rp.url:
                    line += f" — {rp.url}"
                prompt += line + "\n"
            prompt += "Use the URLs above when the user refers to a product by number (e.g. 'first one', 'second one', '#2').\n"

        prompt += "\n--- END CONTEXT ---\nRespond to the user using the above context. Be helpful and concise."

        logger.info(f"System prompt: {len(prompt)} chars")
        return prompt
    
    async def get_response(
        self,
        message: str,
        context: PageContext,
        history: List[ChatMessage] = None,
        client_id: Optional[str] = None,
        shopify_data: Dict = None,
        recent_products: Optional[List[RecentProduct]] = None,
    ) -> Tuple[str, Optional[List[Dict]]]:
        """Get LLM response using function calling for product search.

        Returns (reply_text, recent_products) where recent_products is a list of
        full merged Upstash docs, or None when no search was performed.
        """
        if not self.client:
            return self._fallback_response(message, context, shopify_data), []

        try:
            system_prompt = self.build_prompt_with_data(context, shopify_data or {}, recent_products=recent_products)

            messages = [{"role": "system", "content": system_prompt}]
            if history:
                for msg in history[-20:]:
                    messages.append({"role": msg.role, "content": msg.content})

            last_in_history = (
                history and history[-1].role == "user"
                and history[-1].content.strip() == message.strip()
            )
            if not last_in_history:
                messages.append({"role": "user", "content": message})

            tools = []
            if await product_search_svc.is_available(client_id=client_id, domain=context.domain):
                tools.append(ProductSearchService.SEARCH_TOOL_SCHEMA)
            if await _demo_stores_available(client_id, domain=context.domain):
                tools.append(NEAREST_STORE_TOOL_SCHEMA)
            tools = tools or None

            create_kwargs = dict(model=_LLM_MODEL, messages=messages, max_tokens=600, temperature=0.7)
            if tools:
                create_kwargs["tools"] = tools

            response = self.client.chat.completions.create(**create_kwargs)

            choice = response.choices[0]

            search_recent_products: List[Dict] = []

            if choice.message.tool_calls:
                messages.append(choice.message)

                for tool_call in choice.message.tool_calls:
                    args = json.loads(tool_call.function.arguments)

                    if tool_call.function.name == "search_products":
                        logger.info(f"LLM invoked search_products(query='{args.get('query')}')")

                        result, cards = await product_search_svc.search(
                            query=args.get("query", message),
                            client_id=client_id,
                            domain=context.domain,
                            product_context=context.product,
                            history=history,
                        )
                        search_recent_products = cards
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result or "No products found matching your criteria.",
                        })

                    elif tool_call.function.name == "get_nearest_store":
                        logger.info(f"🏬 LLM invoked get_nearest_store(pincode='{args.get('pincode')}')")
                        _pname = context.product.name if context and context.product else None
                        _purl = context.url if context else None
                        store_result = await _demo_find_nearest_store(
                            args.get("pincode", ""), client_id,
                            domain=context.domain, history=history,
                            product_name=_pname, product_url=_purl,
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": store_result,
                        })

                final = self.client.chat.completions.create(
                    model=_LLM_MODEL, messages=messages, max_tokens=600, temperature=0.7,
                )

                reply_text = final.choices[0].message.content or ""
                matched = match_products_to_reply(search_recent_products, reply_text, MAX_PRODUCT_CARDS)
                chrome_cards = [_format_product_for_chrome(p) for p in matched]
                return reply_text, chrome_cards

            return choice.message.content, []

        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            return self._fallback_response(message, context, shopify_data), []

    async def get_response_streaming(
        self, 
        message: str, 
        context: PageContext, 
        history: List[ChatMessage] = None,
        client_id: Optional[str] = None,
        shopify_data: Dict = None,
        metadata: Dict = None,
        recent_products: Optional[List[RecentProduct]] = None,
    ):
        """Async generator yielding SSE events: ``event: token`` then ``event: done``.

        Uses AsyncOpenAI so the first (non-streaming) call with function
        calling and the search pipeline can all be awaited without blocking
        the event loop.
        """
        if not self.async_client:
            fallback = self._fallback_response(message, context, shopify_data)
            yield f"event: token\ndata: {json.dumps({'content': fallback})}\n\n"
            yield f"event: done\ndata: {json.dumps({'products': [], 'metadata': metadata, 'action': None})}\n\n"
            return

        try:
            system_prompt = self.build_prompt_with_data(context, shopify_data or {}, recent_products=recent_products)
            
            messages = [{"role": "system", "content": system_prompt}]
            if history:
                for msg in history[-20:]:
                    messages.append({"role": msg.role, "content": msg.content})

            last_in_history = (
                history and history[-1].role == "user"
                and history[-1].content.strip() == message.strip()
            )
            if not last_in_history:
                messages.append({"role": "user", "content": message})
                
            tools = []
            if await product_search_svc.is_available(client_id=client_id, domain=context.domain):
                tools.append(ProductSearchService.SEARCH_TOOL_SCHEMA)
            if await _demo_stores_available(client_id, domain=context.domain):
                tools.append(NEAREST_STORE_TOOL_SCHEMA)
            tools = tools or None

            create_kwargs = dict(model=_LLM_MODEL, messages=messages, max_tokens=600, temperature=0.7)
            if tools:
                create_kwargs["tools"] = tools

            first_response = await self.async_client.chat.completions.create(**create_kwargs)

            choice = first_response.choices[0]

            search_recent_products: List[Dict] = []

            if choice.message.tool_calls:
                yield f"event: thinking\ndata: {json.dumps({'status': 'searching'})}\n\n"
                messages.append(choice.message)

                for tool_call in choice.message.tool_calls:
                    args = json.loads(tool_call.function.arguments)

                    if tool_call.function.name == "search_products":
                        logger.info(f"[stream] LLM invoked search_products(query='{args.get('query')}')")

                        result, cards = await product_search_svc.search(
                            query=args.get("query", message),
                            client_id=client_id,
                            domain=context.domain,
                            product_context=context.product,
                            history=history,
                        )
                        search_recent_products = cards
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result or "No products found matching your criteria.",
                        })

                    elif tool_call.function.name == "get_nearest_store":
                        logger.info(f"🏬 [stream] LLM invoked get_nearest_store(pincode='{args.get('pincode')}')")
                        _pname = context.product.name if context and context.product else None
                        _purl = context.url if context else None
                        store_result = await _demo_find_nearest_store(
                            args.get("pincode", ""), client_id,
                            domain=context.domain, history=history,
                            product_name=_pname, product_url=_purl,
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": store_result,
                        })

                stream = await self.async_client.chat.completions.create(
                    model=_LLM_MODEL, messages=messages, max_tokens=600,
                    temperature=0.7, stream=True,
                )

                full_reply = ""
                async for chunk in stream:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta and delta.content:
                        full_reply += delta.content
                        yield f"event: token\ndata: {json.dumps({'content': delta.content})}\n\n"

                action = "order_placed" if "Order Placed" in full_reply else None
                matched = match_products_to_reply(search_recent_products, full_reply, MAX_PRODUCT_CARDS)
                chrome_cards = [_format_product_for_chrome(p) for p in matched]
                yield f"event: done\ndata: {json.dumps({'products': chrome_cards, 'metadata': metadata, 'action': action})}\n\n"

            else:
                content = choice.message.content or ""
                if content:
                    yield f"event: token\ndata: {json.dumps({'content': content})}\n\n"
                action = "order_placed" if "Order Placed" in content else None
                yield f"event: done\ndata: {json.dumps({'products': [], 'metadata': metadata, 'action': action})}\n\n"
                
        except Exception as e:
            logger.error(f"OpenAI streaming error: {e}")
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"
    
    def _initiate_order_response(self, product_name: str, price: str = None) -> str:
        """Ask for customer details to place order"""
        response = f"""🛒 **Great choice!** You want to order **{product_name}**"""
        if price:
            response += f" for **{price}**"
        response += """

To complete your order, please provide your details:

📝 **Required Information:**
1. **Name**: Your full name
2. **Phone**: Mobile number (for delivery updates)
3. **Address**: Complete delivery address with pincode

Just type your details like this:
*Name: John Doe, Phone: 9876543210, Address: 123 Main Street, Mumbai 400001*

Or you can fill the form below! 👇"""
        return response
    
    def _extract_order_details(self, message: str) -> Optional[Dict]:
        """Extract customer details from message"""
        # Look for patterns like "Name: X, Phone: Y, Address: Z"
        name_match = re.search(r'name\s*[:=]\s*([^,\n]+)', message, re.IGNORECASE)
        phone_match = re.search(r'phone\s*[:=]\s*(\d{10})', message, re.IGNORECASE)
        address_match = re.search(r'address\s*[:=]\s*([^,\n]+(?:,\s*[^,\n]+)*)', message, re.IGNORECASE)
        
        # Also try to find phone number anywhere in message
        if not phone_match:
            phone_match = re.search(r'\b(\d{10})\b', message)
        
        if name_match and phone_match and address_match:
            return {
                "name": name_match.group(1).strip(),
                "phone": phone_match.group(1).strip(),
                "address": address_match.group(1).strip()
            }
        return None
    
    def _complete_order(self, product_name: str, price: str, customer_info: Dict) -> str:
        """Complete the order with customer details"""
        # Create order in store
        order = order_store.complete_order(
            session_id=customer_info.get("phone", "unknown"),
            customer_info=customer_info
        )
        
        if not order:
            # Create a new order directly
            order_id = f"DEMO-{datetime.now().strftime('%Y%m%d%H%M%S')}-{len(order_store._orders) + 1:03d}"
            order = {
                "order_id": order_id,
                "product": {"name": product_name, "price": price},
                "customer": customer_info,
                "status": "confirmed",
                "status_history": [
                    {"status": "confirmed", "timestamp": datetime.now().isoformat(), "message": "Order confirmed"}
                ],
                "created_at": datetime.now().isoformat(),
                "estimated_delivery": (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
            }
            order_store._orders[order_id] = order
            if customer_info.get("phone"):
                order_store._orders[f"phone:{customer_info['phone']}"] = order_id
        
        response = f"""🎉 **Order Placed Successfully!**

**Order ID:** `{order['order_id']}`
**Product:** {product_name}
**Amount:** {price or 'As per website'}

📦 **Delivery Details:**
**Name:** {customer_info['name']}
**Phone:** {customer_info['phone']}
**Address:** {customer_info['address']}

**Status:** ✅ Confirmed
**Expected Delivery:** {order['estimated_delivery']}

---
💡 **Save your Order ID** to track your order!
Type "track order {order['order_id']}" anytime to check status.

⚠️ *This is a demo order. No actual transaction was made.*"""
        return response
    
    def _order_status_response(self, message: str) -> str:
        """Handle order status queries"""
        # Try to extract order ID from message
        order_id_match = re.search(r'DEMO-\d{14}-\d{3}', message.upper())
        phone_match = re.search(r'\b\d{10}\b', message)
        
        order = None
        if order_id_match:
            order = order_store.get_order(order_id_match.group())
        elif phone_match:
            order = order_store.get_order_by_phone(phone_match.group())
        
        if order:
            status_emoji = {
                "confirmed": "✅",
                "processing": "📦",
                "shipped": "🚚",
                "out_for_delivery": "🛵",
                "delivered": "🎉"
            }.get(order["status"], "📋")
            
            response = f"""{status_emoji} **Order Status**

**Order ID:** {order['order_id']}
**Product:** {order['product'].get('name', 'N/A')}
**Amount:** {order['product'].get('price', 'N/A')}

**Current Status:** {order['status'].replace('_', ' ').title()}
**Ordered On:** {order['created_at'][:10]}
**Expected Delivery:** {order['estimated_delivery']}

**Delivery To:**
{order['customer'].get('name', 'N/A')}
{order['customer'].get('address', 'N/A')}
📞 {order['customer'].get('phone', 'N/A')}

**Status History:**"""
            for entry in order.get("status_history", [])[-3:]:
                response += f"\n• {entry['status'].replace('_', ' ').title()} - {entry['timestamp'][:16]}"
            
            return response
        else:
            return """📋 **Check Your Order Status**

I couldn't find an order with those details. Please provide:

• **Order ID** (e.g., DEMO-20260109123456-001)
  OR
• **Phone Number** used during order (10 digits)

Example: *"Track order DEMO-20260109123456-001"* or *"Order status for 9876543210"*

💡 If you haven't placed an order yet, say "I want to buy this" to start!"""
    
    def _fallback_response(self, message: str, context: PageContext, shopify_data: Dict = None) -> str:
        """Fallback when LLM unavailable"""
        message_lower = message.lower()
        
        # Price query
        if any(w in message_lower for w in ['price', 'cost', 'how much', "what's the price", "what is the price"]):
            if context.product and context.product.price:
                resp = f"💰 The price is **{context.product.price}**"
                if context.product.originalPrice:
                    resp += f" (Original: {context.product.originalPrice})"
                if context.product.discount:
                    resp += f"\n🏷️ **{context.product.discount}**"
                return resp
            elif shopify_data and shopify_data.get("products"):
                products = shopify_data["products"][:3]
                store_url = f"https://{context.domain}"
                resp = "Here are some products with their prices:\n\n"
                for p in products:
                    resp += ProductFormatter.format_product_summary(p, store_url) + "\n\n"
                return resp
            elif context.product and context.product.name:
                return f"💰 I can see **{context.product.name}** on this page, but the price isn't available. Please check the website for current pricing."
        
        # Product details query
        if any(w in message_lower for w in ['tell me', 'details', 'about this', 'more about', 'info', 'information']):
            if context.product and context.product.name:
                resp = f"📦 **{context.product.name}**\n\n"
                if context.product.description:
                    resp += f"{context.product.description[:300]}\n\n"
                if context.product.price:
                    resp += f"💰 Price: {context.product.price}\n"
                if context.product.attributes:
                    resp += f"\n**Specifications:**\n"
                    for key, value in list(context.product.attributes.items())[:5]:
                        resp += f"• {key.title()}: {value}\n"
                return resp
        
        # Availability query
        if any(w in message_lower for w in ['available', 'stock', 'size', 'in stock']):
            if context.product and context.product.name:
                return f"✅ **{context.product.name}** appears to be available on this page! Check the website for size options and current stock status."
        
        # If we have product info, use it
        if context.product and context.product.name:
            return f"I can see **{context.product.name}** on this page. {f'Price: {context.product.price}' if context.product.price else ''} How can I help you with this product?"
        
        # Default
        return f"I can help you with products on {context.domain}. Try asking about:\n• Product prices\n• Size availability\n• Current offers\n• Store policies"

llm_service = EnhancedLLMService()

# ==================== MAIN HANDLER ====================

async def _prepare_chat_context(request: ChatRequest, location: Optional[Dict] = None) -> Dict:
    """Pre-LLM work: order short-circuits, JSON-LD variant data, and policies.

    Product search/discovery is handled by the LLM via function calling
    (search_products tool) so this function no longer runs search or
    intent detection for search decisions.

    Returns a dict with:
      - shopify_data  (current_product, policies)
      - metadata
      - Optional early_reply for order intents
    """
    store_url = ShopifyFetcher.get_store_url(request.context.domain)

    shopify_data: Dict = {}
    metadata = {
        "store_url": store_url,
        "domain": request.context.domain,
    }
    if location:
        metadata["location"] = location
        shopify_data["client_location"] = {
            key: location.get(key)
            for key in ("city", "pincode", "state", "country", "country_code", "latitude", "longitude")
            if location.get(key) is not None
        }

    # -- Order intents that short-circuit the LLM call --
    message_lower = request.message.lower()
    kw_intent, _ = IntentDetector.detect(request.message)

    is_status_query = (
        kw_intent == "order_status"
        or "status" in message_lower
        or "track" in message_lower
        or "where is my order" in message_lower
        or "my order" in message_lower
        or re.search(r'demo-\d{14}-\d{3}', message_lower)
    )
    if is_status_query:
        return {
            "early_reply": llm_service._order_status_response(request.message),
            "shopify_data": shopify_data,
            "metadata": metadata,
        }

    if kw_intent == "order":
        is_product_page = '/products/' in (request.context.url or '') or '/product/' in (request.context.url or '')
        refers_to_this = any(w in message_lower for w in [
            'this', 'it', 'checkout', 'place order', 'order now', 'add to cart',
        ])
        product_name = request.context.product.name if request.context.product else request.context.title or "this product"
        price = request.context.product.price if request.context.product else None
        if is_product_page and (refers_to_this or llm_service._extract_order_details(request.message)):
            order_details = llm_service._extract_order_details(request.message)
            if order_details:
                return {
                    "early_reply": llm_service._complete_order(product_name, price, order_details),
                    "shopify_data": shopify_data,
                    "metadata": metadata,
                }
            else:
                return {
                    "early_reply": llm_service._initiate_order_response(product_name, price),
                    "shopify_data": shopify_data,
                    "metadata": metadata,
                }

    # -- Current product JSON-LD (variant-level stock data) --
    if request.context.product and request.context.product.name:
        current_product = _build_current_product_from_jsonld(request.context)
        if current_product:
            shopify_data["current_product"] = current_product
            logger.info(
                f"Built current_product from JSON-LD: "
                f"{len(current_product.get('variants', []))} variants"
            )

    # -- Policies (keyword-based detection) --
    if kw_intent == "policy":
        try:
            policies = await ShopifyFetcher.fetch_policies(store_url)
            if policies:
                shopify_data["policies"] = policies
        except Exception as e:
            logger.warning(f"Policy fetch error: {e}")

    logger.info(f"shopify_data keys before LLM: {list(shopify_data.keys())}")

    return {
        "shopify_data": shopify_data,
        "metadata": metadata,
    }


async def process_chat_with_shopify(
    request: ChatRequest,
    location: Optional[Dict] = None,
) -> Tuple[str, Optional[List[Dict]], Dict]:
    """Prepare context + call LLM with function calling."""

    ctx = await _prepare_chat_context(request, location=location)

    if ctx.get("early_reply"):
        return ctx["early_reply"], None, ctx["metadata"]
    
    reply, products = await llm_service.get_response(
        message=request.message,
        context=request.context,
        history=request.history,
        client_id=request.clientId,
        shopify_data=ctx["shopify_data"],
        recent_products=request.recentProducts,
    )
    
    logger.info(f"LLM returned {len(products) if products else 0} products for carousel")
    
    return reply, products, ctx["metadata"]


async def _set_demo_request_location(http_request: Request, request: ChatRequest) -> Dict:
    browser_location = request.clientLocation.dict(exclude_none=True) if request.clientLocation else None
    location = await resolve_client_location_for_demo(browser_location=browser_location)
    backend_ip = extract_client_ip(http_request)
    location["backend_ip"] = backend_ip
    if location.get("public_ip"):
        location["ip"] = location.get("public_ip")
    http_request.state.location = location
    logger.info(
        "Demo request state location set: city=%s pincode=%s lat=%s lon=%s ip=%s backend_ip=%s source=%s",
        location.get("city"),
        location.get("pincode"),
        location.get("latitude"),
        location.get("longitude"),
        location.get("ip"),
        location.get("backend_ip"),
        location.get("source"),
    )
    return location

# ==================== ENDPOINTS ====================

@router.get("/health")
async def demo_health():
    vector_service = get_vector_service()
    return {
        "status": "healthy",
        "service": "demo-chat-enhanced",
        "features": ["shopify_runtime", "intent_detection", "recent_products", "vector_search"],
        "llm_available": llm_service.client is not None,
        "vector_service_available": vector_service is not None,
        "ingested_domains_count": len(INGESTED_DOMAINS),
        "debug_mode": DebugLogger.DEBUG_MODE,
        "timestamp": datetime.now().isoformat()
    }


@router.post("/debug")
async def toggle_debug(enable: bool = True):
    """
    Toggle debug mode for verbose console logging.
    
    Usage:
    - POST /demo/debug?enable=true  - Enable debug mode
    - POST /demo/debug?enable=false - Disable debug mode
    
    When enabled, detailed logs are printed for:
    - Chat requests
    - LLM API calls
    - Page text content
    - System prompts
    """
    if enable:
        DebugLogger.enable()
    else:
        DebugLogger.disable()
    
    return {
        "debug_mode": DebugLogger.DEBUG_MODE,
        "message": f"Debug mode {'enabled' if enable else 'disabled'}"
    }


@router.post("/chat", response_model=ChatResponse)
async def demo_chat(request: ChatRequest, http_request: Request):
    """
    Enhanced demo chat with Shopify runtime data fetching
    
    Features:
    - Intent detection (product search, pricing, availability, policies)
    - Real-time Shopify API data fetching
    - Product cards in response (NOT on product pages)
    - Smart caching (2 minute TTL)
    """
    logger.info(f"Demo chat from {request.context.domain}: {request.message[:50]}...")

    from fashion_bot.monitoring.otel_metrics import set_request_client_id
    set_request_client_id(request.clientId)

    # ==================== DEBUG: What's in the request? ====================
    logger.info("="*60)
    logger.info("📥 INCOMING REQUEST TO /demo/chat")
    logger.info(f"   - Message: {request.message}")
    logger.info(f"   - Domain: {request.context.domain}")
    logger.info(f"   - URL: {request.context.url[:80] if request.context.url else 'None'}...")
    logger.info(f"   - pageText exists: {bool(request.context.pageText)}")
    logger.info(f"   - pageText length: {len(request.context.pageText) if request.context.pageText else 0}")
    if request.context.pageText:
        logger.info(f"   - pageText first 300 chars: {request.context.pageText[:300]}")
    logger.info("="*60)
    
    try:
        location = await _set_demo_request_location(http_request, request)
        reply, _unused_products, metadata = await process_chat_with_shopify(request, location=location)
        
        action = None
        if "Order Placed" in (reply or ""):
            action = "order_placed"
        
        products_to_return = _unused_products if _unused_products else None
        logger.info(f"Returning {len(products_to_return) if products_to_return else 0} products to frontend")
        
        return ChatResponse(
            reply=reply or "",
            action=action,
            metadata=metadata,
            products=products_to_return
        )
        
    except Exception as e:
        logger.error(f"Demo chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat/stream")
async def demo_chat_stream(request: ChatRequest, http_request: Request):
    """SSE streaming variant of /demo/chat.

    Pre-LLM work (orders, JSON-LD, policies) runs first, then the LLM
    reply is streamed token-by-token as ``event: token`` SSE events.
    If the LLM calls the search_products tool, the search pipeline runs
    mid-stream and the final LLM response is streamed after.
    Products and metadata arrive in the final ``event: done`` event.
    """
    logger.info(f"[stream] Demo chat from {request.context.domain}: {request.message[:50]}...")

    from fashion_bot.monitoring.otel_metrics import set_request_client_id
    set_request_client_id(request.clientId)

    try:
        location = await _set_demo_request_location(http_request, request)
        ctx = await _prepare_chat_context(request, location=location)

        if ctx.get("early_reply"):
            async def _early():
                reply = ctx["early_reply"]
                yield f"event: token\ndata: {json.dumps({'content': reply})}\n\n"
                yield f"event: done\ndata: {json.dumps({'products': None, 'metadata': ctx['metadata'], 'action': None})}\n\n"
            return StreamingResponse(_early(), media_type="text/event-stream", headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            })

        gen = llm_service.get_response_streaming(
            message=request.message,
            context=request.context,
            history=request.history,
            client_id=request.clientId,
            shopify_data=ctx["shopify_data"],
            metadata=ctx["metadata"],
            recent_products=request.recentProducts,
        )

        return StreamingResponse(gen, media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    except Exception as e:
        logger.error(f"Demo chat stream error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/fetch-products")
async def fetch_store_products(store_url: str, limit: int = 20, ingest: bool = False):
    """
    Fetch products directly from a Shopify store.
    
    When ``ingest=true``, the endpoint:
      1. Creates (or reuses) a client row in PostgreSQL for this domain.
      2. Stores the website URL as ``website_urls`` in ``client_configs``.
      3. Calls ``ProductIngestionOrchestrator.ingest_products`` which ingests
         into **both** Upstash Search and Upstash VectorDB (via the public
         ``/products.json`` fallback, since no Shopify creds will exist).

    Args:
        store_url: Shopify store URL or domain
        limit: Maximum number of products to fetch (default: 20)
        ingest: If True, create client + ingest to Upstash Search & VectorDB
        
    Returns:
        Product list and optional ingestion status (with client_id)
    """
    try:
        store = ShopifyFetcher.get_store_url(store_url)
        
        products = await ShopifyFetcher.fetch_products(store, limit)
        
        if not products:
            return {"success": False, "error": "Could not fetch products"}
        
        result = {
            "success": True,
            "store": store,
            "count": len(products),
            "products": [ProductFormatter.format_product_card(p) for p in products],
        }
        
        if ingest:
            domain = _normalize_domain(store)
            
            ingestion_result = await _ensure_client_and_ingest(
                domain=domain,
                website_url=store,
            )
            
            result["ingestion"] = ingestion_result
            result["client_id"] = ingestion_result.get("client_id")
            
            if ingestion_result.get("success"):
                logger.info(
                    f"✅ Ingested {ingestion_result.get('products_ingested', 0)} "
                    f"products for {domain} (client {ingestion_result.get('client_id')})"
                )
            else:
                logger.warning(
                    f"⚠️ Ingestion failed for {domain}: {ingestion_result.get('error')}"
                )
        
        return result
        
    except Exception as e:
        logger.error(f"fetch-products error: {e}")
        return {"success": False, "error": str(e)}


async def _ensure_client_and_ingest(
    domain: str,
    website_url: str,
) -> Dict[str, Any]:
    """
    Create / reuse a PostgreSQL client for *domain*, persist its website URL,
    then run the full ProductIngestionOrchestrator pipeline (Upstash Search +
    VectorDB) using the public /products.json fallback.
    """
    import json as _json

    try:
        from fashion_bot.database_manager import get_async_postgres_connection
        from fashion_bot.services.product_ingestion import ProductIngestionOrchestrator

        # --- upsert client row ------------------------------------------------
        normalized = _normalize_domain(domain)
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT id FROM clients
                       WHERE TRIM(TRAILING '/' FROM
                             REPLACE(REPLACE(REPLACE(LOWER(domain),
                                     'https://',''), 'http://',''), 'www.','')
                             ) = %s
                       LIMIT 1""",
                    (normalized,),
                )
                row = await cur.fetchone()

                if row:
                    client_id = str(row["id"])
                    logger.info(f"♻️ Reusing existing client {client_id} for {domain}")
                else:
                    name = normalized.split(".")[0].title()
                    await cur.execute(
                        "INSERT INTO clients (name, domain) VALUES (%s, %s) RETURNING id",
                        (name, normalized),
                    )
                    client_id = str((await cur.fetchone())["id"])
                    logger.info(f"🆕 Created client {client_id} for {domain}")

                # --- upsert website_urls config --------------------------------
                await cur.execute(
                    "SELECT id FROM client_configs "
                    "WHERE client_id = %s AND config_key = 'website_urls'",
                    (client_id,),
                )
                config_row = await cur.fetchone()
                config_val = _json.dumps({"website_url": website_url})

                if config_row:
                    await cur.execute(
                        "UPDATE client_configs SET config_value = %s "
                        "WHERE client_id = %s AND config_key = 'website_urls'",
                        (config_val, client_id),
                    )
                else:
                    await cur.execute(
                        "INSERT INTO client_configs (client_id, config_key, config_value) "
                        "VALUES (%s, 'website_urls', %s)",
                        (client_id, config_val),
                    )

        # --- generate personalized prompts if missing -------------------------
        try:
            from fashion_bot.prompt_generator import ensure_personalized_prompts
            prompt_result = await ensure_personalized_prompts(client_id, domain)
            created = prompt_result.get("created", [])
            if created:
                logger.info(f"🧠 Generated prompts for {domain}: {created}")
        except Exception as e:
            logger.warning(f"⚠️ Prompt generation skipped for {domain}: {e}")

        # --- run full ingestion pipeline (Search + VectorDB) -------------------
        orchestrator = ProductIngestionOrchestrator()
        ing_result = await orchestrator.ingest_products(
            client_id=client_id,
            source="json",
            force_refresh=True,
            max_products=5000,
        )

        response = orchestrator.to_response(ing_result, client_id)
        resp_dict = response.model_dump()

        # Track in-memory for legacy /vector-search & /vector-status endpoints
        INGESTED_DOMAINS[client_id] = {
            "domain": domain,
            "ingested_at": datetime.now().isoformat(),
            "product_count": resp_dict.get("products_ingested", 0),
        }

        return resp_dict

    except Exception as e:
        logger.error(f"❌ _ensure_client_and_ingest failed for {domain}: {e}")
        return {"success": False, "error": str(e)}

@router.get("/fetch-collections")
async def fetch_store_collections(store_url: str):
    """
    Debug endpoint: Fetch collections from a Shopify store
    """
    try:
        store = ShopifyFetcher.get_store_url(store_url)
        collections = await ShopifyFetcher.fetch_collections(store)
        
        if collections:
            return {
                "success": True,
                "store": store,
                "collections": [{"title": c.get("title"), "handle": c.get("handle")} for c in collections]
            }
        return {"success": False, "error": "Could not fetch collections"}
    except Exception as e:
        return {"success": False, "error": str(e)}

@router.delete("/cache")
async def clear_cache():
    """Clear the Shopify data cache"""
    cache.clear()
    return {"success": True, "message": "Cache cleared"}

# ==================== VECTOR SEARCH ENDPOINTS ====================

@router.get("/vector-search")
async def vector_search(
    store_url: str,
    query: str,
    top_k: int = 10,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    segment: Optional[str] = None,
    in_stock_only: bool = False
):
    """
    Semantic product search using Upstash Vector.
    
    Args:
        store_url: Shopify store URL or domain
        query: Search query (natural language)
        top_k: Number of results to return (default: 10)
        min_price: Optional minimum price filter
        max_price: Optional maximum price filter
        segment: Optional segment filter (men, women, kids)
        in_stock_only: Only return in-stock products
        
    Returns:
        Search results with semantic similarity scores
    """
    try:
        # Normalize domain
        store = ShopifyFetcher.get_store_url(store_url)
        domain = _normalize_domain(store)
        client_id = get_client_id_for_domain(domain)
        
        # Check if domain is ingested
        if client_id not in INGESTED_DOMAINS:
            return {
                "success": False,
                "error": "Domain not ingested. Call /demo/fetch-products?ingest=true first.",
                "domain": domain,
                "client_id": client_id
            }
        
        # Perform vector search
        results = await vector_search_products(
            domain=domain,
            query=query,
            top_k=top_k,
            min_price=min_price,
            max_price=max_price,
            segment=segment,
            in_stock_only=in_stock_only
        )
        
        if results is None:
            return {
                "success": False,
                "error": "Vector search failed",
                "domain": domain
            }
        
        return {
            "success": True,
            "domain": domain,
            "client_id": client_id,
            "query": query,
            "count": len(results),
            "results": results
        }
        
    except Exception as e:
        logger.error(f"vector-search error: {e}")
        return {"success": False, "error": str(e)}

@router.get("/vector-status")
async def vector_status(store_url: Optional[str] = None):
    """
    Get vector DB status for a domain or all ingested domains.
    
    Args:
        store_url: Optional store URL to check specific domain
        
    Returns:
        Ingestion status and vector counts
    """
    try:
        vector_service = get_vector_service()
        
        result = {
            "success": True,
            "vector_service_available": vector_service is not None,
            "ingested_domains_count": len(INGESTED_DOMAINS)
        }
        
        if vector_service:
            try:
                index_info = vector_service.get_index_info()
                result["index_info"] = index_info
            except Exception as e:
                result["index_info_error"] = str(e)
        
        if store_url:
            # Check specific domain
            store = ShopifyFetcher.get_store_url(store_url)
            domain = _normalize_domain(store)
            client_id = get_client_id_for_domain(domain)
            
            result["domain"] = domain
            result["client_id"] = client_id
            result["is_ingested"] = client_id in INGESTED_DOMAINS
            
            if client_id in INGESTED_DOMAINS:
                result["ingestion_info"] = INGESTED_DOMAINS[client_id]
                
                # Get actual vector count
                if vector_service:
                    try:
                        count = vector_service.get_client_vector_count(client_id)
                        result["vector_count"] = count
                    except Exception as e:
                        result["vector_count_error"] = str(e)
        else:
            # Return all ingested domains
            result["ingested_domains"] = INGESTED_DOMAINS
        
        return result
        
    except Exception as e:
        logger.error(f"vector-status error: {e}")
        return {"success": False, "error": str(e)}

@router.delete("/vector-clear")
async def vector_clear(store_url: str):
    """
    Clear vector DB entries for a specific domain.
    
    Args:
        store_url: Shopify store URL or domain
        
    Returns:
        Number of vectors deleted
    """
    try:
        vector_service = get_vector_service()
        if not vector_service:
            return {"success": False, "error": "Vector service not available"}
        
        store = ShopifyFetcher.get_store_url(store_url)
        domain = _normalize_domain(store)
        client_id = get_client_id_for_domain(domain)
        
        # Delete from vector DB
        deleted_count = vector_service.delete_by_client(client_id)
        
        # Remove from tracked domains
        if client_id in INGESTED_DOMAINS:
            del INGESTED_DOMAINS[client_id]
        
        return {
            "success": True,
            "domain": domain,
            "client_id": client_id,
            "vectors_deleted": deleted_count
        }
        
    except Exception as e:
        logger.error(f"vector-clear error: {e}")
        return {"success": False, "error": str(e)}

# ==================== ORDER ENDPOINTS ====================

@router.post("/order/submit")
async def submit_order(
    product_name: str,
    product_price: Optional[str] = None,
    customer_name: str = "",
    customer_phone: str = "",
    customer_address: str = "",
    customer_city: Optional[str] = None,
    customer_pincode: Optional[str] = None
):
    """
    Submit a new order with customer details
    """
    if not customer_name or not customer_phone or not customer_address:
        raise HTTPException(status_code=400, detail="Name, phone, and address are required")
    
    if len(customer_phone) != 10 or not customer_phone.isdigit():
        raise HTTPException(status_code=400, detail="Phone must be 10 digits")
    
    customer_info = {
        "name": customer_name,
        "phone": customer_phone,
        "address": customer_address,
        "city": customer_city,
        "pincode": customer_pincode
    }
    
    # Create order
    order_id = f"DEMO-{datetime.now().strftime('%Y%m%d%H%M%S')}-{len(order_store._orders) + 1:03d}"
    order = {
        "order_id": order_id,
        "product": {"name": product_name, "price": product_price},
        "customer": customer_info,
        "status": "confirmed",
        "status_history": [
            {"status": "confirmed", "timestamp": datetime.now().isoformat(), "message": "Order confirmed"}
        ],
        "created_at": datetime.now().isoformat(),
        "estimated_delivery": (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
    }
    
    order_store._orders[order_id] = order
    order_store._orders[f"phone:{customer_phone}"] = order_id
    
    return {
        "success": True,
        "order": order,
        "message": f"Order {order_id} placed successfully!"
    }

@router.get("/order/status/{order_id}")
async def get_order_status(order_id: str):
    """
    Get order status by order ID
    """
    order = order_store.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    
    return {
        "success": True,
        "order": order
    }

@router.get("/order/lookup")
async def lookup_order_by_phone(phone: str):
    """
    Look up order by phone number
    """
    order = order_store.get_order_by_phone(phone)
    if not order:
        raise HTTPException(status_code=404, detail=f"No order found for phone {phone}")
    
    return {
        "success": True,
        "order": order
    }

@router.post("/order/{order_id}/update-status")
async def update_order_status(order_id: str, status: str, message: str = ""):
    """
    Update order status (for demo simulation)
    
    Valid statuses: confirmed, processing, shipped, out_for_delivery, delivered
    """
    valid_statuses = ["confirmed", "processing", "shipped", "out_for_delivery", "delivered"]
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {valid_statuses}")
    
    success = order_store.update_order_status(order_id, status, message)
    if not success:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    
    return {
        "success": True,
        "message": f"Order {order_id} status updated to {status}",
        "order": order_store.get_order(order_id)
    }

@router.get("/orders")
async def list_all_orders():
    """
    List all demo orders (for debugging)
    """
    return {
        "success": True,
        "count": len(order_store.get_all_orders()),
        "orders": order_store.get_all_orders()
    }
