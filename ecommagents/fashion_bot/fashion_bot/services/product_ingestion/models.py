"""
Models for Product Ingestion Service.

Contains data classes and Pydantic models for:
- Shopify configuration
- Vector data representation
- Ingestion results
- API request/response models
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime
from pydantic import BaseModel
import hashlib
import json

# Re-exported from utils.shared_utils so existing
# `from ...product_ingestion.models import iso_to_epoch` imports keep working
# while the canonical definition lives in the shared util layer (AGENTS.md:
# "Shared Utilities Over Duplication").
from fashion_bot.utils.shared_utils import iso_to_epoch


# Max number of collection names injected into the embedding text. The full
# collections list — including the sale/EOSS collection, which Shopify orders
# last — is kept in the search document's `collections` field for filtering,
# but the vector text only needs a bounded sample. Capping it keeps a product
# that sits in dozens of sale/marketing collections from diluting its semantic
# embedding (and keeps the embedding byte-stable vs. the previous 30-collection
# fetch, so widening the fetch does not re-embed the catalog).
_SEARCHABLE_TEXT_MAX_COLLECTIONS = 30


@dataclass
class ShopifyConfig:
    """Shopify API configuration."""
    shop_domain: str  # e.g., "store.myshopify.com"
    access_token: str
    api_version: str = "2024-04"


@dataclass
class VectorData:
    """Data structure for a single vector to be upserted to Upstash."""
    id: str  # Composite key: {client_id}_{product_id}
    searchable_text: str  # Rich text for embedding
    metadata: Dict[str, Any]


@dataclass
class FailedProduct:
    """Represents a product that failed to ingest."""
    product_id: str
    error: str


@dataclass
class IngestionResult:
    """Result of a product ingestion operation."""
    success_count: int
    failed_count: int
    source_used: str
    duration: float
    timestamp: datetime
    failed_details: List[FailedProduct] = field(default_factory=list)
    
    @property
    def success(self) -> bool:
        return self.failed_count == 0


@dataclass
class DeltaSyncResult:
    """Result of a delta sync operation."""
    products_added: int = 0
    products_updated: int = 0
    products_deleted: int = 0
    products_unchanged: int = 0
    failed_count: int = 0
    source_used: str = ""
    duration: float = 0.0
    timestamp: datetime = field(default_factory=datetime.utcnow)
    failed_details: List[FailedProduct] = field(default_factory=list)
    
    @property
    def success(self) -> bool:
        return self.failed_count == 0
    
    @property
    def total_processed(self) -> int:
        return self.products_added + self.products_updated + self.products_unchanged


class DeltaSyncResponse(BaseModel):
    """Response model for delta sync endpoint."""
    success: bool
    client_id: str
    source_used: str
    products_added: int
    products_updated: int
    products_deleted: int
    products_unchanged: int
    failed_count: int
    duration_seconds: float
    timestamp: str
    failed_products: List[Dict[str, str]] = []


# Pydantic models for API endpoints

class ProductIngestionRequest(BaseModel):
    """Request model for product ingestion endpoint."""
    client_id: str
    source: str = "auto"  # "shopify" | "json" | "auto"
    force_refresh: bool = False  # Clear existing vectors before ingestion


class ProductIngestionResponse(BaseModel):
    """Response model for product ingestion endpoint."""
    success: bool
    client_id: str
    source_used: str
    products_ingested: int
    products_failed: int
    duration_seconds: float
    timestamp: str
    failed_products: List[Dict[str, str]] = []


class IngestionStatusResponse(BaseModel):
    """Response model for ingestion status endpoint."""
    client_id: str
    last_ingestion: Optional[str] = None
    product_count: int = 0
    source_used: Optional[str] = None
    index_health: str = "unknown"


# ==================== SEARCH MODELS ====================

class ProductSearchRequest(BaseModel):
    """Request model for product search endpoint."""
    client_id: str
    query: str
    top_k: int = 10  # Number of results to return
    min_price: Optional[float] = None  # Filter: minimum price
    max_price: Optional[float] = None  # Filter: maximum price
    product_type: Optional[str] = None  # Filter: product type
    in_stock_only: bool = False  # Filter: only in-stock products


class ProductSearchResult(BaseModel):
    """A single search result."""
    product_id: str
    title: str
    product_type: str
    vendor: str
    price_min: float
    price_max: float
    compare_at_price_min: Optional[float] = None
    compare_at_price_max: Optional[float] = None
    image_url: str
    all_images: List[str] = []
    product_url: str
    in_stock: bool
    total_inventory: int = 0
    colors: List[str] = []
    sizes: List[str] = []
    tags: List[str] = []
    description: Optional[str] = None
    # Fabric/care fields from metafields
    fabric: Optional[str] = None
    care_instructions: Optional[str] = None
    fit_type: Optional[str] = None
    size_chart: Optional[Dict[str, Any]] = None
    # SEO fields
    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    # Variants data
    variants: List[Dict[str, Any]] = []
    score: float  # Similarity score from vector search


class ProductSearchResponse(BaseModel):
    """Response model for product search endpoint."""
    success: bool
    client_id: str
    query: str
    total_results: int
    results: List[ProductSearchResult] = []
    search_time_ms: float
    error: Optional[str] = None


@dataclass
class NormalizedProduct:
    """Normalized product data from any source."""
    id: str
    title: str
    handle: str
    product_type: str
    vendor: str
    tags: List[str]
    colors: List[str]
    sizes: List[str]
    price_min: float
    price_max: float
    image_url: str
    product_url: str
    in_stock: bool
    description: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # New fields for comprehensive product data
    compare_at_price_min: Optional[float] = None  # Original price (before discount)
    compare_at_price_max: Optional[float] = None
    discount_pct: float = 0.0  # Discount percentage: (compare_at - price) / compare_at * 100
    total_inventory: int = 0
    status: str = "active"  # active, draft, archived
    published_at: Optional[str] = None  # None → unpublished (not on any sales channel / storefront)
    # Metafields - critical for product details
    fabric: Optional[str] = None  # Material/fabric composition
    care_instructions: Optional[str] = None
    fit_type: Optional[str] = None  # Regular, Slim, Relaxed, etc.
    size_chart: Optional[Dict[str, Any]] = None  # Size measurements
    # Additional images
    all_images: List[str] = field(default_factory=list)
    # SEO
    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    # Variant details for availability
    variants: List[Dict[str, Any]] = field(default_factory=list)
    # LLM-extracted structured attributes for accurate search filtering
    base_product_name: Optional[str] = None  # Core product name without model/color/size
    product_line: Optional[str] = None  # Specific model or sub-category for exact-match filtering
    product_line_normalized: Optional[str] = None  # Lowercased for exact-match filtering
    extracted_color: Optional[str] = None  # Color extracted by LLM from title
    material: Optional[str] = None  # Material type (e.g., "leather", "silicone")
    # Recommendation engine fields (extracted by LLM at ingestion)
    occasion: List[str] = field(default_factory=list)
    style: List[str] = field(default_factory=list)
    vibe: List[str] = field(default_factory=list)
    pairing_tags: List[str] = field(default_factory=list)
    category: Optional[str] = None  # Normalized category (e.g., "bottomwear", "topwear")
    subcategory: Optional[str] = None
    segment: Optional[str] = None  # Gender segment (e.g., "women", "men", "unisex")
    color_family: Optional[str] = None  # Normalized color group (e.g., "black", "blue")
    pattern: Optional[str] = None
    # Shopify collections
    collections: List[str] = field(default_factory=list)
    # Brand from metafield; takes priority over vendor in search docs
    brand: Optional[str] = None
    # Generic metafield attributes — every resolved metafield keyed by bare
    # name (e.g. "neckline", "waist_rise", "ingredients", "spf_level").
    # Business-agnostic: works for fashion, cosmetics, electronics, etc.
    metafield_attributes: Dict[str, str] = field(default_factory=dict)
    # Cleaned metafields (key/value pairs for tool output parity with find_product_by_id)
    all_metafields: List[Dict[str, Any]] = field(default_factory=list)
    # Variant-level stock: only sizes/colors that are currently in stock
    available_sizes: List[str] = field(default_factory=list)
    available_colors: List[str] = field(default_factory=list)
    # Share of variants currently in stock (0-100). Lets the search layer rank /
    # filter toward products whose majority of variants/sizes are purchasable.
    variant_availability_pct: float = 0.0
    bestseller: bool = False
    # Engagement analytics, owned SOLELY by the monthly bestseller_refresh cron
    # (Shopify sessions + sales over bestseller_lookback_days). Like `bestseller`
    # these are POST-COMPUTED — the product sync never sets them, so they default
    # to None here and are stripped from to_search_document until the cron writes
    # them (and are carried forward across syncs, see orchestrator delta sync).
    # unique_sessions: distinct storefront sessions for the product in the window.
    # units_sold: net units sold in the window (mirrors the sales number).
    # conversion_rate: units_sold / unique_sessions (higher = better converting).
    unique_sessions: Optional[int] = None
    units_sold: Optional[int] = None
    conversion_rate: Optional[float] = None
    # Image OCR results applied by the orchestrator before to_search_document.
    # Empty when the per-client `image_ocr_enabled` flag is false.
    image_ocr_texts: Dict[str, str] = field(default_factory=dict)
    image_ocr_summary: str = ""
    image_ocr_hash: str = ""

    def to_search_document(self, client_id: str) -> Dict[str, Any]:
        """Convert to Upstash Search document with content (searchable) and metadata (display).

        Universal product fields are top-level keys.  All Shopify metafields
        (category + product, regardless of business type) are stored in the
        ``metafield_attributes`` dict so nothing is lost.
        """
        segment = self.segment or ""
        if not segment:
            tg = (
                self.metafield_attributes.get("target_gender", "")
                or self.metafield_attributes.get("gender", "")
            )
            if tg:
                segment = tg.lower()

        # Hard override: if the product has an explicit gender metafield
        # ("Women" / "Men") and the LLM assigned "unisex", the metafield wins.
        # "unisex" is only appropriate when no gender signal is present at all.
        _explicit_gender = (
            self.metafield_attributes.get("target_gender", "")
            or self.metafield_attributes.get("gender", "")
        ).strip().lower()
        if _explicit_gender in ("women", "men") and segment == "unisex":
            segment = _explicit_gender
        
        # Cap per-value length in metafield_attributes to stay within
        # Upstash Search's 4096-char content limit.
        _MAX_ATTR_LEN = 200
        capped_attrs = {
            k: (v[:_MAX_ATTR_LEN] if isinstance(v, str) and len(v) > _MAX_ATTR_LEN else v)
            for k, v in self.metafield_attributes.items()
        }

        content = {
            "title": self.title,
            "handle": self.handle,
            "description": (self.description or "")[:500],
            "category": self.category or self.product_type or "",
            "subcategory": self.subcategory or "",
            "brand": self.brand or self.vendor,
            "tags": self.tags,
            "collections": self.collections,
            "colors": self.colors,
            "color_family": self.color_family or "",
            "sizes": self.sizes,
            "material": self.material or self.fabric or "",
            "fit": self.fit_type or "",
            "style": self.style,
            "occasion": self.occasion,
            "vibe": self.vibe,
            "pattern": self.pattern or "",
            "segment": segment,
            "pairing_tags": self.pairing_tags,
            "price_min": self.price_min,
            "price_max": self.price_max,
            "compare_at_price_min": self.compare_at_price_min,
            "discount_pct": self.discount_pct,
            "in_stock": self.in_stock,
            "total_inventory": self.total_inventory,
            "available_sizes": self.available_sizes,
            "available_colors": self.available_colors,
            "variant_availability_pct": self.variant_availability_pct,
            "base_product_name": self.base_product_name or "",
            "product_line": self.product_line or "",
            "product_line_normalized": self.product_line_normalized or "",
            "metafield_attributes": capped_attrs,
            "bestseller": self.bestseller,
            # Engagement analytics (cron-owned). None values are stripped below,
            # so the product sync never writes/overwrites them with defaults.
            "unique_sessions": self.unique_sessions,
            "units_sold": self.units_sold,
            "conversion_rate": self.conversion_rate,
            "created_at": self.created_at,
            # Numeric epoch mirror of created_at — Upstash range filters (>=)
            # work on numbers, not ISO strings (see iso_to_epoch). Used by the
            # new-arrivals recency filter. None values are stripped below.
            "created_at_ts": iso_to_epoch(self.created_at),
        }
        # Remove None values to keep document clean
        content = {k: v for k, v in content.items() if v is not None}

        # ---- 4092-byte budget gate for OCR image_text ----
        # Upstash Search caps content at 4096 chars. Compute remaining budget
        # AFTER all other content fields are populated, then truncate the
        # already-summarised image_text to fit. The summary itself is produced
        # earlier by the OCR pipeline; the gate here is the last line of defence.
        HARD_LIMIT = 4092
        JSON_OVERHEAD = 20    # "image_text":""
        SAFETY = 30
        if self.image_ocr_summary:
            current_size = len(json.dumps(content, ensure_ascii=False, default=str))
            budget = HARD_LIMIT - current_size - JSON_OVERHEAD - SAFETY
            if budget >= 120:
                summary = self.image_ocr_summary
                if len(summary) > budget:
                    truncated = summary[:budget]
                    # Truncate cleanly on the last "; " separator if present
                    cut = truncated.rsplit("; ", 1)[0]
                    summary = cut if cut else truncated
                content["image_text"] = summary

        metadata = {
            "product_id": self.id,
            "client_id": client_id,
            "handle": self.handle,
            "image_url": self.image_url,
            "all_images": self.all_images[:10],
            "product_url": self.product_url,
            "variants": self.variants[:20],
            "care_instructions": self.care_instructions,
            "size_chart": self.size_chart,
            "all_metafields": self.all_metafields[:30],
            "seo_title": self.seo_title,
            "seo_description": self.seo_description,
            "content_hash": self.compute_content_hash(),
            "content_hash_no_inventory": self.compute_content_hash_excluding_inventory(),
            "semantic_content_hash": self.compute_semantic_content_hash(),
            "image_texts": self.image_ocr_texts or {},
            "image_ocr_hash": self.image_ocr_hash or "",
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

        return {
            "id": f"{client_id}_{self.id}",
            "content": content,
            "metadata": metadata,
        }

    def to_vector_data(self, client_id: str) -> VectorData:
        """Convert to VectorData for Upstash ingestion."""
        return VectorData(
            id=f"{client_id}_{self.id}",
            searchable_text=self._build_searchable_text(),
            metadata={
                "client_id": client_id,
                "product_id": self.id,
                "title": self.title,
                "handle": self.handle,
                "product_type": self.product_type,
                "vendor": self.vendor,
                "tags": self.tags,
                "colors": self.colors,
                "sizes": self.sizes,
                "price_min": self.price_min,
                "price_max": self.price_max,
                "compare_at_price_min": self.compare_at_price_min,
                "compare_at_price_max": self.compare_at_price_max,
                "discount_pct": self.discount_pct,
                "image_url": self.image_url,
                "all_images": self.all_images[:10],  # Limit to first 10 images
                "product_url": self.product_url,
                "in_stock": self.in_stock,
                "total_inventory": self.total_inventory,
                "available_sizes": self.available_sizes,
                "available_colors": self.available_colors,
                "variant_availability_pct": self.variant_availability_pct,
                "status": self.status,
                "fabric": self.fabric,
                "care_instructions": self.care_instructions,
                "fit_type": self.fit_type,
                "size_chart": self.size_chart,
                "all_metafields": self.all_metafields[:30],
                "seo_title": self.seo_title,
                "seo_description": self.seo_description,
                "variants": self.variants[:20],  # Limit variants stored
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "content_hash": self.compute_content_hash(),  # For delta sync
                # LLM-extracted structured attributes for search filtering
                "base_product_name": self.base_product_name or "",
                "product_line": self.product_line or "",
                "product_line_normalized": self.product_line_normalized or "",
                "extracted_color": self.extracted_color or "",
                "material": self.material or "",
                "bestseller": self.bestseller,
            }
        )
    
    def _build_searchable_text(self) -> str:
        """
        Constructs rich text for semantic embedding.
        Uses LLM-extracted structured fields when available for better search relevance.
        """
        parts = []
        
        if self.base_product_name:
            parts.append(f"Product: {self.base_product_name}")
        else:
            parts.append(self.title)
        
        if self.product_line:
            parts.append(f"Model: {self.product_line}")
        
        if self.extracted_color:
            parts.append(f"Color: {self.extracted_color}")
        elif self.colors:
            parts.append(f"Colors: {' '.join(self.colors)}")
        
        if self.material:
            parts.append(f"Material: {self.material}")
        
        if self.product_type:
            parts.append(f"Category: {self.product_type}")
        if self.vendor:
            parts.append(f"Brand: {self.vendor}")
        if self.collections:
            parts.append(f"Collections: {', '.join(self.collections[:_SEARCHABLE_TEXT_MAX_COLLECTIONS])}")
        if self.tags:
            parts.append(f"Tags: {' '.join(self.tags)}")
        if self.sizes:
            parts.append(f"Sizes: {' '.join(self.sizes)}")
        if self.description:
            parts.append(f"Description: {self.description[:300]}")
        if self.fabric:
            parts.append(f"Fabric: {self.fabric}")
        if self.fit_type:
            parts.append(f"Fit: {self.fit_type}")
        for attr_key, attr_val in self.metafield_attributes.items():
            if attr_val and attr_key not in ("fabric", "fit", "brand"):
                parts.append(f"{attr_key}: {attr_val}")
        
        return " | ".join(filter(None, parts))

    def compute_content_hash(self) -> str:
        """
        Compute a hash of the product content for delta comparison.
        
        Only includes SOURCE fields (from Shopify). Excludes LLM-extracted
        attributes (base_product_name, extracted_color, material, etc.),
        LLM-overwritable fields (fit_type — overwritten by _apply_extracted_attributes),
        and post-computed fields (bestseller) because those are derived during
        ingestion — including them would cause every sync to re-ingest all
        products since the pre-extraction hash never matches the stored
        post-extraction hash.
        
        Returns:
            MD5 hash string of the product content
        """
        hash_content = {
            "title": self.title,
            "product_type": self.product_type,
            "vendor": self.vendor,
            "tags": sorted(self.tags) if self.tags else [],
            "colors": sorted(self.colors) if self.colors else [],
            "sizes": sorted(self.sizes) if self.sizes else [],
            "collections": sorted(self.collections) if self.collections else [],
            "price_min": self.price_min,
            "price_max": self.price_max,
            "compare_at_price_min": self.compare_at_price_min,
            "compare_at_price_max": self.compare_at_price_max,
            "discount_pct": self.discount_pct,
            "in_stock": self.in_stock,
            "total_inventory": self.total_inventory,
            "available_sizes": sorted(self.available_sizes) if self.available_sizes else [],
            "available_colors": sorted(self.available_colors) if self.available_colors else [],
            "status": self.status,
            "description": self.description[:500] if self.description else "",
            "fabric": self.fabric,
            "care_instructions": self.care_instructions,
            "image_url": self.image_url,
        }
        
        # Create stable JSON representation
        content_str = json.dumps(hash_content, sort_keys=True, default=str)
        
        # Return MD5 hash
        return hashlib.md5(content_str.encode()).hexdigest()

    def compute_content_hash_excluding_inventory(self) -> str:
        """Hash of product content WITHOUT inventory-volatile fields.

        Used by the ``products/update`` webhook to detect whether the
        actual product content (title, description, tags, images …)
        changed, or only inventory quantities shifted.  When only
        inventory changed the LLM extraction can be skipped.

        Includes the raw ``rating``/``rating_count`` metafield strings (not
        parsed -- any change in the raw value is enough to move the hash) so
        a Judge.me (or any reviews app) metafield write is picked up as a
        real content change, not silently ignored. This is the whole
        mechanism, not a backstop: there's no dedicated Judge.me webhook in
        this codebase -- a rating metafield write already fires Shopify's
        products/update webhook on its own, and without this hash entry that
        webhook's own hash comparison would see "nothing changed" and skip
        re-indexing the product entirely.

        The two keys are only added when a rating metafield is actually
        present. Adding them unconditionally (even as ``None``) would move
        the hash for every product on the very first sync after this code
        shipped -- reviewed and unreviewed alike -- since ``None`` still
        differs from a hash computed before these keys existed. That would
        force a full LLM+OCR re-extraction storm across the entire catalog,
        the exact cost this hash exists to avoid, to fix staleness on the
        (usually small) minority of products that have reviews at all.
        """
        hash_content = {
            "title": self.title,
            "product_type": self.product_type,
            "vendor": self.vendor,
            "tags": sorted(self.tags) if self.tags else [],
            "colors": sorted(self.colors) if self.colors else [],
            "sizes": sorted(self.sizes) if self.sizes else [],
            "price_min": self.price_min,
            "price_max": self.price_max,
            "compare_at_price_min": self.compare_at_price_min,
            "compare_at_price_max": self.compare_at_price_max,
            "discount_pct": self.discount_pct,
            "status": self.status,
            "description": self.description[:500] if self.description else "",
            "fabric": self.fabric,
            "care_instructions": self.care_instructions,
            "image_url": self.image_url,
        }
        rating = self.metafield_attributes.get("rating")
        rating_count = self.metafield_attributes.get("rating_count")
        if rating is not None:
            hash_content["rating"] = rating
        if rating_count is not None:
            hash_content["rating_count"] = rating_count
        content_str = json.dumps(hash_content, sort_keys=True, default=str)
        return hashlib.md5(content_str.encode()).hexdigest()

    def compute_semantic_content_hash(self) -> str:
        """Hash of the fields that define what the product *is*.

        ``compute_content_hash_excluding_inventory`` answers "does the *search
        document* need rewriting?" and so covers price, discount, status, tags
        and images. Gating LLM extraction on it re-derives attributes on every
        merchandising edit, which is what drives the "Excessive LLM Calls"
        alert. Two measured causes, neither of them a real product change:

        * **Promotional churn.** Tag sweeps (``BUY2FOR10``, ``DEAL10``,
          ``AW25DIS``, hex colour codes) moved the hash on ~5.7k of 18.4k
          product webhooks in a single day, with title/description untouched.
        * **Enrich instability.** When the Shopify GraphQL enrich is throttled
          the payload loses ``fabric``/``colors``, flipping the hash one way;
          the next healthy webhook flips it back. Each cycle cost two
          extractions and accounted for ~59% of re-extractions on quiet days.

        So the gate is deliberately narrow: **title, product_type and
        description**. These are the fields a product's identity is derived
        from, and the ones whose change can genuinely alter what the model
        concludes. Everything else — tags, vendor, colors, sizes, fabric,
        care_instructions, price, discount, status, image — still drives the
        search-document upsert via the full hash, so the index stays current;
        it just no longer re-runs the LLM.

        Accepted trade-off: an attribute inferred *only* from tags or metafields
        (a ``color_family`` for a product whose colour appears nowhere in the
        title or description, say) can go stale until the identity fields change
        or a re-extract is requested via ``/api/v1/products/re-extract``.

        Unlike the full hash, the description is not truncated here: description
        edits are rare, so there is no storm risk, and truncating would hide a
        genuine rewrite past the cut-off.
        """
        hash_content = {
            "title": self.title,
            "product_type": self.product_type,
            "description": self.description or "",
        }
        content_str = json.dumps(hash_content, sort_keys=True, default=str)
        return hashlib.md5(content_str.encode()).hexdigest()


# ==================== RECOMMENDATION API MODELS ====================

class RecommendationRequest(BaseModel):
    """Request model for /recommend endpoint."""
    session_id: Optional[str] = None
    client_id: str
    user_query: str
    image: Optional[str] = None  # base64 or URL (future)
    user_profile: Optional[Dict[str, Any]] = None
    context: Optional[Dict[str, Any]] = None


class RecommendedProduct(BaseModel):
    product_id: str
    title: str
    price: float
    compare_at_price: Optional[float] = None
    image_url: str = ""
    product_url: str = ""
    in_stock: bool = True
    available_sizes: List[str] = []
    colors: List[str] = []
    reasons: List[str] = []
    score: float = 0.0


class RecommendationResponse(BaseModel):
    session_id: Optional[str] = None
    clarifying_question: Optional[str] = None
    recommendations: List[RecommendedProduct] = []
    filters_applied: Dict[str, Any] = {}
    search_config: Dict[str, Any] = {}

