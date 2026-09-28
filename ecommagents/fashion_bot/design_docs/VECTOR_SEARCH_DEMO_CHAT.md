# Upstash Vector Integration for Demo Chat

## Overview

This document describes the integration of Upstash Vector Service into `demo_chat_router.py` for semantic product search capabilities in the demo chat widget.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Client ID Generation | SHA256 hash of domain (first 16 chars) | Deterministic, no persistence needed, same domain = same client_id |
| State Persistence | In-memory `INGESTED_DOMAINS` dict | Simple, no Redis dependency, acceptable for demo purposes |
| Fallback Strategy | Fall back to Shopify API | Graceful degradation when vector search unavailable or returns no results |
| Search Query | LLM-extracted search terms | LLM extracts core search terms for better vector search |
| Price Extraction | LLM + Regex fallback | LLM extracts prices, regex as reliable fallback |
| Intent Detection | LLM-based (gpt-4o-mini) | Works across all product categories, not limited to hardcoded keywords |
| Segment Detection | LLM from message + context | Detects men/women/kids from product name, URL, or query |
| Recommendation Sorting | Priority scoring (type > color > vector) | Ensures shirts don't show t-shirts, same color boosted |
| In-Stock for Recommendations | Filter out OOS products | Only show buyable products for similar/bought together |
| Product Type Normalization | Strip prefixes (plain, regular fit) | "plain shirt" matches "shirt", "casual shirt" |
| Recommendation Query | Use product context, not user message | Search for "SHIRT shirts green" instead of "similar products" |

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Demo Chat Widget                             │
└─────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      POST /demo/chat                                 │
│  ┌──────────────────────┐                                            │
│  │ LLMMessageAnalyzer   │ → LLM-based intent, entities, segment      │
│  │ (gpt-4o-mini)        │   extraction in a single call              │
│  └──────────────────────┘                                            │
│           │                                                          │
│  ┌────────────────┐                                                  │
│  │ IntentDetector │ → Keyword-based fallback (if LLM unavailable)    │
│  └────────────────┘                                                  │
│           │                                                          │
│           ▼                                                          │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │              VECTOR SEARCH (Primary)                            │ │
│  │  ┌──────────────────────┐  ┌─────────────────────────────────┐ │ │
│  │  │ LLM-extracted query  │  │ vector_search_products()        │ │ │
│  │  │ + price + segment    │→ │ - Check INGESTED_DOMAINS        │ │ │
│  │  └──────────────────────┘  │ - Call Upstash Vector search()  │ │ │
│  │                            │ - Apply price/segment filters   │ │ │
│  │                            └─────────────────────────────────┘ │ │
│  │                                       │                        │ │
│  │                            [Results Found?]                    │ │
│  │                            ├── YES → Use vector results       │ │
│  │                            └── NO  ↓                          │ │
│  └────────────────────────────────────────────────────────────────┘ │
│           │                                                          │
│           ▼                                                          │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │              SHOPIFY API (Fallback)                             │ │
│  │  - fetch_products()                                             │ │
│  │  - fetch_collections()                                          │ │
│  │  - search_products()                                            │ │
│  └────────────────────────────────────────────────────────────────┘ │
│           │                                                          │
│           ▼                                                          │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │              LLM Response Generation                            │ │
│  │  build_prompt_with_data() → Includes vector_search_results      │ │
│  │  or shopify_data (products, collections, etc.)                  │ │
│  └────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

## Components

### 1. LLM-Based Message Analysis

```python
class LLMMessageAnalyzer:
    """
    LLM-based message analysis for intent detection, entity extraction, and semantic understanding.
    Replaces keyword-based detection with a single LLM call for better accuracy across all product categories.
    """
    
    async def analyze_message(message: str, context: PageContext) -> Dict:
        """
        Returns:
        {
            "intent": "product_search|price_check|availability|recommendation|...",
            "entities": {
                "collection": "shirts",
                "product_type": "formal shirt",
                "color": "blue",
                "size": "M",
                "min_price": null,
                "max_price": 2000,
                "brand": null
            },
            "segment": "men|women|kids|unisex|null",
            "is_recommendation_query": true|false,
            "search_query": "blue formal shirts"  # Core terms for vector search
        }
        """
```

