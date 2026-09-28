# Product Ingestion to Upstash Vector DB - Design Document

## Overview

This design outlines a comprehensive system to ingest active product data into Upstash VectorDB for semantic search capabilities. The system follows the existing orchestrator/ServiceFactory architecture pattern and includes:

- **Full ingestion**: Initial bulk upload of all products
- **Delta sync**: Efficient incremental updates based on content hash comparison
- **Scheduled sync**: Cron job for automatic synchronization every 4 hours
- **Semantic search**: Vector-based product search with metadata filtering
- **Graph integration**: Seamless integration with the conversation graph for product discovery

## Architecture

### File Structure

```
fashion_bot/
├── fashion_bot/
│   ├── services/
│   │   ├── product_ingestion/
│   │   │   ├── __init__.py
│   │   │   ├── orchestrator.py           # ProductIngestionOrchestrator
│   │   │   ├── product_source_factory.py # Factory for product sources
│   │   │   ├── base_product_service.py   # Abstract base class
│   │   │   ├── shopify_product_service.py # GraphQL-based Shopify fetcher
│   │   │   ├── products_json_service.py  # Fallback for /products.json
│   │   │   ├── upstash_vector_service.py # Vector DB operations
│   │   │   └── models.py                 # Data models and schemas
│   ├── cron_jobs/
│   │   └── product_vector_sync_job.py    # Scheduled delta sync
│   ├── shopify/
│   │   └── tools/
│   │       └── product_adapter.py        # Vector search integration
```

### Vector DB Schema

#### Index Configuration