**Key Benefits:**
- Works across ALL product categories (fashion, electronics, home goods, etc.)
- No hardcoded keyword lists to maintain
- Understands natural language variations
- Extracts segment from context (product name, URL)
- Falls back to keyword-based `IntentDetector` if LLM unavailable

### 2. Client ID Generation

```python
def get_client_id_for_domain(domain: str) -> str:
    """
    Generate deterministic client_id from domain using SHA256 hash.
    Same domain always produces same client_id (no persistence needed).
    """
    normalized = domain.lower()
    normalized = normalized.replace('https://', '').replace('http://', '')
    normalized = normalized.replace('www.', '').rstrip('/')
    
    hash_object = hashlib.sha256(normalized.encode('utf-8'))
    return hash_object.hexdigest()[:16]
```

### 3. Product Transformation

```python
def product_to_vector_data(product: Dict, client_id: str, store_url: str) -> VectorData:
    """
    Convert Shopify product to VectorData for upserting.
    
    Metadata includes:
    - client_id, product_id, title, handle
    - product_type, vendor
    - price_min, price_max, compare_at_price_min/max
    - image_url, all_images
    - product_url
    - in_stock, total_inventory
    - colors, sizes, tags
    - variants (first 10)
    - segment (men, women, kids - extracted from title/tags)
    """
```

### 4. Segment Extraction

Products are automatically categorized by segment (men/women/kids) during ingestion:

```python
def extract_segment_from_product(product: Dict) -> Optional[str]:
    """
    Extract segment from product data (title, product_type, tags).
    
    Patterns detected:
    - men: "men's", "male", "gents", "boy's"
    - women: "women's", "woman's", "female", "ladies", "girl's"
    - kids: "kids", "children's", "junior", "infant", "toddler"
    
    Returns: 'men', 'women', 'kids', or None
    """
```

For "similar products" queries, segment is extracted from page context:

```python
def extract_segment_from_context(context: PageContext) -> Optional[str]:
    """
    Extract segment from current page context.
    
    Checks (in order):
    1. Product name (e.g., "Men's Shirt" → "men")
    2. URL path (e.g., "/products/mens-jacket" → "men")
    3. Structured data (category, productType fields)
    4. Page title
    
    Returns: 'men', 'women', 'kids', or None
    """
```

### 4. Searchable Text Format

```python
def build_searchable_text(product: Dict) -> str:
    """
    Build rich text for embedding. Format:
    
    "Product: {title} | Category: {product_type} | Brand: {vendor} | 
     Tags: {tags} | Colors: {colors} | Sizes: {sizes} | 
     Price: Rs {min} to Rs {max} | Description: {description[:500]}"
    """
```

### 5. Price Extraction

```python
def extract_price_filters(message: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Extract min/max price from natural language.
    
    Patterns:
    - "under 2000", "below 2000", "less than 2000" → max_price=2000
    - "above 1000", "over 1000", "more than 1000" → min_price=1000
    - "between 1000 and 2000", "1000 to 2000" → (1000, 2000)
    - "around 1500", "about 1500" → (1050, 1950) [±30%]
    """
```

## API Endpoints

### Existing Endpoints (Modified)

#### `GET /demo/fetch-products`

**New Parameter:** `ingest: bool = False`

```
GET /demo/fetch-products?store_url=example.com&limit=100&ingest=true

Response:
{
  "success": true,
  "store": "https://example.com",
  "count": 100,
  "products": [...],
  "ingestion": {
    "success": true,
    "client_id": "a1b2c3d4e5f67890",
    "success_count": 100,
    "failed_count": 0
  }
}
```

#### `GET /demo/health`

**New Fields:**
- `vector_service_available`: Whether Upstash Vector is configured
- `ingested_domains_count`: Number of domains currently ingested

```json
{
  "status": "healthy",
  "service": "demo-chat-enhanced",
  "features": ["shopify_runtime", "intent_detection", "product_cards", "vector_search"],
  "llm_available": true,
  "vector_service_available": true,
  "ingested_domains_count": 3,
  "timestamp": "2026-01-31T10:00:00"
}
```