- **Embedding Model**: `BAAI/bge-m3` (via Upstash's built-in embedding)
- **Dimension**: 1024 (BGE-M3 dimension)
- **Distance Metric**: Cosine similarity
- **Multi-tenancy**: Single index with `client_id` in metadata (Option A)

#### Vector Schema

```python
{
    "id": "client123_product456",  # Composite key: {client_id}_{product_id}
    "data": "searchable text...",   # Rich text for embedding
    "metadata": {
        # Core identification
        "client_id": "client123",
        "product_id": "456",
        "title": "Classic Cotton Hoodie",
        "handle": "classic-cotton-hoodie",
        "product_type": "Hoodie",
        "vendor": "BrandName",
        
        # Attributes for filtering
        "tags": ["summer", "casual", "cotton"],
        "colors": ["black", "navy", "grey"],
        "sizes": ["S", "M", "L", "XL"],
        
        # Pricing
        "price_min": 1499.00,
        "price_max": 1899.00,
        "compare_at_price_min": 1999.00,  # Original price (for discounts)
        "compare_at_price_max": 2499.00,
        
        # Media
        "image_url": "https://cdn.shopify.com/...",
        "all_images": ["url1", "url2", "url3"],  # Up to 5 images
        "product_url": "https://store.com/products/classic-cotton-hoodie",
        
        # Inventory
        "in_stock": true,
        "total_inventory": 150,
        "status": "active",  # active, draft, archived
        
        # Metafields (from Shopify)
        "fabric": "100% Cotton",
        "care_instructions": "Machine wash cold",
        "fit_type": "Regular",
        "size_chart": {"S": {"chest": 38}, "M": {"chest": 40}},
        
        # SEO
        "seo_title": "Classic Cotton Hoodie | BrandName",
        "seo_description": "Comfortable everyday hoodie...",
        
        # Variants (first 20)
        "variants": [
            {"id": "v1", "title": "Black / S", "price": "1499", "inventory_quantity": 10},
            {"id": "v2", "title": "Black / M", "price": "1499", "inventory_quantity": 15}
        ],
        
        # Timestamps
        "created_at": "2024-01-15T10:30:00Z",
        "updated_at": "2024-01-20T14:22:00Z",
        
        # Delta sync
        "content_hash": "a1b2c3d4e5f6..."  # MD5 hash for change detection
    }
}
```

### Searchable Text Construction

The `data` field (used for embedding) will be a rich text combining multiple product attributes:

```python
def _build_searchable_text(self) -> str:
    """
    Constructs rich text for semantic embedding.
    Combines multiple product attributes for better search relevance.
    """
    parts = [
        self.title,
        self.product_type,
        self.vendor,
        " ".join(self.tags),
        " ".join(self.colors),
        " ".join(self.sizes),
        self.description,
        self.fabric or "",
        self.fit_type or "",
    ]
    return " | ".join(filter(None, parts))

# Example output:
# "Classic Cotton Hoodie | Hoodie | BrandName | summer casual cotton | black navy grey | S M L XL | Comfortable everyday hoodie... | 100% Cotton | Regular"
```

### Content Hash for Delta Sync

Products include a content hash for efficient change detection:

```python
def compute_content_hash(self) -> str:
    """
    Compute a hash of the product content for delta comparison.
    Changes to these fields indicate the vector needs to be updated.
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
        "in_stock": self.in_stock,
        "total_inventory": self.total_inventory,
        "status": self.status,
        "description": self.description[:500] if self.description else "",
        "fabric": self.fabric,
        "care_instructions": self.care_instructions,
        "fit_type": self.fit_type,
        "image_url": self.image_url,
    }
    
    content_str = json.dumps(hash_content, sort_keys=True, default=str)
    return hashlib.md5(content_str.encode()).hexdigest()
```

---

## API Endpoints

### 1. Ingest Products (Full)

**Endpoint**: `POST /api/v1/products/ingest`

Performs a full ingestion of all products to the vector index.

**Request Body**:

```json
{
    "client_id": "client123",
    "source": "shopify",  // Optional: "shopify" | "json" | "auto" (default)
    "force_refresh": false  // Optional: Clear existing vectors before ingestion
}
```

**Response**:

```json
{
    "success": true,
    "client_id": "client123",
    "source_used": "shopify_graphql",
    "products_ingested": 150,
    "products_failed": 2,
    "duration_seconds": 12.5,
    "timestamp": "2024-01-22T10:30:00Z",
    "failed_products": [
        {"product_id": "789", "error": "Missing required field: title"}
    ]
}
```

### 2. Delta Sync Products (Single Client)

**Endpoint**: `POST /api/v1/products/delta-sync`

Efficiently syncs only changed products based on content hash comparison.

**Request Body**:

```json
{
    "client_id": "client123",
    "source": "auto"  // Optional: "shopify" | "json" | "auto" (default)
}
```

**Response**:

```json
{
    "success": true,
    "client_id": "client123",
    "source_used": "shopify_graphql",
    "products_added": 5,
    "products_updated": 12,
    "products_deleted": 2,
    "products_unchanged": 131,
    "failed_count": 0,
    "duration_seconds": 8.3,
    "timestamp": "2024-01-22T14:30:00Z",
    "failed_products": []
}
```

### 3. Delta Sync All Clients

**Endpoint**: `POST /api/v1/products/delta-sync/all`

Triggers delta sync for all clients with Shopify configuration.

**Response**:

```json
{
    "success": true,
    "clients_processed": 5,
    "clients_failed": 0,
    "total_products_added": 15,
    "total_products_updated": 45,
    "total_products_deleted": 8,
    "total_products_unchanged": 520,
    "duration_seconds": 45.2,
    "started_at": "2024-01-22T14:30:00Z",
    "completed_at": "2024-01-22T14:30:45Z",
    "results": [
        {"client_id": "client1", "success": true, "products_added": 3, ...},
        {"client_id": "client2", "success": true, "products_added": 5, ...}
    ]
}
```

### 4. Trigger Cron Job (Manual)

**Endpoint**: `POST /cron/trigger-product-vector-sync`

Manually triggers the scheduled delta sync cron job without waiting for the 4-hour interval.

**Response**:

```json
{
    "success": true,
    "clients_processed": 5,
    "clients_failed": 0,
    "total_products_added": 10,
    "total_products_updated": 25,
    "total_products_deleted": 3,
    "total_products_unchanged": 450,
    "duration_seconds": 38.5,
    "started_at": "2024-01-22T14:30:00Z",
    "completed_at": "2024-01-22T14:30:38Z"
}
```

### 5. Get Ingestion Status

**Endpoint**: `GET /api/v1/products/ingest/status/{client_id}`

Returns last ingestion timestamp, product count, and source used.

**Response**:

```json
{
    "client_id": "client123",
    "last_ingestion": "2024-01-22T10:30:00Z",
    "product_count": 150,
    "source_used": "shopify_graphql",
    "index_health": "healthy"
}
```

### 6. Search Products

**Endpoint**: `POST /api/v1/products/search`

Performs semantic search on ingested products with optional metadata filters.

**Request Body**:

```json
{
    "client_id": "client123",
    "query": "summer hoodies",
    "top_k": 10,
    "min_price": 500,
    "max_price": 2000,
    "product_type": "Hoodie",
    "in_stock_only": true
}
```

**Response**:

```json
{
    "success": true,
    "client_id": "client123",
    "query": "summer hoodies",
    "total_results": 5,
    "search_time_ms": 45.2,
    "results": [
        {
            "product_id": "456",
            "title": "Classic Cotton Hoodie",
            "product_type": "Hoodie",
            "vendor": "BrandName",
            "price_min": 1499.00,
            "price_max": 1899.00,
            "compare_at_price_min": 1999.00,
            "compare_at_price_max": 2499.00,
            "image_url": "https://cdn.shopify.com/...",
            "all_images": ["url1", "url2"],
            "product_url": "https://store.com/products/...",
            "in_stock": true,
            "total_inventory": 150,
            "colors": ["black", "navy"],
            "sizes": ["S", "M", "L", "XL"],
            "tags": ["summer", "casual"],
            "description": "Comfortable everyday hoodie...",
            "fabric": "100% Cotton",
            "care_instructions": "Machine wash cold",
            "fit_type": "Regular",
            "size_chart": {"S": {"chest": 38}},
            "seo_title": "Classic Cotton Hoodie",
            "seo_description": "...",
            "variants": [...],
            "score": 0.92
        }
    ]
}
```

---

## Service Classes

### 1. ProductIngestionOrchestrator

```python
class ProductIngestionOrchestrator:
    """
    Orchestrates the product ingestion pipeline.
    Main entry point for ingesting products to Upstash VectorDB.
    """
    
    def __init__(self):
        self.source_factory = ProductSourceFactory()
        self.vector_service = UpstashVectorService()
        self._ingestion_status: Dict[str, Dict[str, Any]] = {}
    
    async def ingest_products(
        self,
        client_id: str,
        source: str = "auto",
        force_refresh: bool = False
    ) -> IngestionResult:
        """
        Full ingestion pipeline:
        1. Determine product source (Shopify or JSON fallback)
        2. Fetch all active products
        3. Transform to vector format
        4. Upsert to Upstash VectorDB
        """
        product_service = self.source_factory.get_service(client_id, source)
        
        if force_refresh:
            self.vector_service.delete_by_client(client_id)
        
        products = await product_service.fetch_active_products()
        vectors = [p.to_vector_data(client_id) for p in products]
        result = self.vector_service.upsert_batch(vectors)
        
        return result
    
    async def delta_sync_products(
        self,
        client_id: str,
        source: str = "auto"
    ) -> DeltaSyncResult:
        """
        Delta sync: Only update changed products in the vector index.
        
        Compares products from source with existing vectors:
        - ADD: New products not in index
        - UPDATE: Products with changed content (based on content_hash)
        - DELETE: Products in index but removed from source
        - UNCHANGED: Products with same content_hash (skip)
        """
        product_service = self.source_factory.get_service(client_id, source)
        
        # Fetch current products from source
        source_products = await product_service.fetch_active_products()
        
        # Compute content hashes
        source_hashes = {p.id: p.compute_content_hash() for p in source_products}
        
        # Fetch existing hashes from vector index
        existing_hashes = self.vector_service.get_existing_product_hashes(client_id)
        
        # Determine which products need action
        products_to_add = source_ids - existing_ids
        products_to_delete = existing_ids - source_ids
        products_to_update = {id for id in (source_ids & existing_ids) 
                             if source_hashes[id] != existing_hashes[id]}
        products_unchanged = (source_ids & existing_ids) - products_to_update
        
        # Process deletions, additions, and updates
        # ... (batch operations)
        
        return DeltaSyncResult(...)
```

### 2. ProductSourceFactory

```python
class ProductSourceFactory:
    """
    Factory to determine the appropriate product source.
    
    Priority:
    1. If source="shopify" and credentials exist -> ShopifyProductService
    2. If source="json" or no Shopify -> ProductsJsonService
    3. If source="auto" -> Try Shopify first, fallback to JSON
    """
    
    def get_service(
        self, 
        client_id: str, 
        source: str = "auto"
    ) -> BaseProductService:
        if source in ("shopify", "auto"):
            shopify_config = self._get_shopify_config(client_id)
            if shopify_config:
                website_url = self._get_website_url(client_id)
                return ShopifyProductService(shopify_config, website_base_url=website_url)
            elif source == "shopify":
                raise ValueError(f"Shopify credentials not found for client {client_id}")
        
        if source in ("json", "auto"):
            website_url = self._get_website_url(client_id)
            if website_url:
                return ProductsJsonService(website_url)
        
        raise ValueError(f"No valid product source found for client {client_id}")
    
    def _get_shopify_config(self, client_id: str) -> Optional[ShopifyConfig]:
        """Reads Shopify credentials from config_manager."""
        from fashion_bot.config_manager import get_shopify_config
        config = get_shopify_config(client_id=client_id)
        
        if config and config.get("access_token") and config.get("shop_url"):
            return ShopifyConfig(
                shop_domain=config["shop_url"],
                access_token=config["access_token"],
                api_version=config.get("api_version", "2024-04")
            )
        return None
```

### 3. ShopifyProductService (GraphQL-based)

```python
# GraphQL query for fetching products with all details including metafields
PRODUCTS_GRAPHQL_QUERY = """
query getProducts($cursor: String) {
    products(first: 50, after: $cursor, query: "status:active") {
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
        self.shop_domain = config.shop_domain
        self.access_token = config.access_token
        self.api_version = config.api_version
        self.website_base_url = website_base_url  # For custom product URLs
        self.graphql_url = f"https://{self.shop_domain}/admin/api/{self.api_version}/graphql.json"
    
    @property
    def source_name(self) -> str:
        return "shopify_graphql"
    
    async def fetch_active_products(self) -> List[NormalizedProduct]:
        """
        Fetches all active products with variants and metafields using GraphQL.
        Uses cursor-based pagination for large catalogs.
        """
        products = []
        cursor = None
        has_next_page = True
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            while has_next_page:
                payload = {"query": PRODUCTS_GRAPHQL_QUERY, "variables": {"cursor": cursor}}
                response = await client.post(self.graphql_url, headers=headers, json=payload)
                
                data = response.json()
                for edge in data["data"]["products"]["edges"]:
                    normalized = self._normalize_product(edge["node"])
                    products.append(normalized)
                
                has_next_page = data["data"]["products"]["pageInfo"]["hasNextPage"]
                cursor = data["data"]["products"]["pageInfo"]["endCursor"]
        
        return products
    
    def _normalize_product(self, raw: dict) -> NormalizedProduct:
        """
        Normalize raw Shopify GraphQL product to NormalizedProduct.
        Extracts: prices, sizes, colors, metafields (fabric, care, fit, size_chart)
        """
        # Extract variants, prices, sizes, colors, metafields...
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
            image_url=image_url,
            all_images=all_images,
            product_url=product_url,
            in_stock=in_stock,
            total_inventory=raw.get("totalInventory", 0),
            fabric=fabric,
            care_instructions=care_instructions,
            fit_type=fit_type,
            size_chart=size_chart,
            seo_title=seo_title,
            seo_description=seo_description,
            variants=variant_list,
            ...
        )
```

### 4. ProductsJsonService (Fallback)

```python
class ProductsJsonService(BaseProductService):
    """
    Fetches products from /products.json endpoint.
    Works with Shopify storefronts without API credentials.
    """
    
    def __init__(self, website_url: str):
        self.base_url = website_url.rstrip("/")
    
    async def fetch_active_products(self) -> List[dict]:
        """
        Fetches products from public /products.json endpoint.
        Limited to 250 products per page.
        """
        products = []
        page = 1
        
        while True:
            url = f"{self.base_url}/products.json?page={page}&limit=250"
            response = await self._make_request(url)
            
            if not response.get("products"):
                break
                
            products.extend(response["products"])
            page += 1
        
        return [self._normalize_product(p) for p in products]
```

### 5. UpstashVectorService

```python
class UpstashVectorService:
    """
    Handles all Upstash VectorDB operations.
    Uses Upstash's built-in embedding (data field) with BAAI/bge-m3 model.
    """
    
    def __init__(self):
        self._index = None
        self._url = os.getenv("UPSTASH_VECTOR_REST_URL")
        self._token = os.getenv("UPSTASH_VECTOR_REST_TOKEN")
    
    @property
    def index(self):
        """Lazy initialization of Upstash Vector Index."""
        if self._index is None:
            from upstash_vector import Index
            self._index = Index(url=self._url, token=self._token)
        return self._index
    
    def upsert_batch(
        self, 
        vectors: List[VectorData],
        batch_size: int = 100
    ) -> Dict[str, Any]:
        """
        Upserts vectors in batches.
        Uses Upstash's built-in embedding (data field).
        """
        success_count = 0
        failed = []
        
        for i in range(0, len(vectors), batch_size):
            batch = vectors[i:i + batch_size]
            try:
                upsert_data = [
                    {
                        "id": v.id,
                        "data": v.searchable_text,  # Upstash embeds this
                        "metadata": v.metadata
                    }
                    for v in batch
                ]
                self.index.upsert(vectors=upsert_data)
                success_count += len(batch)
            except Exception as e:
                for v in batch:
                    failed.append({"product_id": v.metadata.get("product_id"), "error": str(e)})
        
        return {"success_count": success_count, "failed": failed}
    
    def search(
        self,
        query: str,
        client_id: str,
        top_k: int = 10,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        product_type: Optional[str] = None,
        in_stock_only: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Search for products using semantic similarity with metadata filters.
        """
        # Build filter string
        filter_parts = [f"client_id = '{client_id}'"]
        if min_price is not None:
            filter_parts.append(f"price_min >= {min_price}")
        if max_price is not None:
            filter_parts.append(f"price_max <= {max_price}")
        if product_type:
            filter_parts.append(f"product_type = '{product_type}'")
        if in_stock_only:
            filter_parts.append("in_stock = true")
        
        filter_str = " AND ".join(filter_parts)
        
        results = self.index.query(
            data=query,
            top_k=top_k,
            filter=filter_str,
            include_metadata=True,
            include_vectors=False
        )
        
        return [{"id": r.id, "score": r.score, **r.metadata} for r in results]
    
    def get_existing_product_hashes(self, client_id: str) -> Dict[str, str]:
        """
        Fetch all existing product_id -> content_hash mappings for a client.
        Used for delta sync comparison.
        """
        result = self.index.query(
            data="products",
            top_k=1000,
            filter=f"client_id = '{client_id}'",
            include_metadata=True,
            include_vectors=False
        )
        
        return {r.metadata.get("product_id"): r.metadata.get("content_hash", "") 
                for r in result if r.metadata.get("product_id")}
    
    def delete_by_ids(self, ids: List[str]) -> int:
        """Delete vectors by their IDs."""
        if not ids:
            return 0
        self.index.delete(ids=ids)
        return len(ids)
    
    def delete_by_client(self, client_id: str) -> int:
        """Deletes all vectors for a specific client using paginated queries."""
        # Query and delete in batches...
        pass
    
    def health_check(self) -> bool:
        """Check if the Upstash Vector index is healthy."""
        try:
            self.index.info()
            return True
        except:
            return False
```

---

## Cron Job: Scheduled Delta Sync

### Overview

The system includes a scheduled cron job that runs every 4 hours to keep the vector index in sync with Shopify products.

**File**: `fashion_bot/cron_jobs/product_vector_sync_job.py`

### Features

- **Delta sync**: Only updates changed products (based on content hash)
- **Multi-client support**: Syncs all configured clients automatically
- **Efficient**: Skips unchanged products, only upserts modified ones
- **Sequential processing**: Processes clients one by one to avoid API rate limits

### Implementation

```python
def get_all_client_ids_with_shopify() -> List[str]:
    """
    Fetch all client IDs that have Shopify configuration.
    Queries the client_configs table for shopify_config entries.
    """
    query = """
        SELECT DISTINCT c.id 
        FROM clients c
        INNER JOIN client_configs cc ON c.id = cc.client_id
        WHERE cc.config_key = 'shopify_config'
        AND cc.config_value IS NOT NULL
        AND cc.config_value != '{}'
    """
    # Execute and return client IDs...

async def sync_client_products(client_id: str) -> Dict[str, Any]:
    """
    Perform delta sync for a single client.
    """
    orchestrator = ProductIngestionOrchestrator()
    result = await orchestrator.delta_sync_products(client_id=client_id, source="auto")
    
    return {
        "client_id": client_id,
        "success": result.success,
        "products_added": result.products_added,
        "products_updated": result.products_updated,
        "products_deleted": result.products_deleted,
        "products_unchanged": result.products_unchanged,
        "duration": result.duration
    }

async def _async_product_vector_delta_sync() -> Dict[str, Any]:
    """
    Async implementation of delta sync for all clients.
    """
    client_ids = get_all_client_ids_with_shopify()
    
    results = []
    for client_id in client_ids:
        result = await sync_client_products(client_id)
        results.append(result)
    
    return {
        "success": True,
        "clients_processed": len(client_ids),
        "total_products_added": sum(r.get("products_added", 0) for r in results),
        "total_products_updated": sum(r.get("products_updated", 0) for r in results),
        "total_products_deleted": sum(r.get("products_deleted", 0) for r in results),
        "total_products_unchanged": sum(r.get("products_unchanged", 0) for r in results),
        "results": results
    }

def product_vector_delta_sync() -> Dict[str, Any]:
    """
    Main entry point for the cron job.
    Synchronous wrapper for async implementation.
    """
    return asyncio.run(_async_product_vector_delta_sync())
```

### Cron Schedule

The job is scheduled to run every 4 hours:

```
0 */4 * * * /path/to/python -c "from fashion_bot.cron_jobs.product_vector_sync_job import product_vector_delta_sync; product_vector_delta_sync()"
```

Or via the manual trigger endpoint:
```
POST /cron/trigger-product-vector-sync
```

---

## Database Access

### Reading Shopify Config

```python
async def _get_shopify_config(self, client_id: str) -> Optional[ShopifyConfig]:
    """
    Reads Shopify credentials from client_platform_credentials table.
    """
    query = """
        SELECT credential_value 
        FROM client_platform_credentials 
        WHERE client_id = %s 
        AND platform = 'shopify' 
        AND is_active = true
    """
    result = await self.db_manager.fetch_one(query, (client_id,))
    
    if result:
        creds = json.loads(result["credential_value"])
        return ShopifyConfig(
            shop_domain=creds["shop_domain"],
            access_token=creds["access_token"]
        )
    return None
```

### Reading Website URL (Fallback)

```python
async def _get_website_url(self, client_id: str) -> Optional[str]:
    """
    Reads website URL from clients table for JSON fallback.
    """
    query = """
        SELECT website_url 
        FROM clients 
        WHERE id = %s AND is_active = true
    """
    result = await self.db_manager.fetch_one(query, (client_id,))
    return result["website_url"] if result else None
```

---

## Search Query Examples

| User Query | Vector Search | Metadata Filter |
|------------|---------------|-----------------|
| "Summer Hoodies" | Semantic on text | `product_type: Hoodie` |
| "Black denim" | Semantic on text | `colors contains "black"` |
| "Products under Rs 2000" | Generic query: "products" | `price_max <= 2000` |
| "Hoodies with zipper" | Semantic on text | `product_type: Hoodie` |
| "Cotton t-shirts large size" | Semantic on text | `sizes contains "L"` |

### Handling Price-Only Queries (Hybrid Approach)

Upstash Vector **requires a vector/text query** to perform a search—you cannot query by metadata alone. For queries that are purely filter-based (e.g., "Products under Rs 2000"), we use a **hybrid approach**:

1. Use a generic embedding term like `"products"` or `"all items"` for the vector search
2. Apply metadata filters to narrow results by price, category, etc.
3. Results are returned ranked by semantic similarity within the filtered set

```python
# Example: "Products under Rs 2000"
results = index.query(
    data="products",  # Generic query for embedding
    top_k=50,
    filter="client_id = 'client123' AND price_max <= 2000",
    include_metadata=True
)
```

**Why this works**:
- The semantic search returns products broadly matching "products"
- The metadata filter narrows results to the price range
- Since all products match "products" reasonably well, the filter becomes the primary selector
- Results maintain consistent ranking within the filtered set

---

## Environment Variables

```bash
# Upstash Vector DB
UPSTASH_VECTOR_REST_URL=https://your-index.upstash.io
UPSTASH_VECTOR_REST_TOKEN=your-token-here

# Optional: Rate limiting
UPSTASH_VECTOR_BATCH_SIZE=100
UPSTASH_VECTOR_RATE_LIMIT_RPS=10
```

---

## Integration with agent_controller.py

```python
from fashion_bot.services.product_ingestion import ProductIngestionOrchestrator

@router.post("/api/v1/products/ingest")
async def ingest_products(request: ProductIngestionRequest):
    """
    Endpoint to trigger product ingestion to VectorDB.
    """
    orchestrator = ProductIngestionOrchestrator(db_manager)
    
    result = await orchestrator.ingest_products(
        client_id=request.client_id,
        source=request.source,
        force_refresh=request.force_refresh
    )
    
    return {
        "success": result.failed_count == 0,
        "client_id": request.client_id,
        "source_used": result.source_used,
        "products_ingested": result.success_count,
        "products_failed": result.failed_count,
        "duration_seconds": result.duration,
        "timestamp": result.timestamp,
        "failed_products": result.failed_details
    }

@router.get("/api/v1/products/ingest/status/{client_id}")
async def get_ingestion_status(client_id: str):
    """
    Returns ingestion status for a client.
    """
    orchestrator = ProductIngestionOrchestrator(db_manager)
    return await orchestrator.get_status(client_id)
```

---

## Error Handling

| Scenario | Response |
|----------|----------|
| Invalid `client_id` | Return 404 with clear message |
| No Shopify credentials + no website | Return 400 with instructions |
| Shopify API errors | Log, retry with backoff, partial success response |
| Upstash errors | Log, return partial success with failed count |
| Rate limiting | Implement exponential backoff |

---

## Design Decisions

### Multi-tenancy Approach
**Decision**: Single index with `client_id` in metadata (Option A)

**Rationale**:
- Simpler infrastructure management
- Lower cost (single index vs multiple)
- Metadata filtering provides sufficient isolation
- Easier to query across clients if needed in future

### Ingestion Mode
**Decision**: Synchronous ingestion

**Rationale**:
- Simpler implementation
- Immediate feedback on success/failure
- Suitable for current catalog sizes
- Can be enhanced to async if needed for larger catalogs

### Search Endpoint
**Decision**: Implemented as `/api/v1/products/search`

**Rationale**:
- Provides direct API access for testing and external integrations
- Supports metadata filters (price, product_type, in_stock)
- Returns similarity scores for ranking

---

## Product Details Agent Integration

The vector search is integrated into the product details agent as a fallback when Shopify's exact/synonym search fails.

### Search Flow in `ShopifyProductAdapter.search_products_by_name`

```
1. Exact Search (Shopify GraphQL)
   ↓ No matches?
2. Synonym Search (jeans → denim)
   ↓ No matches?
3. Vector Search (Upstash semantic search) ← NEW
   ↓ Returns results in Shopify-compatible format
```

### Integration Details

**File**: `fashion_bot/shopify/tools/product_adapter.py`

**Method**: `_vector_search_products(query, limit, state)`

**Features**:
- Automatically falls back to vector search when Shopify search returns no results
- Converts vector results to Shopify-compatible format for seamless integration
- Marks results with `_source: "vector_search"` for tracking
- Includes `_vector_score` for debugging similarity scores

**Example Flow**:
```
User: "summer hoodies"
→ Shopify exact search: No results
→ Synonym search: No results  
→ Vector search: Returns 3 products with high semantic similarity
→ Agent presents products to user
```

---

## Tool Integration

### search_products_by_price Tool

A dedicated tool for price-based product discovery using vector search:

**File**: `fashion_bot/tool_factory.py`

```python
@tool
def search_products_by_price(
    max_price: float = None, 
    min_price: float = None, 
    product_type: str = None, 
    limit: int = 5
) -> dict:
    """
    Search for products by price range using semantic vector search.
    Use when customer asks for products under/over a certain price, or within a budget.
    
    Examples:
    - "products under Rs 2000" → max_price=2000
    - "jeans under 1500" → max_price=1500, product_type="jeans"
    - "shirts between 1000 and 2000" → min_price=1000, max_price=2000, product_type="shirts"
    
    Args:
        max_price: Maximum price filter (e.g., 2000 for "under Rs 2000")
        min_price: Minimum price filter (e.g., 1000 for "above Rs 1000")
        product_type: Optional product category filter (e.g., "jeans", "shirts")
        limit: Maximum number of products to return (default: 5)
    """
    from fashion_bot.services.product_ingestion import UpstashVectorService
    
    client_id = state.get('client_id')
    query = product_type if product_type else "products fashion clothing"
    
    vector_service = UpstashVectorService()
    
    results = vector_service.search(
        query=query,
        client_id=client_id,
        top_k=limit,
        min_price=min_price,
        max_price=max_price,
        in_stock_only=False
    )
    
    # Convert to standard product format and add to conversation context
    products = [...]
    
    return {
        "found": True,
        "count": len(products),
        "products": products,
        "price_range_msg": f"under Rs {max_price}" if max_price else ""
    }
```

### Usage Examples

| User Query | Tool Parameters |
|------------|-----------------|
| "products under Rs 2000" | `max_price=2000` |
| "jeans under 1500" | `max_price=1500, product_type="jeans"` |
| "shirts between 1000 and 2000" | `min_price=1000, max_price=2000, product_type="shirts"` |
| "expensive dresses above 5000" | `min_price=5000, product_type="dresses"` |

---

## Future Enhancements

1. ~~**Incremental sync**: Only ingest new/updated products since last sync~~ ✅ IMPLEMENTED (Delta Sync)
2. **Webhook-triggered ingestion**: Auto-ingest on Shopify product updates
3. **WooCommerce support**: Add `WooCommerceProductService`
4. **Multi-index support**: Separate indexes per client for isolation (if needed)
5. ~~**Scheduled re-ingestion**: Cron job for periodic refresh~~ ✅ IMPLEMENTED (4-hour cron)
6. ~~**Search endpoint**: Semantic search with metadata filtering~~ ✅ IMPLEMENTED
7. ~~**Price-based search tool**: Search products by price range~~ ✅ IMPLEMENTED
8. ~~**Rich metadata**: Metafields (fabric, care, size chart), compare prices, variants~~ ✅ IMPLEMENTED
9. **Real-time sync**: WebSocket notifications for immediate updates
10. **Search analytics**: Track popular queries and improve relevance

---

## API Endpoints Summary

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/products/ingest` | POST | Full ingestion of all products |
| `/api/v1/products/ingest/status/{client_id}` | GET | Get ingestion status |
| `/api/v1/products/delta-sync` | POST | Delta sync for single client |
| `/api/v1/products/delta-sync/all` | POST | Delta sync for all clients |
| `/api/v1/products/search` | POST | Semantic product search |
| `/cron/trigger-product-vector-sync` | POST | Manually trigger cron job |

---

## Data Models

### NormalizedProduct

```python
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
    # Extended fields
    compare_at_price_min: Optional[float] = None
    compare_at_price_max: Optional[float] = None
    total_inventory: int = 0
    status: str = "active"
    fabric: Optional[str] = None
    care_instructions: Optional[str] = None
    fit_type: Optional[str] = None
    size_chart: Optional[Dict[str, Any]] = None
    all_images: List[str] = field(default_factory=list)
    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    variants: List[Dict[str, Any]] = field(default_factory=list)
    
    def to_vector_data(self, client_id: str) -> VectorData:
        """Convert to VectorData for Upstash ingestion."""
        ...
    
    def compute_content_hash(self) -> str:
        """Compute MD5 hash for delta sync comparison."""
        ...
```

### DeltaSyncResult

```python
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
```