### New Endpoints

#### `GET /demo/vector-search`

Direct semantic search endpoint.

```
GET /demo/vector-search?store_url=example.com&query=red shirts under 2000&top_k=10&segment=men

Parameters:
  - store_url: str (required) - Shopify store URL or domain
  - query: str (required) - Natural language search query
  - top_k: int (default: 10) - Number of results
  - segment: str (optional) - Filter by segment: "men", "women", "kids"
  - min_price: float (optional) - Minimum price filter
  - max_price: float (optional) - Maximum price filter
  - in_stock_only: bool (default: false) - Only in-stock products

Response:
{
  "success": true,
  "domain": "example.com",
  "client_id": "a1b2c3d4e5f67890",
  "query": "red shirts under 2000",
  "count": 8,
  "results": [
    {
      "id": "a1b2c3d4e5f67890_12345",
      "score": 0.92,
      "product_id": "12345",
      "title": "Classic Red Cotton Shirt",
      "price_min": 1499,
      "price_max": 1499,
      "in_stock": true,
      "colors": ["Red"],
      "sizes": ["S", "M", "L", "XL"],
      ...
    }
  ]
}
```

#### `GET /demo/vector-status`

Check vector DB status for a domain or all domains.

```
GET /demo/vector-status?store_url=example.com

Response (specific domain):
{
  "success": true,
  "vector_service_available": true,
  "ingested_domains_count": 3,
  "domain": "example.com",
  "client_id": "a1b2c3d4e5f67890",
  "is_ingested": true,
  "ingestion_info": {
    "domain": "example.com",
    "ingested_at": "2026-01-31T10:00:00",
    "product_count": 150
  },
  "vector_count": 150,
  "index_info": {
    "dimension": 1024,
    "similarity_function": "COSINE",
    "vector_count": 450,
    "pending_vector_count": 0
  }
}

Response (all domains):
{
  "success": true,
  "vector_service_available": true,
  "ingested_domains_count": 3,
  "ingested_domains": {
    "a1b2c3d4e5f67890": {"domain": "store1.com", "ingested_at": "...", "product_count": 150},
    "b2c3d4e5f67890a1": {"domain": "store2.com", "ingested_at": "...", "product_count": 80},
    ...
  }
}
```

#### `DELETE /demo/vector-clear`

Clear vector DB entries for a specific domain.

```
DELETE /demo/vector-clear?store_url=example.com

Response:
{
  "success": true,
  "domain": "example.com",
  "client_id": "a1b2c3d4e5f67890",
  "vectors_deleted": 150
}
```

## Chat Flow Integration

### Process Flow

```python
async def process_chat_with_shopify(request: ChatRequest):
    # 1. Detect intent and entities
    intent, entities = IntentDetector.detect(request.message)
    
    # 2. For product intents, try vector search FIRST
    product_intents = ["product_search", "price_check", "availability", 
                       "product_details", "collection", "recommendation", 
                       "deals", "new_arrivals"]
    
    if intent in product_intents or entities.get("collection"):
        # Extract price filters from natural language
        min_price, max_price = extract_price_filters(request.message)
        
        # Try vector search
        vector_results = await vector_search_products(
            domain=domain,
            query=request.message,  # Raw user message
            top_k=15,
            min_price=min_price,
            max_price=max_price
        )
        
        if vector_results:
            # Convert to Shopify-compatible format
            shopify_data["vector_search_results"] = vector_results
            shopify_data["products"] = convert_to_shopify_format(vector_results)
            metadata["vector_search_used"] = True
    
    # 3. FALLBACK to Shopify API if vector search didn't return results
    if not metadata.get("vector_search_used"):
        # ... existing Shopify API logic ...
    
    # 4. Build LLM prompt with results
    prompt = build_prompt_with_data(context, shopify_data)
```

### LLM Prompt Integration

When vector search results are available, they're formatted in the prompt:

```
🎯 SEMANTIC SEARCH RESULTS (8 products found):
These products are semantically matched to the user's query.

1. **Classic Red Cotton Shirt**
   💰 ₹1499
   📏 Sizes: S, M, L, XL
   🎨 Colors: Red

2. **Premium Red Linen Shirt**
   💰 ₹1899
   📏 Sizes: M, L, XL, XXL
   🎨 Colors: Red, Maroon

...

✅ Present these products to the customer with their prices and available options.
```

## Usage Guide

### Step 1: Ingest Products

Before vector search works, products must be ingested:

```bash
# Ingest products for a store (one-time or periodic)
curl "https://api.example.com/demo/fetch-products?store_url=mystore.myshopify.com&limit=250&ingest=true"
```

### Step 2: Chat Uses Vector Search Automatically

Once ingested, the chat flow automatically uses vector search:

```bash
curl -X POST "https://api.example.com/demo/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Show me red shirts under 2000",
    "context": {
      "domain": "mystore.myshopify.com",
      "url": "https://mystore.myshopify.com/"
    }
  }'
```

### Step 3: Direct Vector Search (Optional)

For debugging or direct access:

```bash
curl "https://api.example.com/demo/vector-search?store_url=mystore.myshopify.com&query=red%20shirts&max_price=2000"
```

### Step 4: Check Status

```bash
curl "https://api.example.com/demo/vector-status?store_url=mystore.myshopify.com"
```

### Step 5: Clear and Re-ingest (if needed)

```bash
# Clear existing vectors
curl -X DELETE "https://api.example.com/demo/vector-clear?store_url=mystore.myshopify.com"

# Re-ingest
curl "https://api.example.com/demo/fetch-products?store_url=mystore.myshopify.com&limit=250&ingest=true"
```

## Implementation Details

### Files Modified

- **`fashion_bot/demo_chat_router.py`** - All changes in single file

### New Imports

```python
import hashlib

# Conditional import for graceful degradation
try:
    from fashion_bot.services.product_ingestion.upstash_vector_service import UpstashVectorService
    from fashion_bot.services.product_ingestion.models import VectorData
    VECTOR_SERVICE_AVAILABLE = True
except ImportError:
    VECTOR_SERVICE_AVAILABLE = False
```

### Global State

```python
# Vector service singleton
_vector_service: Optional[UpstashVectorService] = None

# In-memory tracking of ingested domains
# Format: {"client_id": {"domain": str, "ingested_at": str, "product_count": int}}
INGESTED_DOMAINS: Dict[str, Dict[str, Any]] = {}
```

### New Functions

| Function | Purpose |
|----------|---------|
| `get_vector_service()` | Singleton for vector service initialization |
| `get_client_id_for_domain(domain)` | Generate deterministic client ID |
| `extract_colors_and_sizes(product)` | Extract variant attributes |
| `build_searchable_text(product)` | Create text for embedding |
| `product_to_vector_data(product, client_id, store_url)` | Convert to VectorData |
| `extract_price_filters(message)` | Parse price from natural language |
| `extract_color_from_context(context)` | Extract color from product page context |
| `extract_segment_from_context(context)` | Extract segment (men/women/kids) from context |
| `vector_search_products(domain, query, ...)` | Perform semantic search |
| `ingest_products_to_vector_db(products, domain, store_url)` | Ingest to Upstash |
| `product_type_match_score(result)` | Score product type match for recommendations (0-2) |
| `color_match_score(result)` | Score color match for recommendations (0-2) |

## Environment Variables

The integration uses existing Upstash environment variables:

```bash
UPSTASH_VECTOR_REST_URL=https://xxx.upstash.io
UPSTASH_VECTOR_REST_TOKEN=xxx
```

## Graceful Degradation

The system degrades gracefully in these scenarios:

1. **Upstash not configured** → Falls back to Shopify API
2. **Domain not ingested** → Falls back to Shopify API
3. **Vector search returns no results** → Falls back to Shopify API
4. **Vector search throws error** → Falls back to Shopify API

## Future Improvements

1. **Automatic ingestion on first visit** - Could auto-ingest when a new domain is detected
2. **Periodic re-sync** - Background job to keep vectors in sync
3. **Redis-based ingestion tracking** - For persistence across restarts
4. **Hybrid search** - Combine vector + keyword search
5. **Collection-aware search** - Filter by collection in vector search

## Recommendation System Enhancements

### Priority Scoring for Similar Products

When a user asks for "similar products" or recommendations while viewing a product, the system uses a multi-level priority scoring algorithm to ensure the most relevant products appear first.

#### Scoring Hierarchy

```
┌─────────────────────────────────────────────────────────────────┐
│                    PRIORITY SCORING ORDER                       │
│                                                                 │
│  1. Product Type Match (Primary)   → Shirts before T-shirts    │
│  2. Color Match (Secondary)        → Same color boosted        │
│  3. Vector Score (Tertiary)        → Semantic similarity       │
└─────────────────────────────────────────────────────────────────┘
```

#### Product Type Scoring

```python
def product_type_match_score(result):
    """
    Score for product type matching:
    - 2: Exact product type match (after normalization)
    - 2: Partial match (e.g., "shirt" in "casual shirt")
    - 1: Product type found in title
    - 0: No match (e.g., t-shirt when looking for shirt)
    """
```

**Normalization:** Common prefixes are stripped for better matching:
- "plain shirt" → "shirt"
- "regular fit shirt" → "shirt"
- "slim fit trouser" → "trouser"
- "relaxed fit jeans" → "jeans"

#### Color Scoring

```python
def color_match_score(result):
    """
    Score for color matching:
    - 2: Exact color match in product's colors array
    - 1: Color found in product title
    - 0: No color match
    """
```

#### Combined Sorting

```python
# Sort by: product_type match > color match > vector score
vector_results_sorted = sorted(
    vector_results,
    key=lambda r: (product_type_match_score(r), color_match_score(r), r.get("score", 0)),
    reverse=True
)
```

### Recommendation Query Building

For "similar products" queries, instead of searching for generic terms like "similar products", the system builds a rich query from the current product context:

```python
# Example: User is viewing "REGULAR FIT PLAIN SHIRT" in green
# Instead of searching: "similar products"
# We search: "REGULAR FIT PLAIN SHIRT shirts green"

query_parts = [product_name]  # "REGULAR FIT PLAIN SHIRT"
if product_type:
    query_parts.append(product_type)  # "shirts"
if collection:
    query_parts.append(collection)
if current_color:
    query_parts.append(current_color)  # "green"

effective_search_query = " ".join(query_parts)
```

### In-Stock Filtering for Recommendations

For recommendation queries, only in-stock products are shown:

```python
vector_results = await vector_search_products(
    domain=domain,
    query=effective_search_query,
    top_k=30,  # Fetch more for priority sorting, limit to 5 after
    segment=segment,
    in_stock_only=is_recommendation_query  # True for similar products
)

# Additional safety filter (in case metadata wasn't set correctly)
if is_recommendation_query:
    vector_results = [r for r in vector_results if r.get("in_stock", True)]
```

### Example Flow

```
User: "Show me similar products" (while viewing Men's Green Plain Shirt)

1. Detect: is_recommendation_query = true
2. Extract from context:
   - product_name: "REGULAR FIT PLAIN SHIRT"
   - product_type: "plain shirt" → normalized to "shirt"
   - segment: "men"
   - color: "green"

3. Build query: "REGULAR FIT PLAIN SHIRT shirts green"

4. Vector search with filters:
   - segment: "men"
   - in_stock_only: true
   - top_k: 30

5. Priority scoring:
   - Shirts (score 2) ranked before T-shirts (score 0)
   - Green shirts ranked before other colors
   - Within same scores, higher vector similarity wins

6. Return top 5 results
```

### Product URL Format

Product links in recommendations use clean URL format (no markdown):

```
**Slim Fit Cotton Shirt**
💰 ₹1,299
📏 Sizes: S, M, L, XL
🔗 https://store.com/products/slim-fit-cotton-shirt
```

Not:
```
[View Product](https://store.com/products/slim-fit-cotton-shirt)  ❌
Link: https://store.com/products/slim-fit-cotton-shirt  ❌
```
